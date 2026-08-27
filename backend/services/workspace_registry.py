"""工作空间注册表：data/workspaces.json + data/workspaces/<id>/ + QLever abox 指针。

多工作空间隔离（服务端全局"激活空间"模型）：
- 每个空间一套完整目录（project.json / jdbc.properties / files/ / logs/），
  ProjectStore 按 data_dir 参数化，get_store() 解析激活空间并缓存。
- data/abox-pointer.txt 内容 = 激活空间 abox.nt 的相对路径（POSIX 斜杠、无换行）。
  宿主 qlever 容器挂 data:/abox:ro，看守脚本比较「指针内容|目标 mtime」决定重建索引，
  指针缺失/目标不存在或空 → 清索引（0 三元组）。物化查询始终服务激活空间。
- legacy 迁移：旧单项目布局（data/project.json 等）整体 rename 进 workspaces/HD_SAAS/，
  幂等可重入（标记文件 + 逐项 skip 已搬），小文件先 zip 备份（绝不打包 584MB abox.nt）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
LEGACY_WS_NAME = "HD_SAAS"                       # 迁移目标空间名（用户拍板）
LEGACY_ITEMS = ("project.json", "jdbc.properties", "files", "logs")  # 迁移搬运清单（游离文件不动）
MIGRATION_MARKER = ".ws-migration-pending"


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class WorkspaceRegistry:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.ws_root = self.data_dir / "workspaces"
        self._lock = threading.Lock()
        self._state: dict | None = None  # {"active": id, "workspaces": [{"id", "created_at"}]}

    # ---------- 持久化（与 project_store 同款原子写） ----------

    @property
    def json_path(self) -> Path:
        return self.data_dir / "workspaces.json"

    @property
    def pointer_path(self) -> Path:
        return self.data_dir / "abox-pointer.txt"

    def _load(self) -> dict:
        if self.json_path.exists():
            try:
                state = json.loads(self.json_path.read_text(encoding="utf-8"))
                if isinstance(state.get("workspaces"), list):
                    return state
            except Exception:
                pass  # 损坏则走重建
        return {"active": None, "workspaces": []}

    def _flush(self) -> None:
        tmp = self.json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.json_path)

    # ---------- 查询 ----------

    def active_id(self) -> str:
        if self._state is None:
            self.init()
        return self._state["active"]

    def list_ids(self) -> list[str]:
        if self._state is None:
            self.init()
        return [w["id"] for w in self._state["workspaces"]]

    def list_all(self) -> list[dict]:
        """完整登记项（含 created_at）。"""
        if self._state is None:
            self.init()
        return [dict(w) for w in self._state["workspaces"]]

    def ws_dir(self, ws_id: str) -> Path:
        return self.ws_root / ws_id

    def exists(self, ws_id: str) -> bool:
        return ws_id in self.list_ids()

    # ---------- 变更（均持 _lock；停端点/evict 由调用方在锁外先做，见路由层） ----------

    def register_new(self, name: str) -> str:
        """校验名字、大小写不敏感查重、建目录骨架并登记。返回 id。"""
        from backend.services.project_store import ProjectStore  # 延迟 import 防环

        if not NAME_RE.match(name):
            raise ValueError("名字只允许字母、数字、下划线、连字符，1-32 位")
        with self._lock:
            lowers = [w["id"].lower() for w in self._state["workspaces"]]
            if name.lower() in lowers:
                raise FileExistsError(f"工作空间「{name}」已存在")
            ProjectStore(self.ws_dir(name))  # 自动建 files/uploads/logs 骨架
            self._state["workspaces"].append({"id": name, "created_at": _now_str()})
            self._flush()
            return name

    def set_active(self, ws_id: str) -> None:
        """切激活空间并写 QLever 指针。"""
        with self._lock:
            if ws_id not in [w["id"] for w in self._state["workspaces"]]:
                raise KeyError(f"工作空间不存在：{ws_id}")
            self._state["active"] = ws_id
            self._flush()
        self.write_abox_pointer(ws_id)

    def remove(self, ws_id: str) -> None:
        """删除空间。原子完成：evict store 缓存 → 除名；若删的是激活空间，
        同一次 flush 里改绑到剩余第一个（active 绝不悬空指向已删空间，杜绝窗口内
        get_store() 把目录重建出来）。指针先于 rmtree 写向新空间（看守不会对着
        被删目标先清一次索引）。最后一个空间抛 ValueError。"""
        from backend.services import project_store as ps  # 延迟 import 防环

        with self._lock:
            ids = [w["id"] for w in self._state["workspaces"]]
            if ws_id not in ids:
                raise KeyError(f"工作空间不存在：{ws_id}")
            if len(ids) <= 1:
                raise ValueError("至少保留一个工作空间，不能删除")
            ps.evict_store(ws_id)
            self._state["workspaces"] = [w for w in self._state["workspaces"] if w["id"] != ws_id]
            new_active = None
            if self._state["active"] == ws_id:
                new_active = self._state["workspaces"][0]["id"]
                self._state["active"] = new_active
            self._flush()
            if new_active is not None:
                self.write_abox_pointer(new_active)
        shutil.rmtree(self.ws_dir(ws_id))

    # ---------- QLever 指针 ----------

    def write_abox_pointer(self, ws_id: str) -> None:
        """写 data/abox-pointer.txt；内容相同则不重写（避免无谓 mtime 抖动）。"""
        content = f"workspaces/{ws_id}/files/abox.nt"
        try:
            if self.pointer_path.exists() and self.pointer_path.read_text(encoding="utf-8").strip() == content:
                return
        except Exception:
            pass
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pointer_path.write_text(content, encoding="utf-8")

    # ---------- 启动初始化（lifespan 第一件事，先于任何 get_store()） ----------

    def init(self) -> None:
        with self._lock:
            if self._state is not None:
                return  # 已初始化（并发/重复调用）
            self._migrate_legacy()
            self._state = self._load()
            if not self._state["workspaces"]:  # 全新装机：自建 default 保证 API 可用
                self.ws_root.mkdir(parents=True, exist_ok=True)
                self._state = {
                    "active": "default",
                    "workspaces": [{"id": "default", "created_at": _now_str()}],
                }
                self._flush()
            if self._state["active"] not in [w["id"] for w in self._state["workspaces"]]:
                self._state["active"] = self._state["workspaces"][0]["id"]
                self._flush()
        self.write_abox_pointer(self._state["active"])

    # ---------- legacy 迁移（幂等可重入） ----------

    def _migrate_legacy(self) -> None:
        """旧单项目布局 → workspaces/HD_SAAS/。触发：workspaces.json 不存在
        且（data/project.json 存在 或 迁移标记存在）。崩溃后重启重入：已搬项 skip。"""
        if self.json_path.exists():
            if (self.data_dir / MIGRATION_MARKER).exists():  # 异常态：json 已写但标记未删
                (self.data_dir / MIGRATION_MARKER).unlink(missing_ok=True)
                print("[workspace] 异常：workspaces.json 已存在但迁移标记未删，已清理", flush=True)
            return
        marker = self.data_dir / MIGRATION_MARKER
        if not (self.data_dir / "project.json").exists() and not marker.exists():
            return  # 全新装机，无 legacy

        print("[workspace] 检测到旧单项目布局，开始迁移 → workspaces/HD_SAAS/", flush=True)
        if not marker.exists():  # 首次进入才备份（重入不重复备份）
            self._backup_legacy_small_files()
            marker.write_text(_now_str(), encoding="utf-8")

        dst_root = self.ws_root / LEGACY_WS_NAME
        dst_root.mkdir(parents=True, exist_ok=True)
        for item in LEGACY_ITEMS:
            src = self.data_dir / item
            dst = dst_root / item
            if not src.exists():
                continue  # 已搬过（重入）
            if dst.exists():
                print(f"[workspace] 冲突：{src} 与 {dst} 同时存在，保留后者继续", flush=True)
                continue
            os.replace(src, dst)  # 同卷 rename，584MB 也瞬时
            print(f"[workspace] 已搬 {item}", flush=True)

        state = {"active": LEGACY_WS_NAME, "workspaces": [{"id": LEGACY_WS_NAME, "created_at": _now_str()}]}
        tmp = self.json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.json_path)
        marker.unlink(missing_ok=True)
        print(f"[workspace] 迁移完成，active={LEGACY_WS_NAME}", flush=True)

    def _backup_legacy_small_files(self) -> None:
        """迁移前 zip 备份小文件（绝不打包 abox.nt；日志 >50MB 只留末 2MB）。"""
        ts = time.strftime("%Y%m%d-%H%M%S")
        zip_path = self.data_dir / f"backup-pre-ws-migration-{ts}.zip"
        members = [
            self.data_dir / "project.json",
            self.data_dir / "jdbc.properties",
            self.data_dir / "files/ontology.rdf",
            self.data_dir / "files/mapping.obda",
            self.data_dir / "files/builder-draft.json",
            self.data_dir / "files/mapping-draft.json",
        ]
        uploads = self.data_dir / "files/uploads"
        if uploads.exists():
            members.extend(p for p in uploads.rglob("*") if p.is_file())
        log = self.data_dir / "logs/ontop.log"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in members:
                    if p.exists():
                        zf.write(p, p.relative_to(self.data_dir).as_posix())
                if log.exists() and log.stat().st_size > 0:
                    if log.stat().st_size > 50 * 1024 * 1024:
                        with open(log, "rb") as f:
                            f.seek(-2 * 1024 * 1024, os.SEEK_END)
                            tail = f.read()
                        zf.writestr("logs/ontop.log.tail", tail)
                    else:
                        zf.write(log, "logs/ontop.log")
            print(f"[workspace] 迁移前备份：{zip_path.name}", flush=True)
        except Exception as e:
            print(f"[workspace] 备份失败（不阻断迁移）：{e}", flush=True)


_registry: WorkspaceRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> WorkspaceRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                from backend.config import get_config

                _registry = WorkspaceRegistry(get_config().paths.data_dir)
    return _registry
