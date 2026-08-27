"""JDBC 连通测试：容器内编译/调用 tools/DbPing，输出一行 JSON。

测试连接仅 ping（不做 ontop validate），1-2s 出结果。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from backend.config import get_config

_TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"


def _ensure_compiled() -> None:
    """DbPing.class 不存在则用容器内 javac 编译（--release 8 双兼容）。"""
    class_file = _TOOLS_DIR / "DbPing.class"
    java_file = _TOOLS_DIR / "DbPing.java"
    if class_file.exists() and class_file.stat().st_mtime >= java_file.stat().st_mtime:
        return
    subprocess.run(
        [get_config().tools.javac_bin, "--release", "8", str(java_file)],
        cwd=_TOOLS_DIR, capture_output=True, timeout=60, check=True,
    )


def ping(form: dict) -> dict:
    """返回 {ok, ms, error?}。异常文案原样透出给前端。"""
    from backend.services.properties_builder import build_jdbc_url

    cfg = get_config()
    t = cfg.db_types[form["db_type"]]
    cp_sep = ";" if sys.platform == "win32" else ":"
    classpath = cp_sep.join(filter(None, [
        str(_TOOLS_DIR),
        t.ping_classpath or cfg.tools.dbping_classpath_default,
    ]))
    cmd = [
        cfg.tools.java_bin, "-Dfile.encoding=UTF-8", "-cp", classpath, "DbPing",
        build_jdbc_url(form), form["user"], form["password"], t.driver,
    ]
    try:
        _ensure_compiled()
        r = subprocess.run(cmd, capture_output=True, timeout=15, text=True, encoding="utf-8", errors="replace")
        line = (r.stdout or "").strip().splitlines()
        if line:
            try:
                return json.loads(line[-1])
            except json.JSONDecodeError:
                pass
        return {"ok": False, "error": (r.stderr or r.stdout or "DbPing 无输出")[:500]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "连接超时（>15s）"}
    except Exception as e:  # javac 失败等
        return {"ok": False, "error": f"DbPing 执行失败：{e}"}
