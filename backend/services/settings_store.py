"""全局平台设置持久化（data/settings.json，不按工作空间隔离）。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from backend.config import get_config

_lock = threading.Lock()


def _path() -> Path:
    return Path(get_config().paths.data_dir) / "settings.json"


def get_settings() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def update_settings(**fields) -> dict:
    with _lock:
        current = get_settings()
        for k, v in fields.items():
            if v is None or v == "":
                current.pop(k, None)
            else:
                current[k] = v
        tmp = _path().with_suffix(".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _path())
        return current


def get_llm_key() -> str | None:
    return get_settings().get("llm_api_key") or None
