"""本体路径库构建：Text2Sparql 工作流「路径库与本体元数据」节点的平台移植。

从本体 RDF 无向枚举全部简单路径（防环），逆向跳以 ^ 标注（两个方向形态都入库），
跳数上限 max_hops（1~8，默认 4）与条数上限 max_patterns（50~2000，默认 300）可调。
产出供两阶段问答使用：Stage1 拿 patterns_r1（1 跳菜单+字面量）做词汇识别，
Stage2 拿过滤后的 paths_full 当唯一允许的谓词来源。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator

import rdflib
from rdflib.namespace import OWL, RDF, RDFS

XSD = "http://www.w3.org/2001/XMLSchema#"

MAX_HOPS_DEFAULT = 4
MAX_HOPS_MAX = 8
MAX_PATTERNS_DEFAULT = 300

_NUMERIC = ("int", "integer", "long", "short", "decimal", "double", "float",
            "nonnegativeinteger", "positiveinteger", "negativeinteger",
            "nonpositiveinteger")


def _local(iri: str) -> str:
    if "#" in iri:
        return iri.rsplit("#", 1)[1]
    if "/" in iri:
        return iri.rsplit("/", 1)[1]
    return iri


def _ns_of(iri: str) -> str:
    if "#" in iri:
        return iri.rsplit("#", 1)[0] + "#"
    if "/" in iri:
        return iri.rsplit("/", 1)[0] + "/"
    return ""


def clamp_hops(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return MAX_HOPS_DEFAULT
    return max(1, min(MAX_HOPS_MAX, n))


def clamp_patterns(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return MAX_PATTERNS_DEFAULT
    return max(50, min(2000, n))


class PathLibraryError(ValueError):
    """本体不可用或抽不出任何对象边。"""


def build_path_library(rdf_xml: str, max_hops: int | None = None,
                       max_patterns: int | None = None) -> dict:
    """本体 RDF/XML → 路径库。字段与工作流 code 节点输出一一对应。"""
    max_r = clamp_hops(max_hops)
    max_p = clamp_patterns(max_patterns)
    text = (rdf_xml or "").strip()
    if not text:
        raise PathLibraryError("本体为空，无法构建路径库")

    g = rdflib.Graph()
    try:
        g.parse(data=text, format="xml")
    except Exception as e:
        raise PathLibraryError(f"本体解析失败：{e}") from e

    sys_ns = {str(RDF), str(RDFS), str(OWL), XSD}
    classes: dict[str, str] = {}        # IRI → local
    labels: dict[str, str] = {}
    obj_props: dict[str, dict] = {}     # IRI → {domain, range, label}
    dt_props: dict[str, dict] = {}
    subclass: list[tuple[str, str]] = []

    for s in set(g.subjects(RDF.type, OWL.Class)) | set(g.subjects(RDF.type, RDFS.Class)):
        iri = str(s)
        classes[iri] = _local(iri)

    for pred_map, ptype in ((obj_props, OWL.ObjectProperty), (dt_props, OWL.DatatypeProperty)):
        for p in g.subjects(RDF.type, ptype):
            iri = str(p)
            dom = rng = lbl = None
            for o in g.objects(p, RDFS.domain):
                dom = str(o)
                break
            for o in g.objects(p, RDFS.range):
                rng = str(o)
                break
            for o in g.objects(p, RDFS.label):
                lbl = str(o)
                break
            pred_map[iri] = {"domain": dom, "range": rng, "label": lbl or ""}
            if lbl:
                labels.setdefault(iri, lbl)

    for s, o in g.subject_objects(RDFS.subClassOf):
        sub, sup = str(s), str(o)
        if sub in classes or sub in obj_props or sub in dt_props:
            subclass.append((sub, sup))
            classes.setdefault(sub, _local(sub))
            if sup not in sys_ns:
                classes.setdefault(sup, _local(sup))

    # domain/range 指到的未声明实体补成类（跟工作流 ensure 一致）
    def ensure(iri: str | None) -> None:
        if iri and iri not in sys_ns and not iri.startswith(XSD) and iri not in classes:
            classes[iri] = _local(iri)

    for p in list(obj_props.values()) + list(dt_props.values()):
        ensure(p["domain"])
        if p["range"] and not p["range"].startswith(XSD):
            ensure(p["range"])

    # ---- 组装边 / 数据属性 / 共享属性 ----
    edges: list[tuple[str, str, str]] = []
    data_props: dict[str, list[tuple[str, str]]] = defaultdict(list)
    shared: list[tuple[str, str]] = []
    dt_range: dict[str, str] = {}
    warnings: list[str] = []

    for iri, p in list(obj_props.items()) + list(dt_props.items()):
        pl = _local(iri)
        d = classes.get(p["domain"]) if p["domain"] else None
        lit = (not p["range"]) or p["range"].startswith(XSD)
        if iri in obj_props and not lit:
            r = classes.get(p["range"])
            if d and r:
                edges.append((d, pl, r))
            else:
                warnings.append(f"对象属性 {pl} 缺 domain 或 range，未纳入路径库")
        else:
            lbl = p["label"] or pl
            if d:
                data_props[d].append((pl, lbl))
            else:
                shared.append((pl, lbl))
            dt_range[pl] = p["range"] or ""

    if not edges:
        raise PathLibraryError(
            "本体解析成功但没有任何可用对象边（对象属性全部缺 domain/range？），无法构建路径库")

    class_labels = {local: (labels.get(iri) or local) for iri, local in classes.items()}
    prop_labels = {_local(iri): (p["label"] or _local(iri))
                   for iri, p in list(obj_props.items()) + list(dt_props.items())}

    # ---- 无向枚举简单路径（防环），逆向跳 ^ 标注 ----
    adj: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    for s, p, o in edges:
        adj[s].append((p, o, True))    # 顺向：domain→range
        adj[o].append((p, s, False))   # 逆向：range→domain

    def _fmt(chain_c: list[str], chain_p: list[str], chain_fwd: list[bool]) -> str:
        out = "{" + chain_c[0] + "}"
        for p, c, fwd in zip(chain_p, chain_c[1:], chain_fwd):
            mark = "" if fwd else "^"
            out += " ->" + mark + p + "-> {" + c + "}"
        return out

    seen: set[str] = set()
    obj_lines: list[str] = []

    def extend(chain_c: list[str], chain_p: list[str], chain_fwd: list[bool]) -> None:
        cur = _fmt(chain_c, chain_p, chain_fwd)
        if cur not in seen:
            seen.add(cur)
            obj_lines.append(cur)
        if len(chain_p) < max_r:
            for p, o, fwd in adj.get(chain_c[-1], []):
                if o in chain_c:  # 简单路径防环
                    continue
                extend(chain_c + [o], chain_p + [p], chain_fwd + [fwd])

    graph_classes: list[str] = []
    for s, _, o in edges:
        for c in (s, o):
            if c not in graph_classes:
                graph_classes.append(c)
    for c in data_props:
        if c not in graph_classes:
            graph_classes.append(c)
    for c in graph_classes:
        extend([c], [], [])

    obj_lines = [l for l in obj_lines if l.count("->") >= 2]  # ≥1 跳
    obj_lines.sort(key=lambda l: (l.count("->") // 2, l))
    if len(obj_lines) > max_p:
        warnings.append(f"路径数超上限，已截断至 {max_p} 条（当前跳数上限 {max_r}）")
        obj_lines = obj_lines[:max_p]

    # 预测菜单：顺向 1 跳 + 字面量模式；自环边附写法提示
    r1_forward = []
    for s, p, o in edges:
        line = "{" + s + "} ->" + p + "-> {" + o + "}"
        if s == o:
            line += "  # 自环边：主宾同类，直接写 ?x " + p + " ?y"
        r1_forward.append(line)

    def _lit_tag(pl: str) -> str:
        t = (dt_range.get(pl) or "").rsplit("#", 1)[-1].rsplit("/", 1)[-1].lower()
        if t in _NUMERIC:
            return "(Literal数值)"
        return "(Literal字符串)" if t else "(Literal)"

    lit_lines = []
    for c in graph_classes:
        for pl, _lbl in data_props.get(c, []):
            lit_lines.append("{" + c + "} ->" + pl + "-> " + _lit_tag(pl))
    for pl, _lbl in shared:
        for c in graph_classes:
            lit_lines.append("{" + c + "} ->" + pl + "-> " + _lit_tag(pl))

    patterns_r1 = "\n".join(r1_forward + lit_lines)
    paths_full = "\n".join(
        sorted(set(obj_lines) | set(r1_forward), key=lambda l: (l.count("->") // 2, l))
        + lit_lines)

    label_pairs = [c + "=" + class_labels.get(c, c) for c in graph_classes]
    for pl in prop_labels:
        label_pairs.append(pl + "=" + prop_labels[pl])

    ns_counter: Counter[str] = Counter()
    for iri in list(classes) + list(obj_props) + list(dt_props):
        ns_counter[_ns_of(iri)] += 1
    ns = ns_counter.most_common(1)[0][0] if ns_counter else ""

    meta: dict = {"_subclass": {}}
    for child, parent in subclass:
        c_local, p_local = classes.get(child), classes.get(parent)
        if not c_local or not p_local:
            continue
        meta["_subclass"].setdefault(c_local, [])
        if p_local not in meta["_subclass"][c_local]:
            meta["_subclass"][c_local].append(p_local)
    for c in graph_classes:
        items = list(data_props.get(c, [])) + [(pl, prop_labels.get(pl, pl)) for pl, _l in shared]
        txt = ", ".join(pl + "(" + lbl + ")" for pl, lbl in items) if items else "(无专属数据属性)"
        meta[c] = {"data_props": txt,
                   "iri": ns + c + "/<个体键>（样例为推测，以映射为准）"}

    return {
        "patterns_r1": patterns_r1,
        "paths_full": paths_full,
        "labels": "; ".join(label_pairs),
        "vocab": _dumps({"classes": class_labels, "props": prop_labels}),
        "class_meta": _dumps(meta),
        "path_prefix": f"PREFIX : <{ns}>" if ns else "",
        "warnings": warnings,
    }


def _dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
