"""项目路由：上传本体/映射/ABox、数据源表单、测试连接、状态。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from backend.services.ping import ping
from backend.services.project_store import get_store, _now_str
from backend.services.properties_builder import validate_form

router = APIRouter(prefix="/api", tags=["project"])


# ---------- 本体上传 ----------

@router.post("/files/ontology")
async def upload_ontology(file: UploadFile = File(...)):
    """接受 .rdf/.owl/.ttl：Turtle 自动转 RDF/XML；RDF/XML 原样存（先验证可解析）。"""
    raw = (await file.read()).decode("utf-8-sig", errors="strict")
    name = file.filename or "ontology.rdf"
    ext = Path(name).suffix.lower()
    source_format = "rdf/xml"
    if ext in (".ttl", ".turtle", ".n3"):
        source_format = "turtle"
    try:
        import rdflib

        g = rdflib.Graph()
        if source_format == "turtle":
            g.parse(data=raw, format="turtle")
            stored = g.serialize(format="pretty-xml")  # 类型化元素形式，Protege/Playground 均认
        else:
            # 先按 RDF/XML 解析验证；嗅探兜底：不是 XML 就按 Turtle 再试
            try:
                g.parse(data=raw, format="xml")
                stored = raw  # XML 原样保存，避免序列化器改写
            except Exception:
                g.parse(data=raw, format="turtle")
                stored = g.serialize(format="pretty-xml")
                source_format = "turtle(detected)"
        n_triples = len(g)
    except Exception as e:
        raise HTTPException(400, f"本体解析失败：{e}")

    store = get_store()
    store.save_ontology(stored, name, source_format, origin="upload")
    return {"state": store.state, "triples": n_triples, "format": source_format}


# ---------- 映射上传 ----------

@router.post("/files/mapping")
async def upload_mapping(file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8-sig", errors="strict")
    name = file.filename or "mapping.obda"
    if not name.lower().endswith(".obda"):
        raise HTTPException(400, "只接受 .obda 映射文件")
    for section in ("[PrefixDeclaration]", "[MappingDeclaration]"):
        if section not in raw:
            raise HTTPException(400, f"映射结构不完整：缺少 {section}")
    n_mappings = raw.count("mappingId")
    if n_mappings == 0:
        raise HTTPException(400, "映射里一条 mappingId 都没有")

    store = get_store()
    store.save_mapping(raw, name, n_mappings)
    return {"state": store.state, "n_mappings": n_mappings}


@router.delete("/files/ontology")
def delete_ontology():
    store = get_store()
    store.clear_ontology()
    return {"state": store.state}


@router.delete("/files/mapping")
def delete_mapping():
    store = get_store()
    store.clear_mapping()
    return {"state": store.state}


# ---------- 数据源 ----------

class DatasourceForm(BaseModel):
    db_type: str
    host: str
    port: str | int
    database: str = ""
    user: str
    password: str


@router.put("/datasource")
def put_datasource(form: DatasourceForm):
    data = {**form.model_dump(), "port": str(form.port)}
    err = validate_form(data)
    if err:
        raise HTTPException(400, err)
    store = get_store()
    store.save_datasource(data)
    return {"state": store.state, "hint": _type_hint(form.db_type)}


@router.post("/test-connection")
def test_connection(form: DatasourceForm | None = None):
    """测【当前表单值】（请求体）；不带 body 才退回已保存配置（兼容脚本调用）。"""
    store = get_store()
    if form is not None:
        ds = {**form.model_dump(), "port": str(form.port)}
        err = validate_form(ds)
        if err:
            raise HTTPException(400, f"表单有问题：{err}")
    else:
        ds = store.snapshot().get("datasource")
        if not ds:
            raise HTTPException(400, "请先保存数据源配置")
    result = ping(ds)
    result["at"] = _now_str()
    result["tested"] = "form" if form is not None else "saved"
    store.record_ping(result)
    return result


def _type_hint(db_type: str) -> str:
    from backend.config import get_config

    return get_config().db_types[db_type].hint


# ---------- 状态 ----------

@router.get("/status")
def status():
    from backend.services.workspace_registry import get_registry

    store = get_store()
    snap = store.snapshot()
    snap["workspace"] = get_registry().active_id()  # 多工作空间：标明状态归属
    return snap


@router.get("/dbtype-hint")
def dbtype_hint(type: str):
    from backend.config import get_config

    t = get_config().db_types.get(type)
    if not t:
        raise HTTPException(404, f"未知类型 {type}")
    return {"hint": t.hint}


@router.get("/logs/ontop")
def logs_ontop(lines: int = 40):
    store = get_store()
    try:
        content = store.ontop_log.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"tail": "\n".join(content[-lines:])}
    except FileNotFoundError:
        return {"tail": ""}


# ---------- ABox 上传 ----------

_BIG_NT_THRESHOLD = 50 * 1024 * 1024  # .nt 超过 50MB 走流式快路径（rdflib 全量解析 12M+ 三元组会耗尽内存）


def _stream_big_nt(src, abox_path: Path, store) -> dict:
    """大 NT 流式入库（工作线程执行，避免阻塞事件循环）：
    8MB 块读取、按行切分（处理块边界），跳过注释/空行，逐行轻校验
    （UTF-8 可解码、以 . 结尾），坏行丢弃并计数；原子替换 abox.nt。
    """
    import time

    t0 = time.time()
    tmp = abox_path.with_suffix(".nt.tmp")
    n_ok = n_comment = n_bad = 0
    bad_samples: list[str] = []
    ns_sample: set[str] = set()
    try:
        with open(tmp, "wb") as out:
            pending = b""
            while True:
                chunk = src.read(8 * 1024 * 1024)
                eof = not chunk
                if eof:
                    parts = [pending] if pending else []
                    pending = b""
                else:
                    parts = (pending + chunk).split(b"\n")
                    pending = parts.pop()
                for line in parts:
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith(b"#"):
                        n_comment += 1
                        continue
                    if not s.endswith(b"."):
                        n_bad += 1
                        if len(bad_samples) < 3:
                            bad_samples.append(s[:100].decode("utf-8", "replace"))
                        continue
                    try:
                        text = s.decode("utf-8")
                    except UnicodeDecodeError:
                        n_bad += 1
                        if len(bad_samples) < 3:
                            bad_samples.append(s[:100].decode("utf-8", "replace"))
                        continue
                    if len(ns_sample) < 30 and text.startswith("<"):
                        iri = text[1:text.find(">")] if ">" in text else text[:80]
                        ns_sample.add(iri.split("#")[0] if "#" in iri else iri.rsplit("/", 1)[0])
                    out.write(s + b"\n")
                    n_ok += 1
                if eof:
                    break  # EOF：处理完最后一批后必须退出，否则空转死循环
        tmp.replace(abox_path)  # 原子替换，看守进程随即触发索引重建
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(400, f"NT 流式处理失败：{e}")

    warnings = []
    if n_bad:
        warnings.append(f"⚠️ 丢弃 {n_bad} 行无法解析的行，如：{bad_samples}")
    if n_comment:
        warnings.append(f"已跳过 {n_comment} 行注释")
    try:  # 命名空间一致性（采样比对当前本体）
        import rdflib

        og = rdflib.Graph()
        og.parse(data=store.ontology_path.read_text(encoding="utf-8"), format="xml")
        ont_ns = {str(ns) for _, ns in og.namespaces()}
        if ns_sample and not (ns_sample & ont_ns):
            warnings.append(f"⚠️ IRI 命名空间不匹配：NT 用 {sorted(ns_sample)[:2]}，本体用 {sorted(ont_ns)[:2]}")
    except Exception:
        pass

    size_bytes = abox_path.stat().st_size
    store.update_abox({
        "generated_at": _now_str(),
        "triples": n_ok,
        "size_bytes": size_bytes,
        "source": "upload",
        "warnings": warnings,
    })
    return {
        "ok": True,
        "triples": n_ok,
        "size_bytes": size_bytes,
        "format": "nt-stream",
        "warnings": warnings,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "note": "QLever 索引将在后台自动重建，大文件约需数分钟",
    }


@router.post("/files/abox")
async def upload_abox(file: UploadFile = File(...)):
    """接受 .nt/.ttl/.rdf 文件作为 ABox，解析验证后保存到 abox.nt。

    重要：NT 文件中的 IRI 必须与当前本体一致才能正确查询。
    """
    name = file.filename or "abox.nt"
    ext = Path(name).suffix.lower()
    store = get_store()

    # 大 NT 快路径：不经 rdflib（全量解析 GB 级文件会打爆内存），流式校验+落盘
    size = getattr(file, "size", None) or 0
    if ext == ".nt" and size > _BIG_NT_THRESHOLD:
        import asyncio

        return await asyncio.to_thread(_stream_big_nt, file.file, store.abox_path, store)

    raw = (await file.read()).decode("utf-8-sig", errors="strict")
    source_format = "nt"
    if ext in (".ttl", ".turtle", ".n3"):
        source_format = "turtle"
    elif ext in (".rdf", ".owl", ".xml"):
        source_format = "rdf/xml"

    try:
        import rdflib

        g = rdflib.Graph()
        if source_format == "turtle":
            g.parse(data=raw, format="turtle")
            stored = g.serialize(format="nt")  # 统一转为 NT 格式保存
        elif source_format == "rdf/xml":
            g.parse(data=raw, format="xml")
            stored = g.serialize(format="nt")
        else:  # NT 原样
            g.parse(data=raw, format="nt")
            stored = raw  # NT 原样保存
        n_triples = len(g)
    except Exception as e:
        raise HTTPException(400, f"ABox 解析失败：{e}")

    store = get_store()

    # IRI 一致性检查
    warnings = []
    ontology_path = store.ontology_path
    if ontology_path.exists():
        try:
            # 提取 NT 文件中使用的命名空间
            nt_namespaces = set()
            for s, p, o in g:
                if isinstance(s, rdflib.URIRef):
                    ns = str(s).split("#")[0] if "#" in str(s) else str(s).rsplit("/", 1)[0]
                    nt_namespaces.add(ns)
                if isinstance(p, rdflib.URIRef):
                    ns = str(p).split("#")[0] if "#" in str(p) else str(p).rsplit("/", 1)[0]
                    nt_namespaces.add(ns)

            # 提取当前本体的命名空间
            ont_graph = rdflib.Graph()
            ont_graph.parse(data=ontology_path.read_text(encoding="utf-8"), format="xml")
            ont_namespaces = set()
            for prefix, ns in ont_graph.namespaces():
                ont_namespaces.add(str(ns))

            # 检查是否有交集
            if nt_namespaces and ont_namespaces:
                common = nt_namespaces & ont_namespaces
                if not common:
                    warnings.append(f"⚠️ IRI 命名空间不匹配：NT 文件使用 {list(nt_namespaces)[:3]}，本体使用 {list(ont_namespaces)[:3]}")
                else:
                    # 有交集但可能不是全部一致
                    nt_only = nt_namespaces - ont_namespaces
                    if nt_only:
                        warnings.append(f"⚠️ NT 文件包含本体未定义的命名空间：{list(nt_only)[:3]}")
        except Exception as e:
            warnings.append(f"⚠️ 无法验证 IRI 一致性：{e}")

    if n_triples == 0:
        warnings.append("⚠️ NT 文件不包含任何三元组")

    # 统一保存为 NT 格式
    store.abox_path.write_text(stored, encoding="utf-8")

    # 更新元数据
    size_bytes = len(stored.encode("utf-8"))
    store.update_abox({
        "generated_at": _now_str(),
        "triples": n_triples,
        "size_bytes": size_bytes,
        "source": "upload",
        "warnings": warnings
    })

    return {
        "ok": True,
        "triples": n_triples,
        "size_bytes": size_bytes,
        "format": source_format,
        "warnings": warnings
    }


# ---------- 端点控制 ----------

@router.post("/endpoint/start")
def endpoint_start():
    from backend.services.ontop_process import StartError, get_manager

    try:
        return get_manager().start()
    except StartError as e:
        raise HTTPException(507, {"message": str(e), "log_tail": e.log_tail})


@router.post("/endpoint/stop")
def endpoint_stop():
    from backend.services.ontop_process import get_manager

    return get_manager().stop()


@router.post("/endpoint/restart")
def endpoint_restart():
    """僵尸查询逃生舱：重启端点是唯一可靠的服务端取消。"""
    from backend.services.ontop_process import StartError, get_manager

    m = get_manager()
    m.stop()
    try:
        return m.start()
    except StartError as e:
        raise HTTPException(507, {"message": str(e), "log_tail": e.log_tail})
