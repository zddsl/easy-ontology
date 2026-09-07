"""ABox 物化 job 框架：自写生成引擎（abox_gen）+ QLever 索引接力。

流程：POST 抢位 → 同步 prepare（解析/编译/加宽，不支持构造直接 400）
→ 后台线程并行生成 part → 合并原子替换 abox.nt → 看守进程重建索引
→ 前端轮询 index-status 等 ready。本模块只管 job 生命周期与互斥，
生成逻辑在 abox_gen.py。
"""
import asyncio
import threading
import time

import httpx

from backend.config import get_config
from backend.services import abox_gen
from backend.services.project_store import get_store


class MaterializeError(Exception):
    """物化失败"""


# 物化/清除互斥位：进行中禁止切换/删除工作空间（防 rmtree 正在写的 abox）。
# 抢位必须在 ontop_process.op_lock 内进行（工作空间的切换/删除在 op_lock 锁内
# 复查 is_busy），两侧互斥闭环；抢位本身经 to_thread 跑——op_lock 可能被端点
# stop 长持（杀树+等端口 ~20s），不能在事件循环上直接等它。
_busy = threading.Lock()


def is_busy() -> bool:
    return _busy.locked()


def acquire_job() -> bool:
    """在 op_lock 保护下尝试占用物化位（同步阻塞，调用方应放线程池）。"""
    from backend.services.ontop_process import op_lock

    with op_lock:
        return _busy.acquire(blocking=False)


def try_lock_job() -> bool:
    """不走 op_lock 的直接抢位（仅测试/内部用，正常入口用 acquire_job）。"""
    return _busy.acquire(blocking=False)


def unlock_job() -> None:
    _busy.release()


# ---------- 后台 job 状态 ----------

class _Job:
    """一次生成任务的运行态（字段简单赋值，读侧 get_job 汇总）。"""

    def __init__(self, store):
        self.phase = "running"          # running | merging | auditing | done | error | canceled
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.workers_total = 0
        self.workers_done = 0
        self.triples: list[int] = []    # 每 worker 的实时计数（sum 即总进度）
        self.procs: dict = {}           # idx → Popen（取消用；abox_gen 维护）
        self.cancel_event = threading.Event()
        self.error: str | None = None
        self.result: dict | None = None
        self.log_path = store.data_dir / "logs" / "materialize.log"


_current: _Job | None = None


def get_job() -> dict | None:
    """job 快照（前端 3s 轮询）；无 job 返回 None。"""
    job = _current
    if job is None:
        return None
    return {
        "phase": job.phase,
        "started_at": job.started_at,
        "elapsed_s": round((job.finished_at or time.time()) - job.started_at, 1),
        "workers_total": job.workers_total,
        "workers_done": job.workers_done,
        "triples_done": sum(job.triples or []),
        "error": job.error,
        "result": job.result,
        "log_tail": _log_tail(job.log_path),
    }


def _log_tail(path, lines: int = 40) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return ""


def _log(job: _Job, msg: str) -> None:
    try:
        job.log_path.parent.mkdir(exist_ok=True)
        with open(job.log_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%F %T')}] {msg}\n")
    except OSError:
        pass


def cancel_job() -> bool:
    """取消进行中的 job（杀 SqlStream 子进程）；不可取消返回 False。"""
    job = _current
    if job is None or job.phase not in ("running", "merging", "auditing"):
        return False
    job.cancel_event.set()
    for p in list(job.procs.values()):
        try:
            p.kill()  # SqlStream 是单 java 进程，kill 即断（JDBC 连接随进程释放）
        except Exception:
            pass
    return True


def _job_worker(prepared: dict, store, job: _Job) -> None:
    global _current
    try:
        _log(job, f"开始生成：{job.workers_total} 条映射，数据源 "
                  f"{prepared['ds'].get('db_type')} {prepared['ds'].get('host')}")
        result = abox_gen.run(prepared, store, job, job.cancel_event, job.log_path)
        job.result = result
        job.phase = "done"
        _log(job, f"完成：{result['triples']} 三元组 / {result['size_bytes']} 字节 / "
                  f"{result['elapsed_ms']}ms；QLever 索引后台重建中")
    except abox_gen.CanceledError:
        job.phase = "canceled"
        _log(job, "已取消")
    except Exception as e:
        job.phase = "error"
        job.error = str(e)
        _log(job, f"失败：{e}")
    finally:
        try:
            abox_gen.cleanup(store)
        except Exception:
            pass
        job.finished_at = time.time()
        unlock_job()


async def generate_abox() -> dict:
    """启动生成 job：同步 prepare（错误直接抛→400），成功则立即返回。"""
    global _current
    if not await asyncio.to_thread(acquire_job):
        raise MaterializeError("已有一次物化/清除在进行中，请等它结束")
    store = get_store()
    try:
        prepared = await asyncio.to_thread(abox_gen.prepare, store)
    except Exception:
        unlock_job()
        raise
    job = _Job(store)
    job.workers_total = len(prepared["compiled"])
    job.triples = [0] * job.workers_total
    _current = job
    _log(job, "job 启动")
    t = threading.Thread(target=_job_worker, args=(prepared, store, job), daemon=True)
    t.start()
    return {"started": True, "workers_total": job.workers_total}


COUNT_QUERY = "SELECT (COUNT(*) AS ?n) WHERE {?s ?p ?o}"


async def _qlever_count(client: httpx.AsyncClient, url: str) -> int:
    r = await client.post(url, data={"query": COUNT_QUERY},
                          headers={"Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return int(r.json()["results"]["bindings"][0]["n"]["value"])


async def wait_count_zero(timeout_s: int = 120) -> None:
    """清除后等索引归零（空 abox.nt 重建很快，秒级）。"""
    url = get_config().rdf_store.sparql_url
    deadline = time.time() + timeout_s
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        while time.time() < deadline:
            try:
                if await _qlever_count(client, url) == 0:
                    return
            except Exception:
                pass  # 重建窗口服务重启
            await asyncio.sleep(3)
    raise MaterializeError(f"等待 QLever 清除超时（{timeout_s}s）")
