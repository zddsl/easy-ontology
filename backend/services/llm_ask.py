"""LLM 自然语言问答：两阶段管线（Text2Sparql 工作流移植版）。

流程（参考 Dify 工作流 Text2Sparql(7)）：
  本体(+映射增强) → 路径库（ontology_paths，最大跳数可调）
  → Stage1 LLM 词汇识别（涉及类/属性，结构化 JSON）
  → 词汇校正+子类展开+A/B/C 规则过滤路径库 → 取样探测涉及属性的真实值
  → Stage2 LLM 在白名单路径内生成 SPARQL（约 20 条端点适配规则）
  → 围栏剥离/逆向语法修复/PREFIX 注入/谓词白名单校验
  → 执行；失败走一次修复分支（11 条常见病因清单）
  → LLM 依据查询结果作答（空结果与查询失败严格区分）。

API key 由调用方（路由层从 settings_store）传入，本模块不落任何密钥。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
import rdflib
from rdflib.namespace import OWL, RDF, RDFS

from backend.services import sparql_proxy
from backend.services.ontology_paths import (
    PathLibraryError,
    build_path_library,
    clamp_hops,
)
from backend.services.project_store import get_store

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


class LLMError(Exception):
    pass


# ---------- 本体摘要（/api/ontology/summary 仍在用） ----------

def _local(uri: str) -> str:
    """完整 IRI → 短名。"""
    for sep in ("#", "/"):
        if sep in uri:
            return uri.rsplit(sep, 1)[1]
    return uri


def _default_ns(g: rdflib.Graph) -> str:
    """猜项目命名空间：找出现最多的 owl:Class/DatatypeProperty/ObjectProperty 前缀。"""
    from collections import Counter

    counter: Counter[str] = Counter()
    for s in g.subjects(RDF.type, None):
        u = str(s)
        for sep in ("#", "/"):
            if sep in u:
                counter[u.rsplit(sep, 1)[0] + sep] += 1
                break
    if not counter:
        return "http://example.org/"
    return counter.most_common(1)[0][0]


def summarize_ontology(rdf_xml: str) -> dict[str, Any]:
    """从 RDF/XML 抽出 LLM 需要的结构化摘要：{ns, classes, obj_props, dt_props}。

    每个类/属性附中文名（rdfs:label）与描述（rdfs:comment）——LLM 靠它们
    把用户问题里的业务词映射到正确谓词（防"故障/描述"这类近义错位）。
    """
    g = rdflib.Graph()
    g.parse(data=rdf_xml, format="xml")
    ns = _default_ns(g)

    def _annot(ref) -> str:
        label = next(iter(sorted(str(x) for x in g.objects(ref, RDFS.label))), "")
        comment = next(iter(sorted(str(x) for x in g.objects(ref, RDFS.comment))), "")
        out = f"（{label}）" if label else ""
        if comment:
            out += f"：{comment}"
        return out

    classes = sorted({f":{_local(str(c))}{_annot(c)}" for c in g.subjects(RDF.type, OWL.Class)})

    def _prop_lines(prop_type) -> list[str]:
        rows = []
        for p in sorted(g.subjects(RDF.type, prop_type), key=str):
            dom = next(iter(g.objects(p, RDFS.domain)), None)
            rng = next(iter(g.objects(p, RDFS.range)), None)
            arrow = f"{_local(str(dom)) if dom else '?'} → {_local(str(rng)) if rng else '?'}"
            rows.append(f":{_local(str(p))}{_annot(p)} [{arrow}]")
        return rows

    return {
        "ns": ns,
        "classes": classes,
        "obj_props": _prop_lines(OWL.ObjectProperty),
        "dt_props": _prop_lines(OWL.DatatypeProperty),
    }


# ---------- DeepSeek 客户端 ----------

async def _deepseek_chat(
    messages: list[dict[str, str]],
    api_key: str,
    response_format: dict | None = None,
    temperature: float = 0.0,
) -> str:
    """薄 wrapper。返回 message.content 字符串；异常统一抛 LLMError。"""
    payload = {"model": "deepseek-chat", "messages": messages, "temperature": temperature}
    if response_format:
        payload["response_format"] = response_format
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                DEEPSEEK_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.RequestError as e:
        raise LLMError(f"DeepSeek 网络失败：{e}")
    if r.status_code != 200:
        raise LLMError(f"DeepSeek {r.status_code}：{r.text[:400]}")
    try:
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        raise LLMError(f"DeepSeek 响应解析失败：{e}；原文：{r.text[:400]}")


async def _chat_json(messages: list[dict[str, str]], api_key: str, what: str) -> dict:
    """要求 JSON 输出并解析；解析失败抛 LLMError。"""
    raw = await _deepseek_chat(messages, api_key, response_format={"type": "json_object"})
    try:
        return json.loads(raw)
    except Exception as e:
        raise LLMError(f"LLM {what} 输出解析失败：{e}；原文：{raw[:400]}")


# ---------- Stage1：词汇识别 ----------

_S1_SYS = """你是一个本体词汇识别助手。根据给定的三元组模式和中文标签，判断回答用户问题需要用到哪些类和属性。
按 JSON 输出：{"classes": 需要的类数组, "properties": 需要的属性数组}。
要求：优先使用模式/标签中出现的英文类名和属性名；对中文或拿不准的词直接原样输出，系统会自动校正。"""


async def _stage1_predict(question: str, patterns_r1: str, labels: str, api_key: str) -> dict:
    """1 跳菜单 + 标签对照 → {classes[], properties[]}。"""
    user = (
        "以下是三元组模式，形式为 {Class}->Property->{Class} 或 {Class}->Property->(Literal)：\n\n"
        "### Patterns\n" + patterns_r1 + "\n### End of Patterns\n\n"
        "类与属性的英文=中文对照表（输出时使用英文名）：\n" + labels + "\n\n"
        "请根据以上模式判断：回答下面的问题需要用到哪些类和属性？\n\n"
        "用户问题：" + question
    )
    out = await _chat_json(
        [{"role": "system", "content": _S1_SYS}, {"role": "user", "content": user}],
        api_key, "词汇识别")
    return {"classes": list(out.get("classes") or []),
            "properties": list(out.get("properties") or [])}


# ---------- 词汇校正 + 路径过滤（工作流「预测路径」节点移植） ----------

def _load_vocab(vocab_text: str) -> tuple[dict, dict]:
    try:
        v = json.loads(vocab_text or "{}")
    except Exception:
        v = {}
    return v.get("classes", {}) or {}, v.get("props", {}) or {}


def _correct_term(term, vocab_map: dict, notes: list) -> str | None:
    """强制词汇映射：预测词 → 本体规范名。
    优先级：规范名精确(忽略大小写) > 标签精确 > 标签包含(唯一命中) > 放弃保留原文"""
    if not isinstance(term, str) or not term.strip():
        return None
    t = term.strip()
    name_lc = {n.lower(): n for n in vocab_map}
    hit = name_lc.get(t.lower())
    if hit:
        return hit
    label_lc: dict[str, list[str]] = {}
    for n, l in vocab_map.items():
        label_lc.setdefault((l or "").strip().lower(), []).append(n)
    hit = label_lc.get(t.lower())
    if hit and len(hit) == 1:
        notes.append(f"'{t}'→{hit[0]}")
        return hit[0]
    hits = {n for n, l in vocab_map.items()
            if len(t) >= 2 and (t.lower() in (l or "").lower() or (l or "").lower() in t.lower())}
    if len(hits) == 1:
        n = hits.pop()
        notes.append(f"'{t}'→{n}")
        return n
    return None


def _correct_set(terms, vocab_map: dict, notes: list) -> set:
    out, seen = [], set()
    for x in terms:
        c = _correct_term(x, vocab_map, notes)
        sx = (c or (x if isinstance(x, str) else str(x))).strip()
        if not sx:
            continue
        k = sx.lower()
        if k not in seen:
            seen.add(k)
            out.append(sx)
    return set(out)


def _classes_of(line: str) -> set:
    return set(re.findall(r"\{(\w+)\}", line))


def _props_of(line: str) -> set:
    return {p.lstrip("^") for p in re.findall(r"->\s*(\^?[A-Za-z_]\w*)", line)}


def _filter_paths(prediction: dict, lib: dict) -> dict:
    """Stage1 结果 → 过滤路径 + 涉及类元信息 + 取样探测查询。"""
    class_vocab, prop_vocab = _load_vocab(lib["vocab"])
    meta = json.loads(lib["class_meta"])
    superclass = meta.get("_subclass", {})

    notes: list = []
    classes = _correct_set(prediction.get("classes"), class_vocab, notes)
    props = _correct_set(prediction.get("properties"), prop_vocab, notes)

    expanded = set(classes)
    for c in classes:
        expanded.update(superclass.get(c, []))
    cls = expanded

    lines = [l.strip() for l in lib["paths_full"].splitlines() if "->" in l]

    # 取样探测：涉及类的数据属性各采一个样例（MIN 确定性，日期顺手得最早值）
    probe_pairs: list[str] = []
    probe_map: dict[str, str] = {}
    for c in sorted(cls):
        if c in meta and not c.startswith("_"):
            for pl in re.findall(r"([A-Za-z_]\w*)\s*\(", meta[c]["data_props"]):
                if pl not in probe_map:
                    probe_map[pl] = c
                    probe_pairs.append(pl)
    if probe_pairs and lib["path_prefix"]:
        _in = ", ".join(":" + pl for pl in probe_pairs)
        probe_query = (lib["path_prefix"] + "\n"
                       "SELECT ?p (MIN(?v) AS ?sample) WHERE { ?x ?p ?v . "
                       f"FILTER(?p IN ({_in})) }} GROUP BY ?p")
    else:
        probe_query = "SELECT ?p (MIN(?v) AS ?sample) WHERE { ?x ?p ?v . FILTER(1=0) } GROUP BY ?p"

    if not cls and not props:
        return {"filtered_paths": "\n".join(lines),
                "involved_classes": "(none)",
                "probe_query": probe_query,
                "probe_map": json.dumps(probe_map, ensure_ascii=False),
                "cls": set(), "used_preds": set()}

    def rule_A():
        return [l for l in lines if (not cls or cls <= _classes_of(l)) and (not props or props <= _props_of(l))]

    def rule_B():
        return [l for l in lines if (cls and cls <= _classes_of(l)) or (props and props <= _props_of(l))]

    def rule_C():
        return [l for l in lines if (cls and cls & _classes_of(l)) or (props and props & _props_of(l))]

    kept = rule_A() or rule_B() or rule_C() or lines

    meta_lines = []
    if cls:
        meta_lines.append("本题涉及类：" + ", ".join(
            c + (f"({class_vocab[c]})" if class_vocab.get(c) else "")
            for c in sorted(cls) if not c.startswith("_")))
    if notes:
        meta_lines.append("词汇校正：" + "；".join(notes))
    for c in sorted(cls):
        if c in meta and not c.startswith("_"):
            meta_lines.append(f"- {c}: {meta[c]['data_props']}")
            meta_lines.append(f"  IRI样例: {meta[c]['iri']}")

    return {"filtered_paths": "\n".join(kept),
            "involved_classes": "\n".join(meta_lines) if meta_lines else "(none)",
            "probe_query": probe_query,
            "probe_map": json.dumps(probe_map, ensure_ascii=False),
            "cls": cls, "used_preds": props}


# ---------- 取样结果 → 数据样例行 ----------

def _data_samples_from(body: str, probe_map_json: str) -> str:
    """解析取样 JSON 为样例行；任何异常降级为空样例，绝不阻断主链路。"""
    lines = []
    try:
        pm = json.loads(probe_map_json) if (probe_map_json or "").strip() else {}
        d = json.loads(body) if (body or "").strip() else {}
        for b in (d.get("results") or {}).get("bindings") or []:
            p = (b.get("p") or {}).get("value", "")
            smp = b.get("sample") or {}
            v = smp.get("value", "")
            dt = (smp.get("datatype") or "").rsplit("#", 1)[-1]
            if not p or not v:
                continue
            pl = p.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            cls = pm.get(pl) or "?"
            note = ""
            if dt in ("dateTime", "date"):
                note = (f"^^xsd:{dt}（日期类型：比较字面量必须带同样后缀并声明 PREFIX xsd:，"
                        f'如 FILTER(?d >= "2017-09-01T00:00:00"^^xsd:{dt})；'
                        "无类型字符串比较会静默返回空；按年/月统计用 YEAR(?d)/MONTH(?d)）")
            elif dt in ("double", "decimal", "float", "int", "integer", "long", "short"):
                note = ("^^xsd:" + dt + "（数值类型：等值匹配写 FILTER(?q = 1)，"
                        "不要把数字写进三元组模式——存储词法可能不同会静默匹配不到）")
            lines.append(f"- {cls}.{pl} 实际样例值: \"{v}\"" + note)
    except Exception:
        lines = []
    return "\n".join(lines)


# ---------- Stage2：白名单内生成 SPARQL ----------

_S2_SYS = "你是 SPARQL 查询生成专家。严格按照用户消息中的指令执行，只输出 SPARQL 查询语句本身，不要任何解释。"


def _stage2_prompt(question: str, lib: dict, filtered: dict, data_samples: str,
                   max_hops: int | None = None) -> str:
    hop_rule = (f"\n- 本题关联跳数上限为 {max_hops} 跳：从一类出发经对象属性到达另一类的跳数不得超过 {max_hops}，"
                f"禁止把短路径串联成更长的查询；没有 {max_hops} 跳以内的路径就如实说明无法回答，不得硬凑。\n"
                if max_hops else "")
    return f"""给你以下路径模式，形式为
{{类}}->属性->{{类}}->...->属性->{{类}} 或 (字面量)

