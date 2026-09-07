"""物化后数据质量体检：边映射悬空键统计 + 数据属性脏值统计。

背景：生成器的 INNER JOIN 验货把脏外键行静默挡在图外（正确，但用户无感）；
本模块在 abox.nt 落盘后对源库跑只读统计，把"丢了多少、为什么丢"写进
job 结果与物化日志。约定：
- 任何一条统计失败只记 note 并继续，绝不影响已落盘的物化产物；
- 边映射：把 source SQL 的 INNER JOIN 改写为 LEFT JOIN 得"参与行数"，
  原式计数为"入图行数"，差值拆成空键（模板列 NULL）与悬空（键非空但对不上）；
- 数据属性：GROUP BY 取 NULL / 空串 / 出现最多的前 3 个值（CLOB 等不支持
  分组的列自动降级为跳过并记 note）；
- 手写映射若不含 INNER JOIN（如 WHERE EXISTS 风格）无法推算总量，同样跳过。
"""
from __future__ import annotations

import heapq
import re
import subprocess
import threading
import time
from pathlib import Path

from backend.config import get_config
from backend.services.abox_gen import (
    AboxGenError,
    CanceledError,
    NULL_MARK,
    SEP,
    _tools_dir,
    _unescape,
    err_tail,
)
from backend.services.properties_builder import build_jdbc_url

_QUERY_DEADLINE_S = 600  # 单条统计查询上限（COUNT/GROUP BY 在大表上不便宜）


def _log(log_path: Path, msg: str) -> None:
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%F %T')}] {msg}\n")
    except OSError:
        pass


def _sqlstream(sql: str, ds: dict, parts_dir: Path, cancel: threading.Event):
    """跑一条只读 SQL，产出行（list[str|None]）。generator 用完务必耗尽/关闭。"""
    cfg = get_config()
    tcfg = cfg.db_types[ds["db_type"]]
    classpath = ":".join([str(_tools_dir()), tcfg.ping_classpath or cfg.tools.dbping_classpath_default])
    sql_file = parts_dir / ".audit.sql"
    sql_file.write_text(sql, encoding="utf-8")
    err_file = parts_dir / ".audit.err"
    cmd = [cfg.tools.java_bin, "-Dfile.encoding=UTF-8", "-cp", classpath,
           "SqlStream", build_jdbc_url(ds), ds["user"], ds["password"],
           tcfg.driver, str(sql_file)]
    proc = None
    try:
        with open(err_file, "wb") as ef:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef,
                                    cwd=str(_tools_dir()))
            header = proc.stdout.readline()
            if not header:
                raise AboxGenError(f"SqlStream 无输出（{err_tail(err_file)}）")
            deadline = time.time() + _QUERY_DEADLINE_S
            for raw in proc.stdout:
                if cancel.is_set():
                    raise CanceledError()
                if time.time() > deadline:
                    raise AboxGenError(f"统计查询超过 {_QUERY_DEADLINE_S}s 未完成")
                yield [None if fb == NULL_MARK.encode()
                       else _unescape(fb.decode("utf-8", "replace"))
                       for fb in raw.rstrip(b"\n").split(SEP.encode())]
            rc = proc.wait()
            if rc != 0:
                raise AboxGenError(f"统计查询失败（退出码 {rc}）：{err_tail(err_file)}")
            proc = None  # 正常收尾
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()


def _count(sql: str, ds: dict, parts_dir: Path, cancel: threading.Event) -> int:
    for row in _sqlstream(sql, ds, parts_dir, cancel):
        return int(row[0])
    raise AboxGenError("统计查询没有返回行")


