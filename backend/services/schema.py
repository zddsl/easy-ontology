"""JDBC 库表元数据：容器内编译/调用 tools/DbSchema，解析一行 JSON。

映射搭建页的表/字段下拉数据源；结果按数据源五元组做 TTL 缓存（大库反复枚举很慢）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from backend.config import get_config

_TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"
# 长缓存：key 是数据源五元组，换数据源=换 key 自然失效；库表改了用 ?refresh=1 手动强刷。
# 容器重启缓存清空，重启后首次访问重读一次属正常代价。
_CACHE_TTL = 7 * 86400.0
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _ensure_compiled() -> None:
    """DbSchema.class 不存在则用容器内 javac 编译（--release 8 双兼容）。"""
    class_file = _TOOLS_DIR / "DbSchema.class"
    java_file = _TOOLS_DIR / "DbSchema.java"
    if class_file.exists() and class_file.stat().st_mtime >= java_file.stat().st_mtime:
        return
    subprocess.run(
        [get_config().tools.javac_bin, "--release", "8", str(java_file)],
        cwd=_TOOLS_DIR, capture_output=True, timeout=60, check=True,
    )


def schema_scope(form: dict) -> tuple[str, str]:
    """(catalog, schema) 收窄元数据范围：MySQL catalog=库名；达梦 schema=用户名大写。"""
    if form.get("db_type") == "dm8":
        return "", form["user"].upper()
    return form.get("database") or "", ""


def fetch_schema(form: dict) -> dict:
    """返回 {ok, tables?, ms, error?}。异常文案原样透出给前端。"""
    from backend.services.properties_builder import build_jdbc_url

    cfg = get_config()
    t = cfg.db_types[form["db_type"]]
    cp_sep = ";" if sys.platform == "win32" else ":"
    classpath = cp_sep.join(filter(None, [
        str(_TOOLS_DIR),
        t.ping_classpath or cfg.tools.dbping_classpath_default,
    ]))
    catalog, schema = schema_scope(form)
    cmd = [
        cfg.tools.java_bin, "-Dfile.encoding=UTF-8", "-cp", classpath, "DbSchema",
        build_jdbc_url(form), form["user"], form["password"], t.driver,
        catalog, schema,
    ]
    try:
        _ensure_compiled()
        r = subprocess.run(cmd, capture_output=True, timeout=200, text=True, encoding="utf-8", errors="replace")
        line = (r.stdout or "").strip().splitlines()
        if line:
            try:
                return json.loads(line[-1])
            except json.JSONDecodeError:
                pass
        return {"ok": False, "error": (r.stderr or r.stdout or "DbSchema 无输出")[:500]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "读取表结构超时（>200s），数据库表数量可能过多"}
    except Exception as e:  # javac 失败等
        return {"ok": False, "error": f"DbSchema 执行失败：{e}"}


def get_schema(form: dict, refresh: bool = False) -> dict:
    """fetch_schema 的缓存版（key=数据源五元组；同库改表后用 refresh=True 强制重取）。"""
    key = json.dumps({k: form.get(k) for k in ("db_type", "host", "port", "database", "user")}, sort_keys=True)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not refresh and time.time() - hit[0] < _CACHE_TTL:
            return {**hit[1], "cached": True}
    result = fetch_schema(form)
    if result.get("ok"):
        with _cache_lock:
            _cache[key] = (time.time(), result)
    return result
