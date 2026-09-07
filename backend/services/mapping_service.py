"""映射构建器纯逻辑：本体元素解析 / 草稿规整校验 / .obda 生成（无 IO，便于独立测试）。

映射草稿（draft）以本体元素 IRI 为键（name 只是显示缓存，生成时以草稿 name 为准）：
  {"version": 1, "namespace": "…#",
   "classes":   [{"iri","name","label","table","pk":[列,…],"iri_path","iri_sep",
                  "props":[{"iri","name","col"}]}],
   "relations": [{"iri","name","domainIri","rangeIri","mode","table",
                  "domain_cols":[…], "range_cols":[…]}],
   "edited_obda": None, "updated_at": "…"}

mode 由表选择自动推导（不手选）；三种模式的边一律**独立成条 + INNER JOIN 对端表**（默认验货：
外键对不上对端行的边直接不生成，防脏外键假边/幽灵个体；类映射保持纯净不 join——否则脏外键行
会把个体连同属性一起滤掉）：
  domain_fk —— FK 在 domain 类的表：s=domain 表，join range 表 ON fk=pk
  range_fk  —— FK 在 range  类的表：s=range 表，join domain 表 ON fk=pk（range 端 IRI 用 B 自己的主键）
  junction  —— 第三张表（多对多中间表）：s=中间表，双亲各 join 一次
多列主键/外键按数组顺序拼接 IRI（:emp/{site_id}-{id}）；边对象 IRI 的分隔符用**被引用类自己的 iri_sep**，
与其类映射主语模板保持一致，否则同一实体两个 IRI、边查询断链。
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import Counter

from rdflib import Graph, OWL, RDF, RDFS, URIRef

from backend.services.builder_service import IMPORT_XSD_MAP, _local_name, _namespace_of

DRAFT_VERSION = 1
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # 可直接拼进 SQL 的标识符

# java.sql.Types 整数 → 本体 range 家族（string/integer/decimal/double/date/datetime/boolean）
JDBC_TYPE_CATEGORY = {
    1: "string", 12: "string", -1: "string", -15: "string", -9: "string", 2005: "string", 2011: "string",
    -6: "integer", 5: "integer", 4: "integer", -5: "integer",
    2: "decimal", 3: "decimal",
    7: "double", 6: "double", 8: "double",
    91: "date",
    93: "datetime", -101: "datetime", -102: "datetime",
    -7: "boolean", 16: "boolean",
}


class MappingDraftError(Exception):
    """草稿结构损坏（不是内容问题，是连解析都过不了）"""


# ---------- 本体解析（映射视角的元素清单） ----------

def parse_ontology_for_mapping(rdf_text: str) -> dict:
    """已保存本体 → {namespace, digest, classes, objectProperties, dataProperties}。"""
    g = Graph()
    g.parse(data=rdf_text, format="xml")
    all_ns: Counter = Counter()

    def _label(iri) -> str:
        v = g.value(iri, RDFS.label)
        return str(v) if v is not None else ""

    classes: list[dict] = []
    cls_iris: set[str] = set()
    for s in g.subjects(RDF.type, OWL.Class):
        if not isinstance(s, URIRef) or str(s).startswith((str(OWL), str(RDFS), str(RDF))):
            continue
        parent = g.value(s, RDFS.subClassOf)
        parent_iri = str(parent) if isinstance(parent, URIRef) and not str(parent).startswith(str(OWL)) else None
        classes.append({"iri": str(s), "name": _local_name(str(s)), "label": _label(s), "parentIri": parent_iri})
        cls_iris.add(str(s))
        all_ns[_namespace_of(str(s))] += 1

    def _endpoints(iri) -> tuple[str | None, str | None]:
        d = g.value(iri, RDFS.domain)
        r = g.value(iri, RDFS.range)
        ds = str(d) if isinstance(d, URIRef) and str(d) in cls_iris else None
        rs = str(r) if isinstance(r, URIRef) and str(r) in cls_iris else None
        return ds, rs

    oprops: list[dict] = []
    for s in g.subjects(RDF.type, OWL.ObjectProperty):
        if not isinstance(s, URIRef) or str(s).startswith(str(OWL)):
            continue
        d, r = _endpoints(s)
        inv = None
        for t in list(g.objects(s, OWL.inverseOf)) + list(g.subjects(OWL.inverseOf, s)):
            if isinstance(t, URIRef):
                inv = str(t)
                break
        oprops.append({"iri": str(s), "name": _local_name(str(s)), "label": _label(s),
                       "domainIri": d, "rangeIri": r, "inverseOfIri": inv})
        all_ns[_namespace_of(str(s))] += 1

    dprops: list[dict] = []
    for s in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not isinstance(s, URIRef) or str(s).startswith(str(OWL)):
            continue
        d, _ = _endpoints(s)
        rng = "string"
        rv = g.value(s, RDFS.range)
        if isinstance(rv, URIRef) and str(rv).startswith("http://www.w3.org/2001/XMLSchema#"):
            rng = IMPORT_XSD_MAP.get(_local_name(str(rv)), "string")
        dprops.append({"iri": str(s), "name": _local_name(str(s)), "label": _label(s),
                       "domainIri": d, "range": rng})
        all_ns[_namespace_of(str(s))] += 1

    classes.sort(key=lambda c: c["name"])
    oprops.sort(key=lambda p: p["name"])
    dprops.sort(key=lambda p: p["name"])
    return {
        "namespace": all_ns.most_common(1)[0][0] if all_ns else "",
        "digest": hashlib.md5(rdf_text.encode("utf-8")).hexdigest(),
        "classes": classes,
        "objectProperties": oprops,
        "dataProperties": dprops,
    }


# ---------- 草稿规整 ----------

def normalize_mapping_draft(raw: dict) -> dict:
    """外部草稿做结构规整，损坏抛 MappingDraftError（软字段缺失给默认值）。"""
    try:
        draft = {
            "version": int(raw.get("version", DRAFT_VERSION)),
            "namespace": str(raw.get("namespace") or "").strip(),
            "classes": [dict(c) for c in (raw.get("classes") or [])],
            "relations": [dict(r) for r in (raw.get("relations") or [])],
            "edited_obda": raw.get("edited_obda") if isinstance(raw.get("edited_obda"), str) else None,
            "updated_at": str(raw.get("updated_at") or ""),
        }
    except (TypeError, ValueError) as e:
        raise MappingDraftError(f"草稿结构不合法：{e}") from e
    for c in draft["classes"]:
        if not isinstance(c, dict) or not c.get("iri"):
            raise MappingDraftError("classes 里存在无 iri 的条目")
        c["table"] = c.get("table") or None
        c["pk"] = [str(x) for x in (c.get("pk") or [])]
        c["iri_path"] = str(c.get("iri_path") or "").strip()
        c["iri_sep"] = str(c.get("iri_sep") or "-")
        c["props"] = [{"iri": p.get("iri"), "name": p.get("name"), "col": p.get("col")}
                      for p in (c.get("props") or []) if isinstance(p, dict) and p.get("iri")]
        if not isinstance(c.get("name"), str) or not c["name"]:
            c["name"] = _local_name(c["iri"])
    for r in draft["relations"]:
        if not isinstance(r, dict) or not r.get("iri"):
            raise MappingDraftError("relations 里存在无 iri 的条目")
        r["table"] = r.get("table") or None
        r["mode"] = r.get("mode") or None
        r["domain_cols"] = [str(x) for x in (r.get("domain_cols") or [])]
        r["range_cols"] = [str(x) for x in (r.get("range_cols") or [])]
        if not r.get("name"):
            r["name"] = _local_name(r["iri"])
    return draft


def draft_counts(draft: dict) -> dict:
    return {
        "classes_mapped": sum(1 for c in draft["classes"] if c.get("table") and c.get("pk")),
        "relations_mapped": sum(1 for r in draft["relations"] if r.get("table") and r.get("domain_cols")),
    }


def diff_draft_vs_ontology(draft: dict, elements: dict) -> list[str]:
    """草稿引用的本体元素已不存在/端点变化 → 提示列表（进页黄条，不硬拦）。"""
    cls_iris = {c["iri"] for c in elements["classes"]}
    dp_iris = {p["iri"] for p in elements["dataProperties"]}
    op = {p["iri"]: p for p in elements["objectProperties"]}
    out: list[str] = []
    for c in draft["classes"]:
        if c["iri"] not in cls_iris:
            out.append(f"类 {c.get('name') or c['iri']} 已不存在于当前本体")
        else:
            for p in c.get("props") or []:
                if p["iri"] not in dp_iris:
                    out.append(f"数据属性 {p.get('name') or p['iri']} 已不存在于当前本体")
    for r in draft["relations"]:
        e = op.get(r["iri"])
        if e is None:
            out.append(f"对象属性 {r.get('name') or r['iri']} 已不存在于当前本体")
        elif (r.get("domainIri"), r.get("rangeIri")) != (e["domainIri"], e["rangeIri"]):
            out.append(f"对象属性 {e['name']} 的 domain/range 与当前本体不一致")
    return out


# ---------- 校验 ----------

def _schema_columns(schema: dict | None, table: str | None) -> list[dict] | None:
    """schema（/api/mapping/schema 的 tables）里某表的列；schema 缺失/表不在 → None（跳过列级检查）。"""
    if not schema or not table:
        return None
    for t in schema.get("tables") or []:
        if t.get("name") == table:
            return t.get("columns") or []
    return None


def validate_draft(draft: dict, elements: dict, schema: dict | None = None) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。errors 仅结构性问题（commit/generate 422）；其余全软。"""
    errors: list[str] = []
    warnings: list[str] = []
    if not any(c.get("table") and c.get("pk") for c in draft["classes"]):
        errors.append("至少为一个类配置数据表和主键，才能生成映射")
        return errors, warnings

    cls_by_iri = {c["iri"]: c for c in draft["classes"]}
    elem_cls = {c["iri"]: c for c in elements["classes"]}
    elem_dp = {p["iri"]: p for p in elements["dataProperties"]}
    elem_op = {p["iri"]: p for p in elements["objectProperties"]}

    # 类侧
    seen_sig: dict[tuple, str] = {}
    parent_of = {c["iri"]: c.get("parentIri") for c in elements["classes"]}
    elem_name = {c["iri"]: c["name"] for c in elements["classes"]}

    def _ancestors(iri: str) -> set[str]:
        out, cur = set(), parent_of.get(iri)
        while cur and cur not in out:
            out.add(cur)
            cur = parent_of.get(cur)
        return out

    for c in draft["classes"]:
        if c["iri"] not in elem_cls and (c.get("table") or c.get("pk")):
            warnings.append(f"草稿里的类 {c['name']} 在当前本体中不存在，已跳过")
    for r in draft["relations"]:
        if r.get("table") and r["iri"] not in elem_op:
            warnings.append(f"草稿里的对象属性 {r['name']} 在当前本体中不存在，已跳过")
    for e in elements["classes"]:
        c = cls_by_iri.get(e["iri"])
        if not c or not c.get("table"):
            warnings.append(f"类 {e['name']} 未映射到表（纯推理类可忽略）")
            continue
        if not c.get("pk"):
            warnings.append(f"类 {e['name']} 选了表但未选主键，该类映射将被跳过")
            continue
        # 子类不勾属性属正常：属性由父类映射统一发出（同表子类拆分模式）
        if not c.get("props") and not e.get("parentIri"):
            warnings.append(f"类 {e['name']} 未勾选任何数据属性")
        sig = (c["table"], tuple(c["pk"]))
        if sig in seen_sig:
            other = seen_sig[sig]
            # 父子/同祖的类同表共 IRI 空间是子类模式的预期形态（靠 WHERE/类型断言区分），不算重叠事故
            related = other in _ancestors(e["iri"]) or e["iri"] in _ancestors(other) \
                or bool(_ancestors(e["iri"]) & _ancestors(other))
            if not related:
                warnings.append(f"类 {elem_name.get(other, other)} 与 {e['name']} 同表同主键，个体 IRI 将完全重叠")
        else:
            seen_sig[sig] = e["iri"]
        cols = _schema_columns(schema, c["table"])
        for p in c.get("props") or []:
            if not p.get("col"):
                warnings.append(f"类 {e['name']} 的属性 {p.get('name')} 未选列")
                continue
            if cols is not None and not any(x["name"] == p["col"] for x in cols):
                warnings.append(f"类 {e['name']} 的属性 {p['name']} 列 {p['col']} 不在表 {c['table']} 里")
                continue
            dp = elem_dp.get(p["iri"])
            if cols is not None and dp:
                jdbc_cat = next((JDBC_TYPE_CATEGORY.get(x["jdbc_type"]) for x in cols if x["name"] == p["col"]), None)
                if jdbc_cat and dp["range"] != jdbc_cat:
                    col_tn = next((x["type_name"] for x in cols if x["name"] == p["col"]), "")
                    warnings.append(f"属性 {p['name']} 的 xsd:{dp['range']} 与列 {p['col']} 的 {col_tn} 可能不匹配")
            if not IDENT_RE.match(p["col"]):
                warnings.append(f"列名 {p['col']} 含特殊字符，如 SQL 报错请在第 3 步手工加引号")
        for ident in [c["table"], *c["pk"]]:
            if ident and not IDENT_RE.match(ident):
                warnings.append(f"标识符 {ident} 含特殊字符，如 SQL 报错请在第 3 步手工加引号")

    # 子类 iri_path 与父类一致性检查
    elem_cls_by_iri = {c["iri"]: c for c in elements["classes"]}
    for e in elements["classes"]:
        parent_iri = e.get("parentIri")
        if not parent_iri:
            continue
        child_c = cls_by_iri.get(e["iri"])
        parent_c = cls_by_iri.get(parent_iri)
        if not child_c or not parent_c:
            continue
        if not child_c.get("table") or not parent_c.get("table"):
            continue
        child_path = child_c.get("iri_path") or child_c["name"].lower()
        parent_path = parent_c.get("iri_path") or parent_c["name"].lower()
        parent_name = elem_cls_by_iri.get(parent_iri, {}).get("name") or parent_iri
        if child_path != parent_path:
            warnings.append(
                f"类 {e['name']} 的 IRI 路径段「{child_path}」与父类 {parent_name} 的「{parent_path}」不一致，"
                f"子类和父类个体将是不同 IRI，子类推理会断链；请将两者路径段改为相同值"
            )

    # 关系侧
    mapped_op_iris = {r["iri"] for r in draft["relations"] if r.get("table") and r.get("domain_cols")}
    for e in elements["objectProperties"]:
        if e["iri"] in mapped_op_iris:
            inv = e.get("inverseOfIri")
            if inv and inv in mapped_op_iris:
                inv_name = next((p["name"] for p in elements["objectProperties"] if p["iri"] == inv), inv)
                warnings.append(
                    f"对象属性 {e['name']} 与其互逆属性 {inv_name} 都配置了映射；"
                    f"通常只需映射其中一个，另一个由 inverseOf 公理自动改写，双向映射可能产生重复断言"
                )
            continue
        if e.get("inverseOfIri") and e["inverseOfIri"] in mapped_op_iris:
            continue  # 互逆方向已映射，inverseOf 公理查询时自动改写
        warnings.append(f"对象属性 {e['name']} 未映射（该关系查不到数据）")
    for r in draft["relations"]:
        if not r.get("table"):
            continue
        a, b = cls_by_iri.get(r.get("domainIri") or ""), cls_by_iri.get(r.get("rangeIri") or "")
        if not (a and a.get("table") and a.get("pk")) or not (b and b.get("table") and b.get("pk")):
            warnings.append(f"关系 {r['name']} 的一端类未完成映射，该边将被跳过")
            continue
        if r["mode"] == "domain_fk" and len(r["domain_cols"]) != len(b["pk"]):
            warnings.append(f"关系 {r['name']} 的外键列数与 {b['name']} 主键列数不符，该边将被跳过")
        if r["mode"] == "range_fk" and len(r["domain_cols"]) != len(a["pk"]):
            warnings.append(f"关系 {r['name']} 的外键列数与 {a['name']} 主键列数不符，该边将被跳过")
        if r["mode"] == "junction" and not (len(r["domain_cols"]) == len(a["pk"]) and len(r["range_cols"]) == len(b["pk"])):
            warnings.append(f"关系 {r['name']} 的两端列数与各自类主键列数不符，该边将被跳过")
        if not IDENT_RE.match(r["table"]):
            warnings.append(f"表名 {r['table']} 含特殊字符，如 SQL 报错请在第 3 步手工加引号")
    return errors, warnings


