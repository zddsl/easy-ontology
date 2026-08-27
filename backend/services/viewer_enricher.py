"""视图增强器：从映射 .obda 反推对象属性 rdfs:range，只喂给拓扑图看。

原理（胖映射天然自带线索）：
- 类模板：  :loc/{LOCATION}-{SITEID} a :Locations   → 前缀 :loc/ 铸 Locations 的个体
- 边断言：  :occursAt :loc/{LOCATION}-{SITEID}      → 宾语模板前缀 :loc/ ⇒ range=Locations
Ontop 与 /sparql 永远拿原始本体（无 range，无幽灵无 UNION 支路）；只有图拿增强副本。
"""
from __future__ import annotations

import re

import rdflib
from rdflib import RDFS, Literal, URIRef
from rdflib.namespace import OWL, RDF

# 类节点调色板：Tableau 10 前 8 色（对色盲友好，饱和度均衡）
_PALETTE = [
    "#4E79A7", "#F28E2B", "#59A14F", "#E15759",
    "#B07AA1", "#EDC948", "#76B7B2", "#FF9DA7",
]
_PG_COLOR = URIRef("http://easy-ontology.local/pg#color")


def _prefix_map(obda_text: str) -> dict[str, str]:
    """[PrefixDeclaration] 段 → {':' : 'http://...', 'xsd' : '...'}（键不含冒号）。"""
    pm: dict[str, str] = {}
    m = re.search(r"\[PrefixDeclaration\](.*?)(\[\w+\]|\Z)", obda_text, re.S)
    if not m:
        return pm
    for line in m.group(1).splitlines():
        parts = line.split()
        if len(parts) == 2 and not parts[0].startswith("#"):
            pm[parts[0].rstrip(":")] = parts[1]
    return pm


def _expand(prefixed: str, pm: dict[str, str]) -> str | None:
    """:Locations / xsd:string → 完整 IRI（pm 键已去冒号，如 ''、'xsd'）。"""
    pfx, _, local = prefixed.partition(":")
    base = pm.get(pfx)
    if not base:
        return None
    return base + local


def derive_ranges(obda_text: str) -> dict[str, str]:
    """返回 {对象属性完整IRI: range类完整IRI}。"""
    pm = _prefix_map(obda_text)

    # 1) 类模板归属：PREFIXED/{...} a :Class → 该前缀铸哪个类
    #    模板体可复合（{WONUM}-{SITEID}），用 \S*? 跨占位符直到 " a "
    template_owner: dict[str, str] = {}
    for m in re.finditer(r"([\w:]+)/\{\S*?\}\s+a\s+([\w:]+)", obda_text):
        template_owner[m.group(1)] = m.group(2)

    # 2) 边断言：:prop PREFIXED/{...}（宾语是模板而非字面量）
    candidates: set[tuple[str, str]] = set()
    for m in re.finditer(r"([\w:]+)\s+([\w:]+)/\{", obda_text):
        candidates.add((m.group(1), m.group(2)))

    derived: dict[str, str] = {}
    for prop_prefixed, tmpl in candidates:
        cls_prefixed = template_owner.get(tmpl)
        if not cls_prefixed:
            continue
        prop_iri, cls_iri = _expand(prop_prefixed, pm), _expand(cls_prefixed, pm)
        if prop_iri and cls_iri and prop_iri != cls_iri:
            derived[prop_iri] = cls_iri
    return derived


def derive_domains(obda_text: str) -> dict[str, str]:
    """从胖映射 target 行反推 {数据属性IRI: domain类IRI}。

    规则：`<tmpl> a :Class ; :prop "字面量" ...` —— 跟在类断言后的字面量属性，
    domain 即该类（本体当初为防幽灵个体删过 domain，这里只给图补，Ontop 拿原版）。
    同一属性出现在多个类时取首个（setdefault），仅作可视化归属。
    """
    pm = _prefix_map(obda_text)
    domains: dict[str, str] = {}
    for m in re.finditer(r"([\w:]+)/\{\S*?\}\s+a\s+([\w:]+)([^\n]*)", obda_text):
        cls_iri = _expand(m.group(2), pm)
        if not cls_iri:
            continue
        for seg in m.group(3).split(";"):
            lit = re.match(r"\s*([\w:]+)\s+\"", seg)  # 值以引号开头 = 数据属性
            if not lit:
                continue
            prop_iri = _expand(lit.group(1), pm)
            if prop_iri and prop_iri != cls_iri:
                domains.setdefault(prop_iri, cls_iri)
    return domains


def enrich(ontology_rdf_xml: str, obda_text: str) -> str:
    """给缺 range 的对象属性补上从映射推出的 range，返回增强 RDF/XML。"""
    g = rdflib.Graph()
    g.parse(data=ontology_rdf_xml, format="xml")

    obj_props = {p for p in g.subjects(RDF.type, OWL.ObjectProperty)}
    added = []
    for prop_iri, cls_iri in derive_ranges(obda_text).items():
        prop = URIRef(prop_iri)
        if prop not in obj_props:
            continue
        if (prop, RDFS.range, None) in g:
            continue  # 已有 range（如本体没删过），不覆盖
        g.add((prop, RDFS.range, URIRef(cls_iri)))
        added.append(f"{prop.n3(g.namespace_manager)} → {URIRef(cls_iri).n3(g.namespace_manager)}")

    if added:
        print(f"[viewer_enricher] 补 range {len(added)} 条：{'; '.join(added)}", flush=True)

    # 数据属性 domain 反推（同理：本体删过 domain 时 Inspector 会显示不出属性）
    dt_props = {p for p in g.subjects(RDF.type, OWL.DatatypeProperty)}
    added_d = []
    for prop_iri, cls_iri in derive_domains(obda_text).items():
        prop = URIRef(prop_iri)
        if prop not in dt_props:
            continue
        if (prop, RDFS.domain, None) in g:
            continue
        g.add((prop, RDFS.domain, URIRef(cls_iri)))
        added_d.append(f"{prop.n3(g.namespace_manager)} → {URIRef(cls_iri).n3(g.namespace_manager)}")

    if added_d:
        print(f"[viewer_enricher] 补 domain {len(added_d)} 条：{'; '.join(added_d)}", flush=True)

    # 给每个 OWL Class 注入 <color>，让 Playground 的 parser 按类分配节点颜色。
    # 按类 IRI 排序取稳定索引，同一本体每次刷新颜色一致；本体自带 <color> 时不覆盖。
    classes = sorted(g.subjects(RDF.type, OWL.Class), key=str)
    colored = 0
    for i, cls in enumerate(classes):
        if (cls, _PG_COLOR, None) in g:
            continue
        g.add((cls, _PG_COLOR, Literal(_PALETTE[i % len(_PALETTE)])))
        colored += 1
    if colored:
        print(f"[viewer_enricher] 上色 {colored} 类", flush=True)

    # pretty-xml 输出类型化元素（<owl:Class rdf:about>），普通 xml 是 <rdf:Description> 通用形式，
    # Playground 的解析器只认前者
    return g.serialize(format="pretty-xml")
