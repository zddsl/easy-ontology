"""自写 ABox 生成引擎：解析 .obda → 查库并行生成 NT。

一映射一 worker：java SqlStream（tools/，流式 JDBC 管道）吐行，
Python 填模板写 part 文件，全成功后合并原子替换 abox.nt。
与 ontop materialize 产物语义一致（平台向导生成的受限映射模式 +
RDFS 公理加宽），越出模式的构造直接 UnsupportedMappingError。
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from backend.config import get_config
from backend.services.properties_builder import build_jdbc_url

SEP = "\x01"          # SqlStream 管道字段分隔
NULL_MARK = "\x00"    # SqlStream 管道 NULL 标记
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
XSD_NS = "http://www.w3.org/2001/XMLSchema#"
# 支持的字面量类型白名单（平台向导的映射只会用到这些）
XSD_OK = {f"{XSD_NS}{t}" for t in (
    "string", "integer", "long", "short", "double", "float",
    "decimal", "boolean", "date", "time", "dateTime",
)}
MAX_WORKERS = 4
WORKER_DEADLINE_S = 3600  # 单 worker 硬上限


class UnsupportedMappingError(ValueError):
    """映射含受限模式之外的构造（函数模板/多主语/blank node/…）"""


class AboxGenError(RuntimeError):
    """生成失败（SQL/管道/IO）"""


class CanceledError(Exception):
    """用户取消"""


# ---------- .obda 解析（mappingId/target/source 三行块 + 前缀表） ----------

_TPL_COL = re.compile(r"\{([A-Za-z0-9_]+)\}")


def parse_obda(text: str) -> tuple[dict[str, str], list[dict]]:
    """返回 (前缀表, [{id, target, source}])。"""
    prefixes: dict[str, str] = {}
    mappings: list[dict] = []
    section = None
    cur: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[PrefixDeclaration]"):
            section = "prefix"
            continue
        if line.startswith("[MappingDeclaration]"):
            section = "mapping"
            continue
        if line == "]]":
            section = None
            continue
        if section == "prefix":
            parts = re.split(r"[ \t]+", line, 1)
            if len(parts) == 2 and parts[0].endswith(":"):
                prefixes[parts[0][:-1]] = parts[1].strip()
        elif section == "mapping":
            parts = re.split(r"[ \t]+", line, 1)
            kind, val = parts[0], parts[1].strip() if len(parts) > 1 else ""
            if kind == "mappingId":
                if cur.get("id"):
                    mappings.append(cur)
                cur = {"id": val, "target": "", "source": ""}
            elif kind in ("target", "source") and cur.get("id") is not None:
                cur[kind] = val
    if cur.get("id"):
        mappings.append(cur)
    if not mappings:
        raise UnsupportedMappingError("映射文件里没有找到任何 mappingId 条目")
    for m in mappings:
        if not (m["target"] and m["source"]):
            raise UnsupportedMappingError(f"映射 {m['id']} 缺 target 或 source")
    return prefixes, mappings


# ---------- target 模板编译（受限模式） ----------

def _expand(name: str, prefixes: dict[str, str], what: str) -> str:
    """`:asset` / `xsd:string` / `<完整IRI>` → 无尖括号完整 IRI。"""
    if name.startswith("<") and name.endswith(">"):
        return name[1:-1]
    if name.startswith("<"):
        raise UnsupportedMappingError(f"{what} 的 IRI 缺少右尖括号：{name}")
    pfx, _, local = name.partition(":")
    if pfx not in prefixes:
        raise UnsupportedMappingError(f"{what} 用了未声明的前缀：{name}")
    return prefixes[pfx] + local


def _parse_iri_template(tpl: str, prefixes: dict[str, str], what: str):
    """`:asset/{A}-{B}` / `<http://…/{A}>` → (ns, 文字片段列表, 列名列表)。"""
    if tpl.startswith("<"):
        if not tpl.endswith(">"):
            raise UnsupportedMappingError(f"{what} 的 IRI 模板缺右尖括号：{tpl}")
        ns, rest = "", tpl[1:-1]
    else:
        pfx, sep, rest = tpl.partition(":")
        if not sep or pfx not in prefixes:
            raise UnsupportedMappingError(f"{what} 用了未声明的 IRI 前缀：{tpl}")
        ns = prefixes[pfx]
    parts: list[str] = []
    cols: list[str] = []
    last = 0
    for m in _TPL_COL.finditer(rest):
        parts.append(rest[last:m.start()])
        cols.append(m.group(1))
        last = m.end()
    parts.append(rest[last:])
    residual = _TPL_COL.sub("", rest)
    if "{" in residual or "}" in residual:
        # 删掉合法 {列名} 后仍有花括号 = 函数/表达式占位（如 {A*B}），不能静默当常量
        raise UnsupportedMappingError(f"{what} 的 IRI 模板含不支持的占位表达式（只支持 {{列名}}）：{tpl}")
    return ns, parts, cols


def _render_iri(tpl: tuple[str, list[str], list[str]], row: dict) -> str | None:
    """填模板 → `<IRI>`；任一列为 NULL 返回 None。列值百分号编码（非法 IRI 字符）。"""
    ns, parts, cols = tpl
    out = [ns]
    for i, col in enumerate(cols):
        v = row.get(col)
        if v is None:
            return None
        out.append(parts[i])
        out.append(quote(str(v), safe=""))
    out.append(parts[len(cols)])
    return "<" + "".join(out) + ">"


def compile_target(mid: str, target: str, prefixes: dict[str, str]) -> dict:
    """target → 结构化映射对象；越出受限模式抛 UnsupportedMappingError。"""
    t = target.strip()
    if t.endswith("."):
        t = t[:-1].strip()
    segs = [s.strip() for s in t.split(";") if s.strip()]
    if not segs:
        raise UnsupportedMappingError(f"映射 {mid} 的 target 为空")

    subj_types: list[str] = []
    props: list[dict] = []
    parsed: list[tuple[str, str]] = []  # (pred, obj) 待逐个解析

    first = segs[0]
    m = re.match(r"^(\S+)\s+a\s+(\S+)$", first)
    if m:
        subj_tpl = m.group(1)
        subj_types.append(_expand(m.group(2), prefixes, f"映射 {mid} 的类型"))
    else:
        m2 = re.match(r"^(\S+)\s+(\S+)\s+(\S+)$", first)
        if not m2:
            raise UnsupportedMappingError(
                f"映射 {mid} 的 target 第一段无法识别（支持『主语 a 类』或『主语 谓语 宾语』）：{first}")
        subj_tpl = m2.group(1)
        parsed.append((m2.group(2), m2.group(3)))

    for seg in segs[1:]:
        if re.search(r"\sa\s", seg):
            raise UnsupportedMappingError(f"映射 {mid} 含多主语构造：{seg}")
        bits = seg.split(None, 1)
        if len(bits) != 2:
            raise UnsupportedMappingError(f"映射 {mid} 无法解析的段：{seg}")
        parsed.append((bits[0], bits[1]))

    for pred_s, obj_s in parsed:
        pred = _expand(pred_s, prefixes, f"映射 {mid} 的谓语")
        if obj_s.startswith('"') or re.match(r'^\{[A-Za-z0-9_]+\}(?:\^\^\S+)?$', obj_s):
            # 字面量：只认 "{COL}"^^xsd:xxx（或裸 "{COL}" 视为 string；引号可省，兼容旧映射生成器的非 string 无引号写法）
            lm = re.match(r'^"?(\{[A-Za-z0-9_]+\})"?(?:\^\^(\S+))?$', obj_s)
            if not lm:
                raise UnsupportedMappingError(
                    f"映射 {mid} 的字面量只支持单列占位 \"{{COL}}\"^^类型（不支持拼接/函数/语言标签）：{obj_s}")
            col = lm.group(1)[1:-1]
            if lm.group(2) is None:
                xsd = XSD_NS + "string"
            else:
                xsd = _expand(lm.group(2), prefixes, f"映射 {mid} 的字面量类型")
                # 小写 datetime 是表单类型别名，归一为标准 XSD 名 dateTime
                if xsd == XSD_NS + "datetime":
                    xsd = XSD_NS + "dateTime"
                if xsd not in XSD_OK:
                    raise UnsupportedMappingError(f"映射 {mid} 不支持的字面量类型：{obj_s}")
            props.append({"kind": "lit", "pred": pred, "col": col, "xsd": xsd})
        elif obj_s.startswith("_:") or obj_s.startswith("["):
            raise UnsupportedMappingError(f"映射 {mid} 不支持 blank node：{obj_s}")
        elif obj_s.startswith(":") or obj_s.startswith("<"):
            tpl = _parse_iri_template(obj_s, prefixes, f"映射 {mid} 的宾语")
            props.append({"kind": "obj", "pred": pred, "tpl": tpl})
        else:
            raise UnsupportedMappingError(f"映射 {mid} 无法识别的宾语：{obj_s}")

    subj = _parse_iri_template(subj_tpl, prefixes, f"映射 {mid} 的主语")
    needed = list(subj[2])
    for p in props:
        if p["kind"] == "lit":
            needed.append(p["col"])
        else:
            needed.extend(p["tpl"][2])
    return {"id": mid, "subj": subj, "subj_types": subj_types, "props": props,
            "needed_cols": needed}


# ---------- RDFS 公理加宽（编译期查表补行，通用） ----------

def build_axioms(ontology_path: Path) -> dict:
    """读本体建公理表：类/属性层次闭包 + domain/range（range 里滤掉字面量类型）。"""
    from rdflib import Graph, OWL, RDF, RDFS

    g = Graph()
    g.parse(str(ontology_path), format="turtle" if ontology_path.suffix == ".ttl" else None)

    def closure(direct: dict) -> dict:
        out = {}
        for start in direct:
            seen, stack = set(), list(direct.get(start, ()))
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                stack.extend(direct.get(x, ()))
            if seen:
                out[start] = seen
        return out

    sub_c: dict = {}
    sub_p: dict = {}
    dom: dict = {}
    rng: dict = {}
    for s, o in g.subject_objects(RDFS.subClassOf):
        sub_c.setdefault(str(s), set()).add(str(o))
    for s, o in g.subject_objects(RDFS.subPropertyOf):
        sub_p.setdefault(str(s), set()).add(str(o))
    for s, o in g.subject_objects(RDFS.domain):
        dom.setdefault(str(s), set()).add(str(o))
    for s, o in g.subject_objects(RDFS.range):
        if not str(o).startswith(XSD_NS):
            rng.setdefault(str(s), set()).add(str(o))

    classes = set()
    for t in (RDFS.Class, OWL.Class):
        classes.update(str(x) for x in g.subjects(RDF.type, t))

    def only_classes(s: set) -> set:
        # domain/range 里可能出现未声明为类的东西：有类注册表就按它过滤，没有就全保留
        return {c for c in s if c in classes} if classes else set(s)

    return {
        "subclass": closure(sub_c),
        "subprop": closure(sub_p),
        "domain": {p: only_classes(v) for p, v in dom.items()},
        "range": {p: only_classes(v) for p, v in rng.items()},
    }


def apply_widening(compiled: list[dict], ax: dict) -> None:
    """把公理表折进编译产物（物化推理 = 编译期模板加宽）：
    类型补祖先类；属性行补超属性边；边产出后按 domain/range 补主/宾语类型。
    """
    for c in compiled:
        types = set(c["subj_types"])
        for t in list(types):
            types |= ax["subclass"].get(t, set())
        c["subj_types"] = sorted(types)
        for p in c["props"]:
            p["supers"] = sorted(ax["subprop"].get(p["pred"], set()))
            p["extra_dom"] = sorted(ax["domain"].get(p["pred"], set()) - types)
            p["range_types"] = sorted(ax["range"].get(p["pred"], set()))


# ---------- NT 序列化（转义抄自已验证的 generate_nt_from_mysql.py） ----------

def _esc(text: str) -> str:
    return (str(text)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t"))


def _emit(c: dict, row: dict, out: list) -> None:
    """一行查询结果 → NT 行（追加到 out）。主键 NULL 跳行；值 NULL 跳三元组。"""
    subj = _render_iri(c["subj"], row)
    if subj is None:
        return
    for cls in c["subj_types"]:
        out.append(f"{subj} {RDF_TYPE} <{cls}> .\n")
    for p in c["props"]:
        if p["kind"] == "lit":
            v = row.get(p["col"])
            if v is None:
                continue
            out.append(f'{subj} <{p["pred"]}> "{_esc(v)}"^^<{p["xsd"]}> .\n')
        else:
            obj = _render_iri(p["tpl"], row)
            if obj is None:
                continue
            out.append(f"{subj} <{p['pred']}> {obj} .\n")
            for sup in p["supers"]:
                out.append(f"{subj} <{sup}> {obj} .\n")
        for cls in p["extra_dom"]:
            out.append(f"{subj} {RDF_TYPE} <{cls}> .\n")
        if p["kind"] == "obj":
            for cls in p["range_types"]:
                out.append(f"{obj} {RDF_TYPE} <{cls}> .\n")  # noqa: F821（obj 上面分支必有）


# ---------- SqlStream 管道消费 ----------

def _unescape(s: str) -> str:
    if "\\" not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            d = s[i + 1]
            if d == "\\":
                out.append("\\"); i += 2; continue
            if d == "n":
                out.append("\n"); i += 2; continue
            if d == "r":
                out.append("\r"); i += 2; continue
            if d == "e":
                out.append(SEP); i += 2; continue
        out.append(c)
        i += 1
    return "".join(out)


def _worker(c: dict, idx: int, ds: dict, parts_dir: Path, log_path: Path,
            counters: list, procs: dict, lock: threading.Lock, cancel: threading.Event):
    """跑一条映射：Popen SqlStream → 读管道填模板写 part-<idx>.nt。"""
    cfg = get_config()
    tcfg = cfg.db_types[ds["db_type"]]
    classpath = ":".join([str(_tools_dir()), tcfg.ping_classpath or cfg.tools.dbping_classpath_default])
    sql_file = parts_dir / f".part-{idx}.sql"
    sql_file.write_text(c["sql"], encoding="utf-8")
    err_file = parts_dir / f".part-{idx}.err"
    part_file = parts_dir / f"part-{idx}.nt"
    cmd = [cfg.tools.java_bin, "-Dfile.encoding=UTF-8", "-cp", classpath,
           "SqlStream", build_jdbc_url(ds), ds["user"], ds["password"],
           tcfg.driver, str(sql_file)]
    with open(err_file, "wb") as ef:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef,
                                cwd=str(_tools_dir()))
        with lock:
            procs[idx] = proc
        n = 0
        deadline = time.time() + WORKER_DEADLINE_S
        try:
            header_line = proc.stdout.readline()
            if not header_line:
                raise AboxGenError(f"映射 {c['id']}：SqlStream 无输出（{err_tail(err_file)}）")
            header = [h.decode("utf-8", "replace") for h in header_line.rstrip(b"\n").split(SEP.encode())]
            missing = [col for col in c["needed_cols"] if col not in header]
            if missing:
                raise AboxGenError(
                    f"映射 {c['id']}：SQL 结果缺模板需要的列 {missing}（结果列：{header}）")
            with open(part_file, "wb") as out:
                batch: list[str] = []
                for raw in proc.stdout:
                    if cancel.is_set():
                        raise CanceledError()
                    if time.time() > deadline:
                        raise AboxGenError(f"映射 {c['id']}：超过 {WORKER_DEADLINE_S}s 未完成")
                    fields = raw.rstrip(b"\n").split(SEP.encode())
                    row = {}
                    for k, fb in zip(header, fields):
                        row[k] = None if fb == NULL_MARK.encode() else _unescape(fb.decode("utf-8", "replace"))
                    before = len(batch)
                    _emit(c, row, batch)
                    n += len(batch) - before
                    if len(batch) >= 2000:
                        out.write("".join(batch).encode("utf-8"))
                        batch.clear()
                        counters[idx] = n
                if batch:
                    out.write("".join(batch).encode("utf-8"))
                counters[idx] = n
            rc = proc.wait()
            if rc != 0:
                raise AboxGenError(f"映射 {c['id']} 查询失败（退出码 {rc}）：{err_tail(err_file)}")
        finally:
            with lock:
                procs.pop(idx, None)
            if cancel.is_set():
                proc.kill()
    return idx, n


def err_tail(err_file: Path, limit: int = 400) -> str:
    try:
        return err_file.read_text(encoding="utf-8", errors="replace")[-limit:].strip()
    except OSError:
        return "(无 stderr)"


def _tools_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "tools"


# ---------- 对外入口 ----------

def prepare(store) -> dict:
    """同步准备（POST 时调用，UnsupportedMappingError 在这里抛 → 400）：
    解析映射、编译模板、公理加宽、收集数据源信息。"""
    snap = store.snapshot()
    ds = snap.get("datasource")
    if not ds:
        raise AboxGenError("缺少数据源配置")
    if not (snap.get("ontology") and snap.get("mapping")):
        raise AboxGenError("缺少本体或映射")
    prefixes, mappings = parse_obda(store.mapping_path.read_text(encoding="utf-8"))
    compiled = [compile_target(m["id"], m["target"], prefixes) for m in mappings]
    for m, c in zip(mappings, compiled):
        c["sql"] = m["source"]
    try:
        axioms = build_axioms(store.ontology_path)
    except Exception as e:
        raise AboxGenError(f"读本体失败：{e}")
    apply_widening(compiled, axioms)
    return {"compiled": compiled, "ds": ds}


def cleanup(store) -> None:
    """清残留的 part/tmp（job 结束/异常后的 finally 里调）。"""
    parts_dir = store.files_dir / ".abox-parts"
    if parts_dir.exists():
        for old in parts_dir.iterdir():
            old.unlink(missing_ok=True)
    store.abox_path.with_suffix(".nt.tmp").unlink(missing_ok=True)


def run(prepared: dict, store, state, cancel: threading.Event, log_path: Path) -> dict:
    """后台执行（线程里跑）：并行生成 part → 合并 → 原子替换 abox.nt → 返回统计。
    state 需提供 workers_total/workers_done/triples(计数数组)/phase/procs/lock。"""
    compiled: list[dict] = prepared["compiled"]
    ds: dict = prepared["ds"]
    parts_dir = store.files_dir / ".abox-parts"
    parts_dir.mkdir(exist_ok=True)
    for old in parts_dir.iterdir():
        old.unlink(missing_ok=True)

    state.workers_total = len(compiled)
    counters = [0] * len(compiled)
    state.triples = counters
    procs: dict = {}
    lock = threading.Lock()
    state.procs = procs
    t0 = time.time()

    workers = min(len(compiled), MAX_WORKERS)
    results: dict[int, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_worker, c, i, ds, parts_dir, log_path, counters, procs, lock, cancel): i
                for i, c in enumerate(compiled)}
        try:
            for fut in as_completed(futs):
                idx, n = fut.result()
                results[idx] = n
                state.workers_done += 1
        except (CanceledError, AboxGenError):
            cancel.set()
            with lock:
                for p in list(procs.values()):
                    p.kill()
            raise
    if cancel.is_set():
        raise CanceledError()

    # 全部 worker 成功 → 合并去重（推理补行会与原行重复；去重后行数=QLever 索引数，
    # index-status 对账才准）→ 原子替换
    state.phase = "merging"
    tmp = store.abox_path.with_suffix(".nt.tmp")
    part_files = " ".join(str(parts_dir / f"part-{i}.nt") for i in range(len(compiled)))
    # sort 结果先落容器本地 /tmp 再拷回挂载卷：Windows 绑定挂载扛不住 GB 级长流写
    # （sort: write failed: Input/output error 之坑）；挂载卷上只做一次顺序拷贝 + 同目录原子改名
    staged = Path(tempfile.gettempdir()) / f"abox-sorted-{os.getpid()}.nt"
    rc = subprocess.run(
        ["sh", "-c", f"cat {part_files} | sort -u -T {tempfile.gettempdir()} > {staged}"]).returncode
    if rc != 0 or not staged.exists():
        staged.unlink(missing_ok=True)
        raise AboxGenError(f"合并去重失败（sort 退出码 {rc}）")
    try:
        with open(staged, "rb") as src, open(tmp, "wb") as dst:
            while chunk := src.read(1 << 22):
                dst.write(chunk)
        if tmp.stat().st_size != staged.stat().st_size:
            raise AboxGenError("合并结果拷回挂载卷后大小不符")
    finally:
        staged.unlink(missing_ok=True)
    total = 0
    with open(tmp, "rb") as f:
        while chunk := f.read(1 << 22):
            total += chunk.count(b"\n")
    size = tmp.stat().st_size
    tmp.replace(store.abox_path)
    for old in parts_dir.iterdir():
        old.unlink(missing_ok=True)

    # 数据质量体检（A5）：源库只读统计悬空键/脏值，失败只记日志不影响产物
    state.phase = "auditing"
    quality = None
    try:
        from backend.services.abox_audit import audit
        quality = audit(prepared, store, state, cancel, log_path)
    except CanceledError:
        raise
    except Exception as e:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%F %T')}] 体检失败（不影响物化产物）：{e}\n")
        except OSError:
            pass

    from backend.services.project_store import _now_str
    elapsed_ms = int((time.time() - t0) * 1000)
    store.update_abox({
        "generated_at": _now_str(),
        "triples": total,
        "size_bytes": size,
        "source": "builtin",
        "workers": len(compiled),
        "elapsed_ms": elapsed_ms,
        "quality": quality,
    })
    return {"triples": total, "size_bytes": size,
            "workers": len(compiled), "elapsed_ms": elapsed_ms,
            "quality": quality}
