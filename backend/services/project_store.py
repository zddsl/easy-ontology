"""项目存储与状态机：./data 是唯一事实源。

状态：EMPTY → FILES_LOADED(有本体或映射) → READY(本体+映射+数据源齐) → RUNNING
ERROR 为运行期标签（last_error 记录原因），TESTED 不是状态（ping 结果只是缓存元数据）。
所有变迁原子写 project.json（temp + rename），杜绝半写状态。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os

EMPTY = "EMPTY"
FILES_LOADED = "FILES_LOADED"
READY = "READY"
RUNNING = "RUNNING"

def _now_str() -> str:
    """当前时间字符串，读 TZ_OFFSET 环境变量（整数小时，默认 8 = UTC+8）。"""
    offset = int(os.environ.get("TZ_OFFSET", "8"))
    tz = timezone(timedelta(hours=offset))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

class ProjectStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.files_dir = self.data_dir / "files"
        self.uploads_dir = self.files_dir / "uploads"
        self.logs_dir = self.data_dir / "logs"
        for d in (self.data_dir, self.files_dir, self.uploads_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._state: dict = self._load_or_init()

    # ---------- 持久化 ----------

    @property
    def project_json(self) -> Path:
        return self.data_dir / "project.json"

    def _load_or_init(self) -> dict:
        if self.project_json.exists():
            try:
                return json.loads(self.project_json.read_text(encoding="utf-8"))
            except Exception:
                pass  # 损坏则重建
        return {
            "state": EMPTY,
            "ontology": None,       # {filename, saved_at, format}
            "mapping": None,        # {filename, saved_at, n_mappings}
            "datasource": None,     # {db_type, host, port, database, user, password}
            "last_ping": None,      # {ok, ms, error, at}
            "ontop": None,          # {pid, port, started_at} 运行期信息
            "abox": None,           # {generated_at, triples, size_bytes} 物化产物
            "last_error": None,
            "updated_at": None,
        }

    def _flush(self) -> None:
        self._state["updated_at"] = _now_str()
        tmp = self.project_json.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.project_json)

    def _update(self, **fields) -> None:
        with self._lock:
            self._state.update(fields)
            self._recalc_state()
            self._flush()

    def _recalc_state(self) -> None:
        """按文件/数据源齐备度收敛状态（RUNNING 由 ontop_process 显式设置，不在此推导）。"""
        if self._state.get("state") == RUNNING:
            return
        has_files = bool(self._state.get("ontology") and self._state.get("mapping"))
        has_ds = bool(self._state.get("datasource"))
        self._state["state"] = READY if (has_files and has_ds) else FILES_LOADED if (has_files or self._state.get("ontology") or self._state.get("mapping")) else EMPTY

    # ---------- 文件路径 ----------

    @property
    def ontology_path(self) -> Path:
        return self.files_dir / "ontology.rdf"

    @property
    def mapping_path(self) -> Path:
        return self.files_dir / "mapping.obda"

    @property
    def properties_path(self) -> Path:
        return self.data_dir / "jdbc.properties"

    @property
    def ontop_log(self) -> Path:
        return self.logs_dir / "ontop.log"

    # ---------- 写入接口 ----------

    def save_ontology(self, rdf_xml: str, original_name: str, source_format: str, origin: str = "upload") -> None:
        p = self.ontology_path
        p.write_text(rdf_xml, encoding="utf-8")
        (self.uploads_dir / "ontology.upload").write_text(rdf_xml, encoding="utf-8")  # 原件备份（已是RDF/XML）
        self._update(
            ontology={"filename": original_name, "saved_at": _now_str(), "format": source_format, "origin": origin},
            last_error=None,
        )

    def save_mapping(self, obda_text: str, original_name: str, n_mappings: int, origin: str = "upload") -> None:
        self.mapping_path.write_text(obda_text, encoding="utf-8")
        self._update(
            mapping={"filename": original_name, "saved_at": _now_str(), "n_mappings": n_mappings, "origin": origin},
            last_error=None,
        )

    def clear_ontology(self) -> None:
        if self.ontology_path.exists():
            self.ontology_path.unlink()
        backup = self.uploads_dir / "ontology.upload"
        if backup.exists():
            backup.unlink()
        if self.builder_draft_path.exists():
            self.builder_draft_path.unlink()
        self._update(ontology=None, last_error=None)

    def clear_mapping(self) -> None:
        if self.mapping_path.exists():
            self.mapping_path.unlink()
        if self.mapping_draft_path.exists():
            self.mapping_draft_path.unlink()
        self._update(mapping=None, last_error=None)

    def save_datasource(self, form: dict) -> None:
        from backend.services.properties_builder import build_properties_text

        text = build_properties_text(form)
        self.properties_path.write_text(text, encoding="utf-8")
        self._update(datasource=form, last_ping=None, last_error=None)

    def set_running(self, pid: int, port: int) -> None:
        self._update(state=RUNNING, ontop={"pid": pid, "port": port, "started_at": _now_str()})

    def set_stopped(self) -> None:
        with self._lock:
            self._state["ontop"] = None
            self._recalc_state()
            self._flush()

    def set_error(self, message: str) -> None:
        with self._lock:
            self._state["last_error"] = message
            self._flush()

    def record_ping(self, result: dict) -> None:
        self._update(last_ping=result)

    def update_abox(self, meta: dict | None) -> None:
        """更新 ABox 物化元数据（None 表示清除）。"""
        self._update(abox=meta)

    @property
    def abox_path(self) -> Path:
        return self.files_dir / "abox.nt"

    @property
    def builder_draft_path(self) -> Path:
        """本体搭建器草稿（工作区文件，不进 project.json、不触发状态重算）。"""
        return self.files_dir / "builder-draft.json"

    @property
    def mapping_draft_path(self) -> Path:
        """映射搭建器草稿（工作区文件，不进 project.json、不触发状态重算）。"""
        return self.files_dir / "mapping-draft.json"

    # ---------- 读取 ----------

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))  # 深拷贝

    @property
    def state(self) -> str:
        return self._state.get("state", EMPTY)

    def can_start(self) -> tuple[bool, str]:
        snap = self.snapshot()
        missing = []
        if not snap.get("ontology"):
            missing.append("本体")
        if not snap.get("mapping"):
            missing.append("映射")
        if not snap.get("datasource"):
            missing.append("数据源")
        if missing:
            return False, "缺少：" + "、".join(missing)
        return True, ""


_stores: dict[str, ProjectStore] = {}
_stores_lock = threading.Lock()


def get_store() -> ProjectStore:
    """激活工作空间的 store（workspace_registry 决定激活；延迟 import 防环）。

    红线：lifespan 必须先 get_registry().init()（含 legacy 迁移）再调本函数。
    """
    from backend.services import workspace_registry as wr

    ws_id = wr.get_registry().active_id()
    with _stores_lock:
        st = _stores.get(ws_id)
        if st is None:
            st = ProjectStore(wr.get_registry().ws_dir(ws_id))
            _stores[ws_id] = st
        return st


def evict_store(ws_id: str) -> None:
    """踢掉某工作空间的缓存 store（删除该空间前必须先调用，防在途写复活目录）。"""
    with _stores_lock:
        _stores.pop(ws_id, None)