### 前缀声明

{lib['path_prefix']}

### 路径模式（唯一允许使用的谓词来源）

{filtered['filtered_paths']}

### 涉及类的数据属性与 IRI 样例

{filtered['involved_classes']}

### 涉及属性的实际数据样例（值格式以此为准）

{data_samples or "（没有样例的属性按路径模式中的类型标注处理）"}

### 规则
{hop_rule}
- 只允许使用上面路径模式中出现的类和属性。
- 类断言（a :X）中的 X 必须使用「本题涉及类」中列出的类，不得替换成其他类。
- 绝对不要把类当作属性使用（反之亦然）。
- 模式中 ->^属性-> 表示该属性要逆向使用。以 {{A}} ->^p-> {{B}} 为例，两种等价写法： ① 对调写法：?bVar p ?aVar（推荐，不易错） ② 逆向写法：?aVar ^:p ?bVar——注意变量位置：A 的变量写在 ^ 的左侧、B 的变量写在 ^ 的右侧；写成 ?bVar ^:p ?aVar 就是语义反了，查询会静默返回空结果。^ 必须紧贴冒号之前（^:p 正确，:^p 非法）。
- 逆向跳请写成独立的三元组，不要把 ^ 嵌进链式路径（如 :p/^:q 这种链式混写在 SPARQL 端点会返回空结果）。
- 如果多条路径都能回答问题，选择跳数最少、最直接的那条，不要绕路。
- 如果问题中包含具体的实体值，用对应的数据属性匹配（例如 ?x :某编号属性 "值"，具体属性以路径模式与数据样例为准），或参照 IRI 样例。
- SELECT 时优先返回数据属性的值（编号、名称、日期等字面量），不要只返回 IRI；用户需要看到的是可读值，状态/名称/类别一律取其字面量属性而非实体。
- 路径模式里数据属性的标注 (Literal字符串)/(Literal数值) 是该属性的数据类型：字符串属性的值必须加双引号（如 ?x :某字符串属性 "值"）；数值属性用于比较、排序、求和时不加引号（如 FILTER(?q > 1.5)、ORDER BY DESC(?q)）；数值等值匹配必须写 FILTER(?q = 值)，不要把数字写进三元组模式（如 :某数值属性 1.0）——存储词法可能不同会静默匹配不到。标注为 (Literal) 时按字符串处理。
- 日期/时间比较以数据样例的标注为准：样例带 ^^xsd:dateTime（或 ^^xsd:date）后缀时，比较字面量必须带同样后缀并声明 PREFIX xsd:（如 FILTER(?d >= "2017-09-01T00:00:00"^^xsd:dateTime && ?d < "2017-10-01T00:00:00"^^xsd:dateTime)）——用无类型字符串比较会静默返回空；按年/月统计用 YEAR(?d)/MONTH(?d) 分组。
- 样例为无类型字符串的日期按字符串比较："某日之前"用 FILTER(?d < "YYYY-MM-DD")；"到某日（含当天）"写成 FILTER(?d < 次日)——写 <= "当天日期" 会漏掉当天全部数据；月度/年度统计用范围比较 >= "YYYY-MM" && < "YYYY-MM下1位"。
- 禁止在 FILTER 里用 STR() 包裹变量（如 STRSTARTS(STR(?d),...) 会让端点超时）；字符串前缀匹配直接写 STRSTARTS(?d, "前缀")。
- 本端点不支持 xsd 类型构造函数（xsd:decimal(?x)、xsd:double(?x) 等会直接报错）；聚合与比较直接使用变量，不要包裹类型转换。
- 不支持标量子查询（FILTER 里嵌 (SELECT …) 会报错）；要"最值+对应记录"时用 ORDER BY + LIMIT 1。
- 对计算结果判等必须用容差 FILTER(ABS(表达式 - 值) < 0.01)（如 数量×单价 对比 行成本）；浮点直接用 = / != 会把表示噪声当成差异。对存储值的直接等值（FILTER(?q = 1)）不受此限。
- 匹配方式与问题措辞对齐：问"等于/是某值"写成三元组模式（如 ?x :某字符串属性 "值"，走索引）；问"包含/带有某字"才用 CONTAINS(?x, "字")；禁止把"等于"写成 CONTAINS。
- 问题里的数量词（如"5530张"中的 5530）只是统计描述，绝不能作为实体值写进查询；实体编号锚定哪个属性，对照数据样例中各属性值的实际形态决定（长纯数字、字母数字混合等各有含义，勿凭猜测）。
- 类别判定优先使用子类断言（用「本题涉及类」中列出的类名，如 ?x a :某子类名），不要用数据属性值猜类别——其值域可能与问题措辞不一致。
- 按 JSON 输出 {{"sparql": "..."}}，不要输出任何多余内容。