def audit(prepared: dict, store, state, cancel: threading.Event, log_path: Path) -> dict:
    """物化成功后的体检入口。永不抛业务异常（CanceledError 除外）。"""
    ds: dict = prepared["ds"]
    compiled: list[dict] = prepared["compiled"]
    parts_dir = store.files_dir / ".abox-parts"
    parts_dir.mkdir(exist_ok=True)
    t0 = time.time()
    edges: list[dict] = []
    literals: list[dict] = []
    notes: list[str] = []

    def guarded(note: str, fn):
        """单条统计的兜底：失败记 note 继续；取消照常上抛。"""
        try:
            return fn()
        except CanceledError:
            raise
        except Exception as e:
            notes.append(f"{note}：{e}")
            _log(log_path, f"体检跳过 {note}：{e}")
            return None

    # ---------- 边映射：INNER JOIN → LEFT JOIN 对账 ----------
    for c in compiled:
        obj_props = [p for p in c["props"] if p["kind"] == "obj"]
        if not obj_props:
            continue
        sql: str = c["sql"]
        left = re.sub(r"\bINNER\s+JOIN\b", "LEFT JOIN", sql, flags=re.I)
        if left == sql:
            notes.append(f"{c['id']}：source 无 INNER JOIN，无法推算参与行数")
            continue
        key_cols = list(dict.fromkeys(
            list(c["subj"][2]) + [col for p in obj_props for col in p["tpl"][2]]))
        where_null = " OR ".join(f"{col} IS NULL" for col in key_cols)
        total = guarded(c["id"], lambda: _count(
            f"SELECT COUNT(*) AS N FROM ({left}) t", ds, parts_dir, cancel))
        if total is None:
            continue
        kept = guarded(c["id"], lambda: _count(
            f"SELECT COUNT(*) AS N FROM ({sql}) t", ds, parts_dir, cancel))
        null_key = guarded(c["id"], lambda: _count(
            f"SELECT COUNT(*) AS N FROM ({left}) t WHERE {where_null}", ds, parts_dir, cancel))
        if kept is None or null_key is None:
            continue
        entry = {
            "mapping": c["id"],
            "preds": [p["pred"] for p in obj_props],
            "total": total,
            "kept": kept,
            "null_key": null_key,
            "dangling": total - kept - null_key,
        }
        edges.append(entry)

        def _local(iri: str) -> str:
            return iri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]

        _log(log_path,
             f"体检[边] {', '.join(_local(p['pred']) for p in obj_props)}"
             f"（{c['id']}）：参与 {total:,} · 入图 {kept:,} · 空键 {null_key:,} · 悬空 {entry['dangling']:,}")

    # ---------- 数据属性：NULL / 空串 / 高频值 ----------
    for c in compiled:
        for p in c["props"]:
            if p["kind"] != "lit":
                continue
            col, mid = p["col"], c["id"]

            def scan(col=col, mid=mid, xsd=p["xsd"]):
                sql = f"SELECT {col} AS V, COUNT(*) AS N FROM ({c['sql']}) t GROUP BY {col}"
                null_n = blank_n = total_n = 0
                top: list[tuple[int, str]] = []
                for row in _sqlstream(sql, ds, parts_dir, cancel):
                    n, v = int(row[1]), row[0]
                    total_n += n
                    if v is None:
                        null_n += n
                    elif v.strip() == "":
                        blank_n += n
                    else:
                        if len(top) < 3:
                            heapq.heappush(top, (n, v))
                        elif n > top[0][0]:
                            heapq.heapreplace(top, (n, v))
                return {
                    "mapping": mid, "column": col, "xsd": xsd,
                    "rows": total_n, "null": null_n, "blank": blank_n,
                    "top": [[v, n] for n, v in sorted(top, reverse=True)],
                }

            r = guarded(f"{mid}.{col}", scan)
            if r is None:
                continue
            # 只报有内容的：零脏值且高频值无信息量的列也保留（总数有价值），不滤
            literals.append(r)
            dirt = r["null"] + r["blank"]
            tops = ", ".join(f"{v!r}×{n:,}" for v, n in r["top"])
            _log(log_path,
                 f"体检[值] {r['column']}（{mid}）：共 {r['rows']:,} · 空 {r['null']:,} · "
                 f"空串 {r['blank']:,} · 高频: {tops or '—'}"
                 + (f" ⚠ 脏值合计 {dirt:,}" if dirt else ""))

    report = {
        "edges": edges,
        "literals": literals,
        "notes": notes,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    _log(log_path, f"体检完成：{len(edges)} 条边映射 / {len(literals)} 个数据属性 / "
                   f"{len(notes)} 项跳过，耗时 {report['elapsed_ms']}ms")
    return report
