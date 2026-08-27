"""本体构建器纯逻辑：草稿校验 / RDF 生成 / 已有本体解析回填（无 IO，便于独立测试）。

草稿结构（draft）：
  {"version": 1, "namespace": "http://…#",
   "classes":          [{"id","name","label","comment","parentId","equivalentIds","disjointIds"}],
   "objectProperties": [{"id","name","label","comment","domainId","rangeId","inverseOfId","subPropertyOfId"}],
   "dataProperties":   [{"id","name","label","comment","domainId","range","subPropertyOfId"}]}
元素用稳定 id 相互引用，改名不断链。数据属性 domain 可空（全局属性也合法）；
equivalentIds/disjointIds 为多选数组；inverseOfId/subPropertyOfId 可空（build 时对称公理去重输出）。
"""
from __future__ import annotations

import re
from collections import Counter

from rdflib import BNode, Graph, Literal, Namespace, OWL, RDF, RDFS, URIRef, XSD

# 表单类型（draft 里 range 字段的取值）→ xsd IRI
XSD_TYPES = {
    "string": XSD.string,
    "integer": XSD.integer,
    "decimal": XSD.decimal,
    "double": XSD.double,
    "date": XSD.date,
    "datetime": XSD.dateTime,
    "boolean": XSD.boolean,
}

# 导入时 xsd local name → 表单类型（对齐 Playground parser 的 XSD_TO_TYPE：int/long→integer、float→decimal）
IMPORT_XSD_MAP = {
    "string": "string", "int": "integer", "integer": "integer", "long": "integer",
    "decimal": "decimal", "float": "decimal", "double": "double",
    "date": "date", "dateTime": "datetime", "boolean": "boolean",
}

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
NS_RE = re.compile(r"^https?://[^\s<>\"{}|^`\\]+[#/]$")

BUILTIN_NS = (str(OWL), str(RDFS), str(RDF), str(XSD))

DRAFT_VERSION = 1
DEFAULT_NS = "http://tohi.cn/2026/onto#"


class DraftError(Exception):
    """草稿结构损坏（不是内容问题，是连解析都过不了）"""


# ---------- 工具 ----------

def _local_name(iri: str) -> str:
    """IRI → local name：优先 # 分隔，否则 / 分隔。"""
    return iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _namespace_of(iri: str) -> str:
    """IRI → 命名空间（含结尾 # 或 /）。"""
    if "#" in iri:
        return iri.rsplit("#", 1)[0] + "#"
    return iri.rsplit("/", 1)[0] + "/"


def _is_builtin(ref) -> bool:
    return isinstance(ref, URIRef) and str(ref).startswith(BUILTIN_NS)


def normalize_draft(raw: dict) -> dict:
    """外部传入的草稿做结构规整，结构损坏抛 DraftError（软字段缺失给默认值）。"""
    try:
        draft = {
            "version": int(raw.get("version", DRAFT_VERSION)),
            "namespace": str(raw.get("namespace") or "").strip(),
            "classes": list(raw.get("classes") or []),
            "objectProperties": list(raw.get("objectProperties") or []),
            "dataProperties": list(raw.get("dataProperties") or []),
        }
    except (TypeError, ValueError) as e:
        raise DraftError(f"草稿结构不合法：{e}") from e
    for section in ("classes", "objectProperties", "dataProperties"):
        for item in draft[section]:
            if not isinstance(item, dict) or not item.get("id"):
                raise DraftError(f"{section} 里存在无 id 的元素")
    return draft


def draft_counts(draft: dict) -> dict:
    return {
        "classes": len(draft.get("classes") or []),
        "objectProperties": len(draft.get("objectProperties") or []),
        "dataProperties": len(draft.get("dataProperties") or []),
    }


# ---------- 校验 ----------

def detect_cycle(items: list[dict], field: str = "parentId") -> str | None:
    """沿 items 的 field 引用链找环，返回环节点 id（无环 None）。"""
    parent = {it["id"]: it.get(field) for it in items}
    state: dict[str, int] = {}  # 0=未访 1=在栈 2=完成
    for start in parent:
        if state.get(start):
            continue
        path = []
        cur = start
        while cur and cur in parent and state.get(cur, 0) == 0:
            state[cur] = 1
            path.append(cur)
            cur = parent[cur]
        if cur and state.get(cur) == 1:
            return cur  # cur 在当前 DFS 栈上 → 成环
        for node in path:
            state[node] = 2
    return None