用户问题：{question}"""


async def _stage2_generate(prompt: str, api_key: str) -> str:
    out = await _chat_json(
        [{"role": "system", "content": _S2_SYS}, {"role": "user", "content": prompt}],
        api_key, "SPARQL 生成")
    return str(out.get("sparql") or "").strip()


# ---------- SPARQL 后处理 + 谓词白名单（工作流「llm输出处理」节点移植） ----------

def _preds_of(q: str) -> set:
    q = re.sub(r'"[^"]*"', "", q)          # 排除字符串字面量里的冒号
    q = re.sub(r"PREFIX[^<]*<[^>]*>", "", q, flags=re.I)  # 排除 PREFIX 声明
    return set(re.findall(r"(?<![\w:])\^?:([A-Za-z_]\w*)", q))


def _postprocess_sparql(arg1, path_prefix: str, full_paths: str) -> tuple[str, bool, list[str], set]:
    """剥围栏/修逆向畸形/补 PREFIX/白名单校验。返回 (sparql, ok, invalid_preds, used_preds)。"""
    t = str(arg1.get("sparql") if isinstance(arg1, dict) else arg1 or "").strip()
    m = re.search(r"```(?:sparql)?\s*(.*?)```", t, re.S | re.I)
    if m:
        t = m.group(1).strip()
    t = t.strip("` \n\r")
    # 自动修复逆向语法畸形：?x :^p ?y → ?y :p ?x
    t = re.sub(r"(\?\w+)\s*:\^(\w+)\s*(\?\w+)", r"\3 :\2 \1", t)
    # 确定性兜底：用了前缀缩写但丢了 PREFIX 声明时强制补上
    if t and "PREFIX" not in t.upper() and (path_prefix or "").strip():
        t = path_prefix.strip() + "\n" + t
    ok = t.lstrip().upper().startswith(("PREFIX", "SELECT", "ASK", "CONSTRUCT"))
    invalid: list[str] = []
    used: set = set()
    try:
        menu_preds: set = set()
        for l in (full_paths or "").splitlines():
            if "->" in l:
                menu_preds.update(re.findall(r"\{(\w+)\}", l))
                for p in re.findall(r"->\s*(\^?[A-Za-z_]\w*)", l):
                    menu_preds.add(p.lstrip("^"))
        used = _preds_of(t)
        invalid = sorted(used - menu_preds)
        if invalid:
            ok = False
    except Exception:
        invalid = []
    return t, ok, invalid, used


# ---------- 修复分支（工作流「组装修复prompt」移植） ----------

def _repair_prompt(question: str, failed_sparql: str, lib: dict,
                   filtered: dict, data_samples: str, extra_causes: str = "") -> str:
    return f"""刚才的 SPARQL 查询在端点**执行已确认失败**（语法错误/报错/超时/结果过大之一，不是偶发）。你的任务是诊断原因并输出一条**改写后**的查询。