# ---------- .obda 生成 ----------

def _tpl(cols: list[str], path: str, sep: str) -> str:
    return f":{path}/" + sep.join("{" + c + "}" for c in cols)


def generate_obda(draft: dict, elements: dict, db_type: str = "mysql", scope: str | None = None) -> dict:
    """草稿 → {obda, n_mappings, warnings}。纯函数；db_type/scope 仅影响表名引用写法（dm8 全限定）。"""
    warnings: list[str] = []
    ns = draft.get("namespace") or elements.get("namespace") or "http://example.org/onto#"
    dp_range = {p["iri"]: p["range"] for p in elements["dataProperties"]}

    def table_ref(t: str) -> str:
        # dm8：全限定引用；带引号标识符在达梦大小写敏感，而达梦对象名全大写，
        # 草稿表名可能是 MySQL 下拉选的小写 → 必须大写化，否则 "HD_SAAS"."asset" 查不到表
        return f'"{scope}"."{t.upper()}"' if db_type == "dm8" and scope else t

    mapped = {c["iri"]: c for c in draft["classes"] if c.get("table") and c.get("pk")}
    for c in draft["classes"]:
        if c["iri"] not in mapped and (c.get("table") or c.get("pk")):
            warnings.append(f"类 {c['name']} 未完成映射（缺表或缺主键），已跳过")

    # 边统一独立成条（INNER JOIN 验货）；类映射不再内嵌边
    edges: list[tuple[dict, dict, dict]] = []  # (rel, A, B)
    for r in draft["relations"]:
        if not (r.get("table") and r.get("domain_cols")):
            continue
        a, b = mapped.get(r.get("domainIri") or ""), mapped.get(r.get("rangeIri") or "")
        if not (a and b):
            warnings.append(f"关系 {r['name']} 的一端类未完成映射，已跳过")
            continue
        ok = ((r["mode"] == "domain_fk" and len(r["domain_cols"]) == len(b["pk"]))
              or (r["mode"] == "range_fk" and len(r["domain_cols"]) == len(a["pk"]))
              or (r["mode"] == "junction" and len(r["domain_cols"]) == len(a["pk"])
                  and len(r["range_cols"]) == len(b["pk"])))
        if ok:
            edges.append((r, a, b))
        else:
            warnings.append(f"关系 {r['name']} 的列配置与目标类主键数不符，已跳过")
    edges.sort(key=lambda x: x[0]["name"])

    used_ids: set[str] = set()

    def mid_for(name: str) -> str:
        base = f"map-{name.lower()}"
        mid, i = base, 1
        while mid in used_ids:
            i += 1
            mid = f"{base}-{i}"
        if mid != base:
            warnings.append(f"{base} 与已有条目重名，已改为 {mid}")
        used_ids.add(mid)
        return mid

    blocks: list[str] = []

    for c in sorted(mapped.values(), key=lambda x: x["name"]):
        path, sep = c["iri_path"] or c["name"].lower(), c["iri_sep"] or "-"
        subj = _tpl(c["pk"], path, sep)
        parts = [f"{subj} a :{c['name']}"]
        cols: list[str] = list(c["pk"])
        for p in c.get("props") or []:
            if not p.get("col"):
                continue
            rng = dp_range.get(p["iri"], "string")
            rng = "dateTime" if rng == "datetime" else rng  # 表单类型名 → 标准 XSD 局部名（abox 生成器/严格解析器都认驼峰）
            lit = f'"{{{p["col"]}}}"^^xsd:{rng}'
            parts.append(f":{p['name']} {lit}")
            if p["col"] not in cols:
                cols.append(p["col"])
        blocks.append(
            f"mappingId\t{mid_for(c['name'])}\n"
            f"target\t\t{' ; '.join(parts)} .\n"
            f"source\t\tSELECT {', '.join(cols)} FROM {table_ref(c['table'])}"
        )

    for r, a, b in edges:
        a_path, a_sep = a["iri_path"] or a["name"].lower(), a["iri_sep"] or "-"
        b_path, b_sep = b["iri_path"] or b["name"].lower(), b["iri_sep"] or "-"
        if r["mode"] == "domain_fk":
            subj = _tpl(a["pk"], a_path, a_sep)
            obj = _tpl(r["domain_cols"], b_path, b_sep)
            joins = [(b, list(zip(r["domain_cols"], b["pk"])))]
        else:
            subj = _tpl(r["domain_cols"], a_path, a_sep)
            obj = _tpl(r["range_cols"] if r["mode"] == "junction" else b["pk"], b_path, b_sep)
            joins = [(a, list(zip(r["domain_cols"], a["pk"])))]
            if r["mode"] == "junction":
                joins.append((b, list(zip(r["range_cols"], b["pk"]))))
        # 子表列去重后统一 s. 前缀 + AS 裸名（join 下裸列名有歧义；SqlStream 表头取 getColumnLabel 认 AS）
        sel = list(dict.fromkeys(
            list(r["domain_cols"])
            + (list(r["range_cols"]) if r["mode"] == "junction" else list(b["pk"]))
            + (list(a["pk"]) if r["mode"] == "domain_fk" else [])))
        select_cols = ", ".join(f"s.{c} AS {c}" for c in sel)
        join_sql = " ".join(
            f"INNER JOIN {table_ref(p['table'])} {tag} ON "
            + " AND ".join(f"s.{fk} = {tag}.{pk}" for fk, pk in pairs)
            for (p, pairs), tag in zip(joins, ("a", "b")))
        blocks.append(
            f"mappingId\t{mid_for(r['name'])}\n"
            f"target\t\t{subj} :{r['name']} {obj} .\n"
            f"source\t\tSELECT {select_cols} FROM {table_ref(r['table'])} s {join_sql}"
        )

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    header = (
        "; ================================================================\n"
        f"; 平台生成的 OBDA 映射（easy_ontology 映射搭建）· {now}\n"
        f"; 本体命名空间 : {ns}\n"
        f"; 共 {len(blocks)} 条；行首分号为注释；条目之间空行\n"
        "; ================================================================\n"
        "\n"
        "[PrefixDeclaration]\n"
        f":\t\t{ns}\n"
        "xsd:\thttp://www.w3.org/2001/XMLSchema#\n"
        "\n"
        "[MappingDeclaration] @collection [[\n"
    )
    obda = header + "\n\n" + "\n\n".join(blocks) + "\n\n]]\n"
    return {"obda": obda, "n_mappings": len(blocks), "warnings": warnings}


