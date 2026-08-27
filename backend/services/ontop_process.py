"""Ontop 子进程生命周期：spawn / 就绪轮询 / 杀树 / 看门狗 / 日志尾。

平台适配：
- Windows（本机应急）：ontop17.bat，杀树 taskkill /T /F
- Linux（容器，正式）：bin/ontop，杀树 psutil 子进程树
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from backend.config import AppConfig, get_config
from backend.services import project_store as ps


class StartError(Exception):
    def __init__(self, message: str, log_tail: str = ""):
        super().__init__(message)
        self.log_tail = log_tail


class OntopProcess:
    def __init__(self, cfg: AppConfig | None = None):
        self.cfg = cfg or get_config()
        self.proc: subprocess.Popen | None = None
        self._log_fh = None
        self._watchdog: threading.Thread | None = None
        self._running_store: ps.ProjectStore | None = None  # 当前端点归属的空间（启动时捕获）

    # ---------- 启动 ----------

    def start(self) -> dict:
        with op_lock:  # 与 stop/工作空间切换互斥：防就绪轮询期间切换导致 RUNNING 写错空间
            store = ps.get_store()  # 此刻的激活空间（函数内解析，不随切换漂移）
            ok, why = store.can_start()
            if not ok:
                raise StartError(f"无法启动：{why}")
            if self.is_running():
                self.stop()

            port = self.cfg.ontop.port
            if _port_in_use(port):
                raise StartError(f"端口 {port} 已被占用（可能有残留 ontop 进程）")

            t0 = time.time()
            log_path = store.ontop_log
            self._log_fh = open(log_path, "ab")
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            cmd = [
                str(self.cfg.ontop.launcher_path), "endpoint",
                "--mapping", str(store.mapping_path),
                "--properties", str(store.properties_path),
                "--ontology", str(store.ontology_path),
                "--port", str(port),
            ]
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=str(store.data_dir),
                    stdout=self._log_fh, stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            except Exception as e:
                raise StartError(f"启动命令失败：{e}\n命令：{' '.join(cmd)}")

            # 就绪轮询：GET / 出 welcome 页即就绪
            deadline = time.time() + self.cfg.ontop.boot_timeout_s
            while time.time() < deadline:
                if self.proc.poll() is not None:  # 进程提前退出
                    raise StartError(
                        f"Ontop 启动即退出（exit={self.proc.returncode}），多半是映射/连接问题",
                        log_tail=_tail(log_path, 40),
                    )
                if _http_ok(port):
                    boot_ms = int((time.time() - t0) * 1000)
                    store.set_running(self.proc.pid, port)
                    self._running_store = store
                    self._start_watchdog(store)
                    return {"status": "RUNNING", "pid": self.proc.pid, "port": port, "boot_ms": boot_ms}
                time.sleep(0.5)

            self.kill_tree()
            raise StartError(f"就绪超时（>{self.cfg.ontop.boot_timeout_s}s）", log_tail=_tail(log_path, 40))

    def ensure(self) -> dict:
        """幂等启动：在跑就直接返回，没在跑才启动。

        查询路由懒启动用（虚拟路线 /sparql 来了端点没跑就自动拉起）。
        op_lock 串行化保证并发冷启动只触发一次真实 start，后来的看到在跑即返回。
        """
        with op_lock:  # RLock，start() 内部再拿不冲突
            if self.is_running():
                return {"status": "RUNNING", "pid": self.proc.pid, "port": self.cfg.ontop.port, "boot_ms": 0}
            return self.start()

    # ---------- 停止 ----------

    def stop(self) -> dict:
        with op_lock:
            # 状态写回端点归属的空间（路由层保证先 stop 后切空间，二者通常一致）
            store = self._running_store or ps.get_store()
            if not self.proc:
                store.set_stopped()
                return {"status": "stopped", "note": "本就没在跑"}
            pid = self.proc.pid
            self.kill_tree()
            store.set_stopped()
            self._running_store = None
            return {"status": "stopped", "pid": pid}

    def kill_tree(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            else:
                import psutil

                try:
                    parent = psutil.Process(proc.pid)
                    kids = parent.children(recursive=True)
                    for k in kids:
                        k.kill()
                    parent.kill()
                    psutil.wait_procs(kids + [parent], timeout=5)
                except psutil.NoSuchProcess:
                    pass
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        # 等端口释放，最多 5s
        deadline = time.time() + 5
        while time.time() < deadline and _port_in_use(self.cfg.ontop.port):
            time.sleep(0.3)
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    # ---------- 健康 ----------

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _start_watchdog(self, store: "ps.ProjectStore") -> None:
        def _watch():
            while self.proc and self.proc.poll() is None:
                time.sleep(1)
            if self.proc is not None:  # 非主动 stop（stop 会先置 None）
                store.set_error(f"Ontop 意外退出（exit={self.proc.returncode}）")
                store.set_stopped()
                self.proc = None

        self._watchdog = threading.Thread(target=_watch, daemon=True)
        self._watchdog.start()


# ---------- 工具 ----------

def _http_ok(port: int) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.5)
        return r.status_code < 500
    except Exception:
        return False


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _tail(path: Path, n: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


# 端点生命周期互斥锁：start/stop 与工作空间切换/新建/删除共用，
# 防「start 就绪轮询（≤60s）期间切空间 → RUNNING 写进新空间」。可重入（start 内部会调 stop）。
op_lock = threading.RLock()

# 单例（进程内共享）
_manager: OntopProcess | None = None
_manager_lock = threading.Lock()


def get_manager() -> OntopProcess:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = OntopProcess()
        return _manager
