"""LLM 自然语言问答：Dify 工作流的搬运工。

管线：question → LLM#1 生成 SPARQL → Ontop 执行 → LLM#2 CSV→自然语言。
提示词从 Dify 版本通用化：本体结构不再硬编码火电，从当前项目本体 rdflib 抽出摘要。
仅支持 DeepSeek（OpenAI 兼容 chat completions），api_key 明面传。
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
import rdflib
from rdflib.namespace import OWL, RDF, RDFS

from backend.services import sparql_proxy
from backend.services.project_store import get_store

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


class LLMError(Exception):
    pass


# ---------- 本体摘要 ----------

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


# ---------- 主管线 ----------

_SYS_NL2SPARQL = """你是 SPARQL 生成器，根据下方本体结构和用户问题生成一条 SPARQL 查询。
只输出 JSON：{{"sparql": "..."}}，不要输出任何多余内容。

生成规则：
0. 查询第一行必须是：PREFIX : <{ns}>
1. 给人看的字段必须选数据属性，禁止把个体 IRI 当答案返回
2. 问数量用 SELECT (COUNT(DISTINCT ?x) AS ?n)
3. 只能使用下方列出的类和属性，不得发明
4. 多跳问题逐层拆解，每一层"的"字关系对应一个独立三元组模式
5. 字符串常量必须带引号，数值条件用 FILTER（如 FILTER(?s > 20000)）
6. 术语映射：属性/类后面的（中文名）：描述是业务含义——用户问题里的词
   必须按中文名精确对应属性，注意区分相近词（如"工单描述"≠"故障描述"≠"关联故障"）
7. "没有/不存在 XX"的否定问题用 FILTER NOT EXISTS

本体结构：
类：{classes}
对象属性：{obj_props}
数据属性：{dt_props}"""

_SYS_ANSWER = """你是问答助手，根据「用户问题」和「查询结果」用中文回答。规则：
- 答案形状必须和问题一致（问数量只答数字，问名单才列名单）
- 查询结果只有表头或为空就回答"没有查到相关数据"
- 不得编造结果里没有的信息
- 不要向用户提及 SPARQL 等技术细节"""


async def ask(question: str, api_key: str, route: str = "virtual") -> dict[str, Any]:
    """两跳问答；返回中间产物+最终答，方便调试。route=virtual|materialized。"""
    store = get_store()
    if not store.ontology_path.exists():
        raise LLMError("请先上传本体（当前项目无 ontology.rdf）")
    if route == "materialized" and not store.snapshot().get("abox"):
        raise LLMError("物化路线未准备：请先在主页点击③生成 ABox")
    rdf_xml = store.ontology_path.read_text(encoding="utf-8")
    summary = summarize_ontology(rdf_xml)

    # 跳 1：NL→SPARQL
    t0 = time.time()
    sys_prompt = _SYS_NL2SPARQL.format(
        ns=summary["ns"],
        classes="、".join(summary["classes"]) or "（无）",
        obj_props="\n".join(f"- {r}" for r in summary["obj_props"]) or "- 无",
        dt_props="\n".join(f"- {r}" for r in summary["dt_props"]) or "- 无",
    )
    raw = await _deepseek_chat(
        [{"role": "system", "content": sys_prompt}, {"role": "user", "content": question}],
        api_key,
        response_format={"type": "json_object"},
    )
    try:
        sparql = json.loads(raw)["sparql"]
    except Exception as e:
        raise LLMError(f"LLM 生成 SPARQL 解析失败：{e}；原文：{raw[:400]}")
    gen_ms = int((time.time() - t0) * 1000)

    # 跳 2：跑 SPARQL 拿 CSV
    t1 = time.time()
    try:
        resp, entry_id = await sparql_proxy.forward(sparql, "text/csv", route=route, meta={"question": question})
    except sparql_proxy.EndpointUnavailable as e:
        raise LLMError(f"{e}；无法执行生成的查询：\n{sparql}")
    except httpx.TimeoutException:
        raise LLMError(f"SPARQL 超时；生成的查询：\n{sparql}")
    if resp.status_code != 200:
        endpoint = "QLever" if route == "materialized" else "Ontop"
        raise LLMError(f"{endpoint} 返回 {resp.status_code}：{resp.text[:400]}；SPARQL：\n{sparql}")
    csv_text = resp.text
    sparql_ms = int((time.time() - t1) * 1000)

    # 跳 3：CSV→自然语言
    t2 = time.time()
    answer = await _deepseek_chat(
        [
            {"role": "system", "content": _SYS_ANSWER},
            {"role": "user", "content": f"用户问题：{question}\n\n查询结果（CSV，第一行是表头）：\n{csv_text}"},
        ],
        api_key,
        temperature=0.7,
    )
    ans_ms = int((time.time() - t2) * 1000)

    sparql_proxy.update_entry(entry_id, answer=answer.strip())

    return {
        "question": question,
        "route": route,
        "sparql": sparql,
        "csv": csv_text,
        "answer": answer.strip(),
        "timings": {"gen_ms": gen_ms, "sparql_ms": sparql_ms, "answer_ms": ans_ms},
    }