硬性要求：禁止原样输出失败查询或其等价形式。STR() 包装必须去掉（如 STRSTARTS(STR(?d),...) → STRSTARTS(?d,...)）；CONTAINS 仅当问题语义是"等于/为某值"时改写为三元组模式（如 ?x :某字符串属性 "值"），问题语义本来就是"包含/带有"的保留 CONTAINS；数值等值改写为 FILTER(?q = 1)。

### 原始用户问题

{question}

### 失败的 SPARQL

{failed_sparql}

### 前缀声明

{lib['path_prefix']}

### 路径模式（唯一允许使用的谓词来源）

{filtered['filtered_paths']}

### 涉及类的数据属性与 IRI 样例

{filtered['involved_classes']}

### 涉及属性的实际数据样例（值格式以此为准）

{data_samples or "（无）"}

### 常见失败原因（按概率逐条排查）

1. FILTER 里用 STR() 包裹变量（如 STRSTARTS(STR(?d),...)）——端点会超时。字符串前缀匹配直接写 STRSTARTS(?d, "前缀")。
2. 用 FILTER(CONTAINS(?x, "值")) 做大表包含匹配——会超时。"等于某个值"必须写成三元组模式（如 ?x :某字符串属性 "值"），禁止用 CONTAINS 表达"等于"。
3. 数值等值写成了三元组模式（如 :某数值属性 1.0）——存储词法可能是 1.000000，字面量对不上会静默匹配不到。数值等值必须写 FILTER(?q = 1)。
4. 查询无约束/无具体模式（如 ?s ?p ?o 全表捞取）——结果过大被拒绝。必须包含至少一个具体类断言（?x a :某类）或具体属性值匹配，并给 SELECT 加合理 LIMIT（如 LIMIT 200）。
5. 缺 PREFIX 声明；逆向语法畸形（:^p 非法，应为对调写法 ?b :p ?a 或 ?aVar ^:p ?bVar）；把 ^ 嵌进链式路径（:p/^:q 会返回空）。
6. 用了菜单外的类/属性名，或把类当属性用。
7. 用了 xsd 类型构造函数（xsd:decimal(...)、xsd:double(...) 等）——端点不支持会报错；去掉包裹，聚合直接用变量。
8. FILTER 里嵌了标量子查询 (SELECT …)——不支持；改用 ORDER BY + LIMIT 1。
9. 浮点计算结果用 = / != 判等——全是表示噪声；改用 FILTER(ABS(表达式 - 值) < 0.01)。
10. 日期比较用了无类型字符串——样例标注为 ^^xsd:dateTime 时必须改用带同类型后缀的字面量并声明 PREFIX xsd:，否则静默返回空。
11. 把题面数量词当成了实体值（如把"5530张"的 5530 写进编号属性）——删除该锚定，按语义重新定位实体。
{extra_causes}