def validate_draft(draft: dict) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。errors 阻塞 commit；warnings 仅提示（autosave 照存）。"""
    errors: list[str] = []
    warnings: list[str] = []

    ns = draft.get("namespace", "")
    if not ns:
        errors.append("命名空间不能为空")
    elif not NS_RE.match(ns):
        errors.append("命名空间须为 http(s):// 开头且以 # 或 / 结尾的 IRI 前缀")
    elif ns.startswith(BUILTIN_NS):
        errors.append("命名空间不能使用 owl/rdfs/rdf/xsd 官方命名空间")

    classes = draft.get("classes") or []
    oprops = draft.get("objectProperties") or []
    dprops = draft.get("dataProperties") or []

    if not classes:
        warnings.append("还没有类，提交前至少需要 1 个类")

    # 名称规则 + 全局唯一（三类共享同一 IRI 前缀，合并查重）
    seen: dict[str, str] = {}
    lower_seen: dict[str, str] = {}
    for kind, items in (("类", classes), ("对象属性", oprops), ("数据属性", dprops)):
        for item in items:
            name = (item.get("name") or "").strip()
            label = item.get("label") or ""
            if not name:
                errors.append(f"{kind} {item['id']} 未填名称")
            elif not NAME_RE.match(name):
                errors.append(f"{kind}「{name}」名称须以字母开头，仅含字母/数字/下划线（≤64 字符）；中文名请填到标签")
            elif name in seen:
                errors.append(f"名称「{name}」重复（{seen[name]} 与 {kind}）")
            else:
                seen[name] = kind
                if name.lower() in lower_seen and lower_seen[name.lower()] != name:
                    warnings.append(f"名称「{name}」与「{lower_seen[name.lower()]}」仅大小写不同，拓扑图可能混淆")
                else:
                    lower_seen.setdefault(name.lower(), name)
            if len(label) > 120:
                errors.append(f"{kind}「{name}」标签超过 120 字符")
            elif not label:
                warnings.append(f"{kind}「{name}」没填中文标签，拓扑图将显示英文名")
            if len(item.get("comment") or "") > 500:
                errors.append(f"{kind}「{name}」描述超过 500 字符")

    class_ids = {c["id"] for c in classes}

    # 类层级
    class_by_id = {c["id"]: c for c in classes}
    for c in classes:
        pid = c.get("parentId")
        if pid is None:
            continue
        if pid == c["id"]:
            errors.append(f"类「{c.get('name')}」的父类不能是自己")
        elif pid not in class_ids or pid not in class_by_id:
            errors.append(f"类「{c.get('name')}」的父类引用已失效")
    cyc = detect_cycle(classes)
    if cyc:
        errors.append(f"类「{class_by_id[cyc].get('name')}」的父类层级成环")

    # 类公理：等价/不相交（引用须存在且非自指）
    for c in classes:
        for field, label in (("equivalentIds", "等价类"), ("disjointIds", "不相交类")):
            for ref in c.get(field) or []:
                if ref == c["id"]:
                    errors.append(f"类「{c.get('name')}」的{label}不能是自己")
                elif ref not in class_ids:
                    errors.append(f"类「{c.get('name')}」的{label}引用已失效")

    # 对象属性：domain/range 必填且指向已定义类（Playground 对缺端点的关系直接丢弃）
    oprop_ids = {o["id"] for o in oprops}
    for op in oprops:
        name = op.get("name") or op["id"]
        for field, cname in (("domainId", "domain"), ("rangeId", "range")):
            v = op.get(field)
            if not v:
                errors.append(f"对象属性「{name}」未选 {cname}（必填）")
            elif v not in class_ids:
                errors.append(f"对象属性「{name}」的 {cname} 引用的类不存在")
        if op.get("inverseOfId"):
            if op["inverseOfId"] == op["id"]:
                errors.append(f"对象属性「{name}」的互逆属性不能是自己")
            elif op["inverseOfId"] not in oprop_ids:
                errors.append(f"对象属性「{name}」的互逆属性引用已失效")
        if op.get("subPropertyOfId"):
            if op["subPropertyOfId"] == op["id"]:
                errors.append(f"对象属性「{name}」的父属性不能是自己")
            elif op["subPropertyOfId"] not in oprop_ids:
                errors.append(f"对象属性「{name}」的父属性引用已失效")
    cyc_op = detect_cycle(oprops, "subPropertyOfId")
    if cyc_op:
        op_by_id = {o["id"]: o for o in oprops}
        errors.append(f"对象属性「{op_by_id[cyc_op].get('name')}」的父属性层级成环")

    # 数据属性：domain 可空（全局属性合法）、range 限 7 项
    dprop_ids = {p["id"] for p in dprops}
    for dp in dprops:
        name = dp.get("name") or dp["id"]
        if dp.get("domainId") and dp["domainId"] not in class_ids:
            errors.append(f"数据属性「{name}」的 domain 引用的类不存在")
        if dp.get("range") not in XSD_TYPES:
            errors.append(f"数据属性「{name}」的值类型无效")
        if dp.get("subPropertyOfId"):
            if dp["subPropertyOfId"] == dp["id"]:
                errors.append(f"数据属性「{name}」的父属性不能是自己")
            elif dp["subPropertyOfId"] not in dprop_ids:
                errors.append(f"数据属性「{name}」的父属性引用已失效")
    cyc_dp = detect_cycle(dprops, "subPropertyOfId")
    if cyc_dp:
        dp_by_id = {p["id"]: p for p in dprops}
        errors.append(f"数据属性「{dp_by_id[cyc_dp].get('name')}」的父属性层级成环")

    return errors, warnings


# ---------- RDF 生成 ----------

def build_rdf(draft: dict) -> tuple[str, int]:
    """草稿 → RDF/XML（rdflib pretty-xml，类型化元素形式，Playground parser 兼容）。"""
    ns = Namespace(draft["namespace"])
    g = Graph()
    g.bind("", ns)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)

    g.add((URIRef(str(ns).rstrip("#/")), RDF.type, OWL.Ontology))

    name_of = {c["id"]: c.get("name") for c in draft.get("classes") or []}
    oname_of = {o["id"]: o.get("name") for o in draft.get("objectProperties") or []}
    dname_of = {p["id"]: p.get("name") for p in draft.get("dataProperties") or []}

    # 对称公理（equivalentClass/disjointWith/inverseOf）双向声明只输出一条
    seen_pairs: set[tuple[str, str, str]] = set()

    def add_pair(sub, obj, pred):
        rkey = (str(obj), str(sub), str(pred))
        if rkey not in seen_pairs:
            seen_pairs.add((str(sub), str(obj), str(pred)))
            g.add((sub, pred, obj))

    for c in draft.get("classes") or []:
        iri = ns[c["name"]]
        g.add((iri, RDF.type, OWL.Class))
        if c.get("label"):
            g.add((iri, RDFS.label, Literal(c["label"], lang="zh")))
        if c.get("comment"):
            g.add((iri, RDFS.comment, Literal(c["comment"])))
        pid = c.get("parentId")
        if pid and pid in name_of and name_of[pid] and name_of[pid] != c["name"]:
            g.add((iri, RDFS.subClassOf, ns[name_of[pid]]))
        for eid in c.get("equivalentIds") or []:
            if eid in name_of and eid != c["id"] and name_of[eid]:
                add_pair(iri, ns[name_of[eid]], OWL.equivalentClass)
        for did in c.get("disjointIds") or []:
            if did in name_of and did != c["id"] and name_of[did]:
                add_pair(iri, ns[name_of[did]], OWL.disjointWith)

    for op in draft.get("objectProperties") or []:
        iri = ns[op["name"]]
        g.add((iri, RDF.type, OWL.ObjectProperty))
        if op.get("label"):
            g.add((iri, RDFS.label, Literal(op["label"], lang="zh")))
        if op.get("comment"):
            g.add((iri, RDFS.comment, Literal(op["comment"])))
        if op.get("domainId") in name_of:
            g.add((iri, RDFS.domain, ns[name_of[op["domainId"]]]))
        if op.get("rangeId") in name_of:
            g.add((iri, RDFS.range, ns[name_of[op["rangeId"]]]))
        inv = op.get("inverseOfId")
        if inv in oname_of and inv != op["id"] and oname_of[inv]:
            add_pair(iri, ns[oname_of[inv]], OWL.inverseOf)
        sup = op.get("subPropertyOfId")
        if sup in oname_of and sup != op["id"] and oname_of[sup]:
            g.add((iri, RDFS.subPropertyOf, ns[oname_of[sup]]))

    for dp in draft.get("dataProperties") or []:
        iri = ns[dp["name"]]
        g.add((iri, RDF.type, OWL.DatatypeProperty))
        if dp.get("label"):
            g.add((iri, RDFS.label, Literal(dp["label"], lang="zh")))
        if dp.get("comment"):
            g.add((iri, RDFS.comment, Literal(dp["comment"])))
        if dp.get("domainId") in name_of:
            g.add((iri, RDFS.domain, ns[name_of[dp["domainId"]]]))
        if dp.get("range") in XSD_TYPES:
            g.add((iri, RDFS.range, XSD_TYPES[dp["range"]]))
        sup = dp.get("subPropertyOfId")
        if sup in dname_of and sup != dp["id"] and dname_of[sup]:
            g.add((iri, RDFS.subPropertyOf, ns[dname_of[sup]]))

    return g.serialize(format="pretty-xml"), len(g)


# ---------- 导入解析 ----------

def majority_namespace(iris: list[str]) -> str | None:
    """类+属性 IRI 的命名空间取众数（Protege 导出常混入外来命名空间）。"""
    if not iris:
        return None
    return Counter(_namespace_of(i) for i in iris).most_common(1)[0][0]


def _rdf_list(g: Graph, head) -> list:
    """展开 RDF 链表（rdf:first/rdf:rest），head 为 None 返回 []。"""
    out, cur = [], head
    while cur is not None and cur != RDF.nil:
        first = g.value(cur, RDF.first)
        if first is None:
            break
        out.append(first)
        cur = g.value(cur, RDF.rest)
    return out


def parse_ontology_for_builder(rdf_text: str) -> dict:
    """已保存本体 RDF/XML → 草稿。只认四件套 + label，其余构造计入 warnings/skipped。

    Returns: {"draft": {...}, "warnings": [...], "skipped": {"axioms": int, "foreign": [...]}}
    Raises: ValueError（RDF 解析失败）
    """
    g = Graph()
    try:
        g.parse(data=rdf_text, format="xml")
    except Exception as e:
        raise ValueError(f"RDF 解析失败：{e}") from e

    warnings: list[str] = []

    def entities(rdf_type: URIRef) -> list[URIRef]:
        return sorted(
            {s for s in g.subjects(RDF.type, rdf_type) if isinstance(s, URIRef) and not _is_builtin(s)},
            key=str,
        )

    class_iris = entities(OWL.Class)
    oprop_iris = entities(OWL.ObjectProperty)
    dprop_iris = entities(OWL.DatatypeProperty)
    all_iris = [str(i) for i in class_iris + oprop_iris + dprop_iris]
    ns_str = majority_namespace(all_iris) or DEFAULT_NS

    # 外来命名空间的元素整体跳过并列名
    foreign = [i for i in all_iris if _namespace_of(i) != ns_str]
    foreign_set = set(foreign)
    if foreign:
        warnings.append(f"跳过外来命名空间的 {len(foreign)} 个元素：" + "、".join(_local_name(i) for i in foreign[:8]) + ("…" if len(foreign) > 8 else ""))

    kept_classes = [i for i in class_iris if str(i) not in foreign_set]
    kept_oprops = [i for i in oprop_iris if str(i) not in foreign_set]
    kept_dprops = [i for i in dprop_iris if str(i) not in foreign_set]
    class_iri_str = {str(i) for i in kept_classes}

    def first_label(ref) -> str:
        labels = sorted(g.objects(ref, RDFS.label), key=str)
        if len(labels) > 1:
            warnings.append(f"{_local_name(str(ref))} 有多个 label，取第一个")
        return str(labels[0]) if labels else ""

    def first_comment(ref) -> str:
        comments = sorted(g.objects(ref, RDFS.comment), key=str)
        return str(comments[0]) if comments else ""

    # unsupported 复杂公理计数（类表达式等，导入即丢；四类普通公理已支持）
    UNSUPPORTED = (OWL.unionOf, OWL.intersectionOf)
    n_axioms = sum(len(list(g.triples((None, p, None)))) for p in UNSUPPORTED)
    if n_axioms:
        warnings.append(f"丢弃 {n_axioms} 条不支持的复杂公理（unionOf/intersectionOf 等类表达式）")

    # 类（subClassOf 仅收指向已知类的 URIRef）
    classes, id_by_iri = [], {}
    for i, iri in enumerate(kept_classes, 1):
        cid = f"c{i}"
        id_by_iri[str(iri)] = cid
        classes.append({"id": cid, "name": _local_name(str(iri)), "label": first_label(iri),
                        "comment": first_comment(iri),
                        "parentId": None, "equivalentIds": [], "disjointIds": []})
    for iri, c in zip(kept_classes, classes):
        parents = [o for o in g.objects(iri, RDFS.subClassOf)]
        taken = None
        for p in parents:
            if isinstance(p, BNode):
                warnings.append(f"类「{c['name']}」的 subClassOf 指向匿名表达式（如限制），已忽略")
            elif str(p) in id_by_iri and str(p) != str(iri):
                taken = id_by_iri[str(p)]
            elif not _is_builtin(p):
                warnings.append(f"类「{c['name']}」的 subClassOf 指向未知类 {_local_name(str(p))}，已忽略")
        if len([p for p in parents if isinstance(p, URIRef) and str(p) in id_by_iri]) > 1:
            warnings.append(f"类「{c['name']}」有多个父类，取最后一个（单继承）")
        c["parentId"] = taken

        # 等价/不相交：双向收集（A eq B 与 B eq A 算同一条，去重）
        for pred, field, label in ((OWL.equivalentClass, "equivalentIds", "equivalentClass"),
                                   (OWL.disjointWith, "disjointIds", "disjointWith")):
            seen_t: set[str] = set()
            for t in list(g.objects(iri, pred)) + list(g.subjects(pred, iri)):
                key = str(t)
                if key == str(iri) or key in seen_t:
                    continue
                seen_t.add(key)
                if key in id_by_iri:
                    if id_by_iri[key] not in c[field]:
                        c[field].append(id_by_iri[key])
                elif isinstance(t, BNode):
                    warnings.append(f"类「{c['name']}」的 {label} 指向匿名表达式，已忽略")
                elif not _is_builtin(t):
                    warnings.append(f"类「{c['name']}」的 {label} 指向未知类 {_local_name(key)}，已忽略")

    # owl:AllDisjointClasses：members 两两拆成 disjointWith（双向存储，build 时 add_pair 去重）
    cls_by_id = {c["id"]: c for c in classes}
    for ax in g.subjects(RDF.type, OWL.AllDisjointClasses):
        members = _rdf_list(g, g.value(ax, OWL.members))
        known = [id_by_iri[str(m)] for m in members if isinstance(m, URIRef) and str(m) in id_by_iri]
        if len(members) - len(known):
            warnings.append(f"AllDisjointClasses 有 {len(members) - len(known)} 个未知成员已忽略")
        for a in range(len(known)):
            for b in range(a + 1, len(known)):
                if known[b] not in cls_by_id[known[a]]["disjointIds"]:
                    cls_by_id[known[a]]["disjointIds"].append(known[b])
                if known[a] not in cls_by_id[known[b]]["disjointIds"]:
                    cls_by_id[known[b]]["disjointIds"].append(known[a])

    # 对象属性
    oprops = []
    for i, iri in enumerate(kept_oprops, 1):
        domains = [d for d in g.objects(iri, RDFS.domain) if str(d) in class_iri_str]
        ranges = [r for r in g.objects(iri, RDFS.range) if str(r) in class_iri_str]
        if not domains:
            warnings.append(f"对象属性「{_local_name(str(iri))}」缺 domain（或指向非类），需补全后才能提交")
        if not ranges:
            warnings.append(f"对象属性「{_local_name(str(iri))}」缺 range（或指向非类），需补全后才能提交")
        oprops.append({
            "id": f"o{i}", "name": _local_name(str(iri)), "label": first_label(iri),
            "comment": first_comment(iri),
            "domainId": id_by_iri.get(str(domains[0])) if domains else None,
            "rangeId": id_by_iri.get(str(ranges[0])) if ranges else None,
            "inverseOfId": None, "subPropertyOfId": None,
        })

    # 互逆/父属性（取第一个，多值告知；指向属性链/未知属性则丢并告知）
    op_id_by_iri = {str(iri): o["id"] for iri, o in zip(kept_oprops, oprops)}

    def take_single(item: dict, iri, pred, field, label):
        vals = list(g.objects(iri, pred))
        if pred == OWL.inverseOf:  # 互逆常双向各声明一次，合并收集
            vals += [s for s in g.subjects(pred, iri) if str(s) != str(iri)]
        seen_v: set[str] = set()
        for v in vals:
            key = str(v)
            if key == str(iri) or key in seen_v:
                continue
            seen_v.add(key)
            if key in op_id_by_iri:
                if not item[field]:
                    item[field] = op_id_by_iri[key]
                else:
                    warnings.append(f"对象属性「{item['name']}」有多个 {label}，取第一个")
            elif isinstance(v, BNode):
                warnings.append(f"对象属性「{item['name']}」的 {label} 指向属性链，已忽略")
            elif not _is_builtin(v):
                warnings.append(f"对象属性「{item['name']}」的 {label} 指向未知属性 {_local_name(key)}，已忽略")

    for iri, o in zip(kept_oprops, oprops):
        take_single(o, iri, OWL.inverseOf, "inverseOfId", "inverseOf")
        take_single(o, iri, RDFS.subPropertyOf, "subPropertyOfId", "subPropertyOf")

    # 数据属性（domain 可空；xsd 映射表外降级 string）
    dprops = []
    for i, iri in enumerate(kept_dprops, 1):
        name = _local_name(str(iri))
        domains = [d for d in g.objects(iri, RDFS.domain) if str(d) in class_iri_str]
        range_local = ""
        for r in g.objects(iri, RDFS.range):
            range_local = _local_name(str(r))
        if range_local and range_local not in IMPORT_XSD_MAP:
            warnings.append(f"数据属性「{name}」的 xsd:{range_local} 不在支持列表，降级为 string")
            range_local = "string"
        if not range_local:
            range_local = "string"
        dprops.append({
            "id": f"d{i}", "name": name, "label": first_label(iri),
            "comment": first_comment(iri),
            "domainId": id_by_iri.get(str(domains[0])) if domains else None,
            "range": range_local,
            "subPropertyOfId": None,
        })

    # 数据属性的父属性（限同类数据属性）
    dp_id_by_iri = {str(iri): p["id"] for iri, p in zip(kept_dprops, dprops)}
    for iri, p in zip(kept_dprops, dprops):
        for sup in g.objects(iri, RDFS.subPropertyOf):
            key = str(sup)
            if key == str(iri):
                continue
            if key in dp_id_by_iri:
                if not p["subPropertyOfId"]:
                    p["subPropertyOfId"] = dp_id_by_iri[key]
                else:
                    warnings.append(f"数据属性「{p['name']}」有多个 subPropertyOf，取第一个")
            elif isinstance(sup, BNode):
                warnings.append(f"数据属性「{p['name']}」的 subPropertyOf 指向属性链，已忽略")
            elif not _is_builtin(sup):
                warnings.append(f"数据属性「{p['name']}」的 subPropertyOf 指向未知属性 {_local_name(key)}，已忽略")

    draft = {
        "version": DRAFT_VERSION,
        "namespace": ns_str,
        "classes": classes,
        "objectProperties": oprops,
        "dataProperties": dprops,
    }
    for item in classes + oprops + dprops:
        if not NAME_RE.match(item["name"]):
            warnings.append(f"「{item['name']}」名称不符合规则（字母开头，字母/数字/下划线），请改名后提交")

    return {"draft": draft, "warnings": warnings, "skipped": {"axioms": n_axioms, "foreign": foreign}}
