"""ABox 物化路由：POST /api/materialize（启动后台 job，秒回），
GET /api/materialize/job（进度），POST /api/materialize/cancel（取消），
GET /api/materialize/status、/index-status、/preview（流式预览），
DELETE /api/materialize（清除）。"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, HTTPException

from backend.services import abox_gen
from backend.services import materialize as mat
from backend.services.project_store import get_store

router = APIRouter(prefix="/api", tags=["materialize"])

# NT 行的轻量解析：主语(<IRI>|_:\S+) 谓语(<IRI>) 宾语(任意，含字面量)
_NT_LINE = re.compile(r"^(<[^>]*>|_:\S+)\s+(<[^>]*>)\s+(.+?)\s*\.\s*$")

_PREVIEW_SCAN_TIMEOUT_S = 30  # 大文件（GB 级）搜索的软上限，超时返回已扫到的部分


@router.get("/materialize/preview")
def abox_preview(q: str = "", limit: int = 100, offset: int = 0):
    """流式预览 abox.nt：q=不区分大小写子串过滤，offset=跳过前 N 条匹配（分页）。

    逐行扫描、命中 limit+1 即停，GB 级文件不占内存；搜索最坏扫全文件，
    超 30s 截断并置 truncated_scan=True。
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    store = get_store()
    p = store.abox_path
    if not p.exists():
        return {"exists": False, "meta": store.snapshot().get("abox"), "lines": []}

    q_lower = q.strip().lower()
    out: list[dict] = []
    matched = 0
    scanned = 0
    truncated_scan = False
    t0 = time.time()
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            scanned += 1
            if time.time() - t0 > _PREVIEW_SCAN_TIMEOUT_S:
                truncated_scan = True
                break
            if q_lower and q_lower not in raw.lower():
                continue
            matched += 1
            if matched <= offset:
                continue
            line = raw.strip()
            m = _NT_LINE.match(line)
            out.append({
                "n": scanned,  # 文件内行号（含未匹配行）
                "s": m.group(1) if m else None,
                "p": m.group(2) if m else None,
                "o": (m.group(3) if m else None) or line[:500],
                "raw": line[:2000],
            })
            if len(out) > limit:
                break

    has_more = len(out) > limit
    return {
        "exists": True,
        "meta": store.snapshot().get("abox"),
        "size_bytes": p.stat().st_size,
        "q": q.strip(),
        "offset": offset,
        "lines": out[:limit],
        "has_more": has_more,
        "truncated_scan": truncated_scan,
        "scanned": scanned,
    }


@router.post("/materialize")
async def do_materialize():
    """启动 ABox 生成 job：同步解析/编译映射（不支持构造→400），后台并行生成。"""
    try:
        result = await mat.generate_abox()
    except (mat.MaterializeError, abox_gen.UnsupportedMappingError, abox_gen.AboxGenError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "phase": "running", **result}


@router.get("/materialize/job")
def job_status():
    """生成 job 进度（前端 3s 轮询）；无 job 返回 {phase: null}。"""
    return mat.get_job() or {"phase": None}


@router.post("/materialize/cancel")
def cancel():
    """取消进行中的生成（杀查询子进程）；无可取消任务→404。"""
    if not mat.cancel_job():
        raise HTTPException(404, "没有进行中的生成任务")
    return {"ok": True}


@router.get("/materialize/status")
def status():
    """返回 abox 元信息（未生成时返回 None）。"""
    return {"abox": get_store().snapshot().get("abox")}


@router.get("/materialize/index-status")
async def index_status():
    """ABox 索引状态：入库三元组数 vs QLever 已索引数。

    上传/物化只保证 NT 落盘，索引由看守进程后台重建——前端轮询本接口，
    state=ready 才代表物化查询可用：
      no_abox    无 ABox
      rebuilding QLever 服务不可达（多半是重建窗口被看守杀掉）
      syncing    服务在跑但索引数还没对上（旧索引/重建中）
      ready      已索引数 == 入库数
    """
    import httpx

    from backend.config import get_config

    abox = get_store().snapshot().get("abox")
    expected = int((abox or {}).get("triples") or 0)
    indexed = None
    if expected:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0)) as client:
                indexed = await mat._qlever_count(client, get_config().rdf_store.sparql_url)
        except Exception:
            indexed = None
    if not expected:
        state = "no_abox"
    elif indexed is None:
        state = "rebuilding"
    elif indexed == expected:
        state = "ready"
    else:
        state = "syncing"
    return {"state": state, "abox_triples": expected, "indexed": indexed}


@router.delete("/materialize")
async def clear():
    """清除 ABox：把 abox.nt 置空（看守进程随之重建 0 三元组索引）；清 project.json.abox。"""
    import asyncio

    if not await asyncio.to_thread(mat.acquire_job):
        raise HTTPException(409, "已有一次物化/清除在进行中，请等它结束")
    try:
        store = get_store()
        p = store.abox_path
        if p.exists():
            p.write_text("", encoding="utf-8")  # 置空而非删除：看守只认文件 mtime 变化

        try:
            await mat.wait_count_zero(timeout_s=120)
        except Exception:
            pass  # QLever 不可达也允许清元数据

        store.update_abox(None)
    finally:
        mat.unlock_job()
    return {"ok": True}