# ---------- obda 文本结构校验（与上传路由同规则） ----------

def validate_obda_text(text: str) -> tuple[bool, str, int]:
    for section in ("[PrefixDeclaration]", "[MappingDeclaration]"):
        if section not in text:
            return False, f"映射结构不完整：缺少 {section}", 0
    n = text.count("mappingId")
    if n == 0:
        return False, "映射里一条条目都没有", 0
    return True, "", n


# ---------- 外键命中率抽检（防死外键边静默进图） ----------

FK_SAMPLE_N = 200
FK_TIMEOUT_S = 90
_SQL_SEP = "\x01"  # 与 abox_gen/SqlStream 管道字段分隔一致


def _fk_run_sql(ds: dict, sql: str) -> dict[str, str]:
    """跑一条 SQL 返回首数据行（列名小写键）。复用 tools/SqlStream，与 ABox 生成同通道。"""
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from backend.config import get_config
    from backend.services.properties_builder import build_jdbc_url

    cfg = get_config()
    tcfg = cfg.db_types[ds["db_type"]]
    tools = Path(__file__).resolve().parent.parent.parent / "tools"
    cp_sep = ";" if sys.platform == "win32" else ":"
    classpath = cp_sep.join(filter(None, [
        str(tools), tcfg.ping_classpath or cfg.tools.dbping_classpath_default]))
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
        f.write(sql)
        sql_file = f.name
    try:
        r = subprocess.run(
            [cfg.tools.java_bin, "-Dfile.encoding=UTF-8", "-cp", classpath, "SqlStream",
             build_jdbc_url(ds), ds["user"], ds["password"], tcfg.driver, sql_file],
            capture_output=True, timeout=FK_TIMEOUT_S, cwd=str(tools))
        lines = r.stdout.decode("utf-8", "replace").strip().splitlines()
        if r.returncode != 0 or len(lines) < 2:
            raise RuntimeError((r.stderr or r.stdout or b"").decode("utf-8", "replace")[:300] or "SqlStream 无输出")
        header = [h.strip().lower() for h in lines[0].split(_SQL_SEP)]
        row = lines[1].split(_SQL_SEP)
        return dict(zip(header, row))
    finally:
        Path(sql_file).unlink(missing_ok=True)