### 修复要求

- 按 JSON 输出 {{"sparql": "..."}}：修正后的 SELECT 查询本身，不要任何解释。
- 只允许使用路径模式中出现的类和属性。
- 方向或谓词选错时按路径模式修正；日期/数值写法以上面的失败原因为准。"""


# ---------- 回答 ----------

_SYS_ANSWER = """你是数据问答助手。根据 SPARQL 查询结果用中文回答用户问题。

规则：
- 结果为空或查询失败时如实说明，不得编造；查询成功但结果为空时说"没有/不存在"，"未查到相关数据"仅用于查询失败的情形。
- 问题含多个子问题时逐问作答，不能只答一半。
- 实体称谓与问题保持一致（问缺陷单就答缺陷单，不要说成工单）。
- 回答中不得出现 http:// 开头的 IRI，一律用编号、名称等可读值。
- 问题涉及本体没有的概念时，明确说明本体不含该概念，不得用近义概念顶替。
- 能统计总数时给出总数；引用具体值时保持与结果一致。
- 查询结果为空或失败时，只能回答"没有找到/查询失败"，禁止给出任何具体数值——你输出的每个数字必须能在查询结果里原样找到，找不到的数字一个都不能写。
- 用户要求"列前 N 个 + 汇总"时，汇总总数必须来自查询的全量计数，不得用本次列出的条数充当总数；查询没有返回全量计数时，如实说明"查询只返回了前 N 条，未获得总数"。"""


def _used_paths_lines(filtered_paths: str, used_preds: set) -> str:
    """路径回显：被生成查询真正用到的**对象属性**所命中的路径行（只用了类断言时为空）。"""
    lines = [l for l in filtered_paths.splitlines() if l.strip()]
    hit = [l for l in lines if _props_of(l) & used_preds]
    return "\n".join(hit)


def _edge_preds(full_paths: str) -> set:
    """路径库中出现过的全部对象属性（跳数统计用）：只统计两端都是 {类} 的属性，
    指向 (Literal…) 的数据属性不算。"""
    ps: set = set()
    for l in (full_paths or "").splitlines():
        if "->" not in l:
            continue
        toks = [t.strip() for t in l.split("->")]
        for i in range(1, len(toks) - 1, 2):  # 奇数位是属性，偶数位是类或字面量
            if toks[i + 1].startswith("{"):
                ps.add(toks[i].lstrip("^").strip())
    return ps


def _query_hops(q: str, obj_preds: set) -> int:
    """估计查询的对象属性跳数：WHERE 里 var—var 对象属性边构成的链的最大长度。
    数据属性（不在 obj_preds 里）不算跳数；类断言不算跳数。"""
    q = re.sub(r'"[^"]*"', "", q)  # 排除字符串字面量
    edges: dict[str, set[str]] = {}
    for s, p, o in re.findall(r"(\?\w+)\s*[:^]?([A-Za-z_]\w*)\s*(\?\w+)", q):
        if p.lstrip("^") in obj_preds:
            edges.setdefault(s, set()).add(o)
            edges.setdefault(o, set()).add(s)
    best = 0

    def dfs(v: str, seen: set, depth: int) -> None:
        nonlocal best
        best = max(best, depth)
        for w in edges.get(v, ()):
            if w not in seen:
                dfs(w, seen | {w}, depth + 1)

    for v in list(edges):
        dfs(v, {v}, 0)
    return best


# ---------- 主管线 ----------

async def ask(question: str, api_key: str, route: str = "virtual",
              max_hops: int | None = None) -> dict[str, Any]:
    """两阶段问答；返回中间产物+最终答，方便调试。route=virtual|materialized。"""
    store = get_store()
    if not store.ontology_path.exists():
        raise LLMError("请先上传本体（当前项目无 ontology.rdf）")
    hops = clamp_hops(max_hops)

    # 本体文本：有映射时用 viewer 增强版（从映射反推 range，治本体缺 range）
    rdf_xml = store.ontology_path.read_text(encoding="utf-8")
    try:
        if store.mapping_path.exists():
            from backend.services.viewer_enricher import enrich
            rdf_xml = enrich(rdf_xml, store.mapping_path.read_text(encoding="utf-8"))
    except Exception:
        pass  # 增强失败用原文本

    try:
        lib = build_path_library(rdf_xml, max_hops=hops)
    except PathLibraryError as e:
        raise LLMError(str(e))

    # Stage1：词汇识别
    t0 = time.time()
    prediction = await _stage1_predict(question, lib["patterns_r1"], lib["labels"], api_key)
    filtered = _filter_paths(prediction, lib)

    # 取样探测：失败降级为空样例，绝不阻断
    probe_body = '{"results":{"bindings":[]}}'
    try:
        resp, _ = await sparql_proxy.forward(
            filtered["probe_query"], "application/sparql-results+json", route=route,
            meta={"question": question + "（取样探测）"})
        if resp.status_code == 200:
            probe_body = resp.text
    except Exception:
        pass
    data_samples = _data_samples_from(probe_body, filtered["probe_map"])

    # Stage2：白名单内生成 SPARQL
    prompt = _stage2_prompt(question, lib, filtered, data_samples, max_hops=hops)
    sparql = await _stage2_generate(prompt, api_key)
    sparql, ok, invalid, used_preds = _postprocess_sparql(sparql, lib["path_prefix"], lib["paths_full"])
    gen_ms = int((time.time() - t0) * 1000)

    # 执行；跳数超限/失败/非法走一次修复分支
    t1 = time.time()
    repaired = False
    warnings = list(lib["warnings"])
    obj_preds = _edge_preds(lib["paths_full"])
    over = _query_hops(sparql, obj_preds) - hops
    csv_text, exec_error = (None, None) if over > 0 else await _execute(sparql, route, question)
    ok = csv_text is not None
    if not ok:
        repaired = True
        extra = (f"12. 生成的查询对象属性关联跳数实测超过上限 {hops} 跳（超出 {over} 跳）——"
                 f"必须只改用不超过 {hops} 跳的路径重写；若题意确实需要更长路径，"
                 f"改答一条 {hops} 跳以内最接近的问题。\n" if over > 0 else "")
        rprompt = _repair_prompt(question, sparql, lib, filtered, data_samples, extra_causes=extra)
        sparql2 = await _stage2_generate(rprompt, api_key)
        sparql2, _ok2, _inv2, used2 = _postprocess_sparql(
            sparql2, lib["path_prefix"], lib["paths_full"])
        sparql, used_preds = sparql2, (used2 or used_preds)
        over = _query_hops(sparql, obj_preds) - hops
        if over <= 0:
            csv_text, exec_error = await _execute(sparql, route, question)
            ok = csv_text is not None
        else:
            warnings.append(f"生成的查询实际跳数超过上限 {hops}（超出 {over} 跳），已拒绝执行")
            csv_text, exec_error = None, f"生成的查询跳数超过上限 {hops}，已拒绝执行"
            ok = False
    sparql_ms = int((time.time() - t1) * 1000)

    # 回答
    t2 = time.time()
    if not ok:
        csv_text = exec_error or "查询执行失败"
    answer = await _deepseek_chat(
        [
            {"role": "system", "content": _SYS_ANSWER},
            {"role": "user", "content": (
                f"用户问题：{question}\n\n执行的 SPARQL：\n{sparql}\n\n"
                f"查询结果（CSV，第一行是表头）：\n{csv_text}")},
        ],
        api_key,
        temperature=0.3,
    )
    ans_ms = int((time.time() - t2) * 1000)

    return {
        "question": question,
        "route": route,
        "max_hops": hops,
        "sparql": sparql,
        "csv": csv_text,
        "answer": answer.strip(),
        "repaired": repaired,
        "invalid_preds": invalid,
        "used_paths": _used_paths_lines(filtered["filtered_paths"], used_preds),
        "warnings": warnings,
        "timings": {"gen_ms": gen_ms, "sparql_ms": sparql_ms, "answer_ms": ans_ms},
    }


async def _execute(sparql: str, route: str, question: str) -> tuple[str | None, str | None]:
    """执行查询。成功返回 (CSV文本, None)；失败返回 (None, 错误摘要)。"""
    try:
        resp, entry_id = await sparql_proxy.forward(
            sparql, "text/csv", route=route, meta={"question": question})
    except sparql_proxy.EndpointUnavailable as e:
        raise LLMError(f"{e}；无法执行生成的查询：\n{sparql}")
    except httpx.TimeoutException:
        return None, "端点超时"
    if resp.status_code != 200:
        endpoint = "QLever" if route == "materialized" else "Ontop"
        summary = f"{endpoint} 返回 {resp.status_code}：{resp.text[:300]}"
        sparql_proxy.update_entry(entry_id, state="error")
        return None, summary
    return resp.text, None