def check_fk_hit_rates(draft: dict, ds: dict, scope: str | None = None,
                       sample_n: int = FK_SAMPLE_N, full: bool = True) -> dict:
    """逐边验外键命中率：非空外键 LEFT JOIN 对端表数命中。

    默认全量核对（DM8 实测单边 2~5 秒，索引 hash join）；抽样模式（full=False）按主键头尾各半，
    但脏数据聚在表中段时会漏报（实测 ASSETSPEC 表头表尾 100% 干净、中段 33% 孤儿），仅作快速粗筛。
    命中 0% = 死外键（无 join 胖映射会把整条边物化成幽灵）；<90% = 脏外键（部分悬空）。
    单边失败不影响其余边。
    """
    from concurrent.futures import ThreadPoolExecutor

    db_type = ds.get("db_type", "mysql")

    def q(t: str) -> str:
        # 与 generate_obda 的 table_ref 同规则：dm8 全限定 + 大写化
        return f'"{scope}"."{t.upper()}"' if db_type == "dm8" and scope else t

    mapped = {c["iri"]: c for c in draft["classes"] if c.get("table") and c.get("pk")}
    checks: list[tuple[dict, str, list[str], str, list[str], str]] = []  # (rel, fk表, fk列, 对端表, 对端主键, 排序列)
    for r in draft["relations"]:
        if not (r.get("table") and r.get("domain_cols")):
            continue
        a, b = mapped.get(r.get("domainIri") or ""), mapped.get(r.get("rangeIri") or "")
        if not (a and b):
            continue
        if r["mode"] == "domain_fk" and len(r["domain_cols"]) == len(b["pk"]):
            checks.append((r, r["table"], r["domain_cols"], b["table"], b["pk"], a["pk"][0]))
        elif r["mode"] == "range_fk" and len(r["domain_cols"]) == len(a["pk"]):
            checks.append((r, r["table"], r["domain_cols"], a["table"], a["pk"], b["pk"][0]))
        elif r["mode"] == "junction" and r.get("range_cols") and len(r["range_cols"]) == len(b["pk"]):
            # 中间表抽 range 侧（domain 侧同表同理，抽一侧足够示警）
            checks.append((r, r["table"], r["range_cols"], b["table"], b["pk"], r["domain_cols"][0]))

    half = max(1, sample_n // 2)

    def run_one(item):
        r, fk_table, fks, other, pks, order_col = item
        sel = ", ".join(fks)
        on = " AND ".join(f"o.{pk} = s.{fk}" for pk, fk in zip(pks, fks))
        out = {"name": r["name"], "mode": r["mode"], "fk_table": fk_table, "other_table": other}
        try:
            if full:
                sql = (f"SELECT COUNT(*) AS total, COUNT(o.{pks[0]}) AS hit "
                       f"FROM {q(fk_table)} s LEFT JOIN {q(other)} o ON {on} "
                       f"WHERE s.{fks[0]} IS NOT NULL")
                row = _fk_run_sql(ds, sql)
                total, hit = int(row.get("total") or 0), int(row.get("hit") or 0)
            else:
                subs = []
                if db_type == "dm8":
                    subs.append(f"SELECT TOP {half} {sel} FROM {q(fk_table)} WHERE {fks[0]} IS NOT NULL")
                    subs.append(f"SELECT TOP {half} {sel} FROM {q(fk_table)} WHERE {fks[0]} IS NOT NULL ORDER BY {order_col} DESC")
                else:
                    subs.append(f"SELECT {sel} FROM {q(fk_table)} WHERE {fks[0]} IS NOT NULL LIMIT {half}")
                    subs.append(f"SELECT {sel} FROM {q(fk_table)} WHERE {fks[0]} IS NOT NULL ORDER BY {order_col} DESC LIMIT {half}")
                total = hit = 0
                for sub in subs:
                    sql = (f"SELECT COUNT(*) AS total, COUNT(o.{pks[0]}) AS hit "
                           f"FROM ({sub}) s LEFT JOIN {q(other)} o ON {on}")
                    row = _fk_run_sql(ds, sql)
                    total += int(row.get("total") or 0)
                    hit += int(row.get("hit") or 0)
            out.update(total=total, hit=hit,
                       rate=(hit / total) if total else None,
                       verdict=("empty" if not total else
                                "dead" if hit == 0 else
                                "warn" if hit / total < 0.9 else "ok"))
        except Exception as e:
            out.update(total=None, hit=None, rate=None, verdict="error", error=str(e).strip()[:200])
        return out

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run_one, checks))
    return {"ok": True, "sample_n": sample_n, "full": full, "db_type": db_type, "results": results}
