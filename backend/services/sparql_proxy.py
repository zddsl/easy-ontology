"""对外 /sparql：Ontop 协议兼容代理（form query + Accept 透传）+ 查询登记簿 + 行数上限。

僵尸查询现实：HTTP 超时不取消服务端 SQL，唯一可靠杀法是重启端点——
超时条目标记 timeout，前端提供重启按钮。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time
import uuid
import os
from collections import deque
from datetime import datetime, timezone, timedelta

import httpx

from backend.config import get_config

_registry: deque = deque(maxlen=50)


class EndpointUnavailable(Exception):
    """虚拟路线端点自动拉起失败（配置不全/启动报错），message 面向调用方。"""


async def _ensure_endpoint() -> None:
    """虚拟路线懒启动：端点没在跑就自动拉起（to_thread 防阻塞事件循环，boot 可到 60s）。"""
    from backend.services.ontop_process import StartError, get_manager

    m = get_manager()
    if m.is_running():
        return
    try:
        await asyncio.to_thread(m.ensure)
    except StartError as e:
        raise EndpointUnavailable(f"端点自动启动失败：{e}") from e

def _now_str() -> str:
    """当前时间字符串，读 TZ_OFFSET 环境变量（整数小时，默认 8 = UTC+8）。"""
    offset = int(os.environ.get("TZ_OFFSET", "8"))
    tz = timezone(timedelta(hours=offset))
    return datetime.now(tz).strftime("%H:%M:%S")


def list_queries() -> list[dict]:
    return list(_registry)


def update_entry(entry_id: str, **fields) -> None:
    for e in _registry:
        if e["id"] == entry_id:
            e.update(fields)
            return


def _register(query: str, meta: dict | None = None) -> dict:
    entry = {"id": uuid.uuid4().hex[:8], "query": query, "started_at": _now_str(),
             "state": "running", "elapsed_ms": None}
    if meta:
        entry.update(meta)
    _registry.appendleft(entry)
    return entry


def _finish(entry: dict, state: str, elapsed_ms: int) -> None:
    entry["state"] = state
    entry["elapsed_ms"] = elapsed_ms


def _bypass_cap(query: str) -> bool:
    head = query.lstrip()[:16].upper()
    return head.startswith(("ASK", "CONSTRUCT", "DESCRIBE"))


def _cap_csv(text: str, max_rows: int) -> tuple[str, bool]:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) <= max_rows + 1:  # 表头 + 数据
        return text, False
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerows(rows[: max_rows + 1])
    writer.writerow([f"...结果超过 {max_rows} 行已截断，请加 LIMIT 或缩小条件"])
    return out.getvalue(), True


def _cap_json(text: str, max_rows: int) -> tuple[str, bool]:
    data = json.loads(text)
    bindings = data.get("results", {}).get("bindings")
    if bindings is None or len(bindings) <= max_rows:
        return text, False
    data["results"]["bindings"] = bindings[:max_rows]
    return json.dumps(data, ensure_ascii=False), True


async def forward(query: str, accept: str, route: str = "virtual", meta: dict | None = None) -> tuple[httpx.Response, str]:
    """转发查询。route='virtual' 走 Ontop（虚拟），'materialized' 走 QLever。返回 (response, entry_id)。"""
    cfg = get_config()
    entry = _register(query, meta)
    entry["route"] = route
    t0 = time.time()

    if route == "materialized":
        target = cfg.rdf_store.sparql_url
    else:
        target = f"http://127.0.0.1:{cfg.ontop.port}/sparql"
        await _ensure_endpoint()  # 没跑就拉起（幂等；/sparql 与 /api/ask 共用此 choke point）

    try:
        async with httpx.AsyncClient(timeout=cfg.sparql.timeout_s) as client:
            r = await client.post(
                target,
                data={"query": query},
                headers={"Accept": accept} if accept else {},
            )
        _finish(entry, "done", int((time.time() - t0) * 1000))
        return r, entry["id"]
    except httpx.TimeoutException:
        _finish(entry, "timeout", int((time.time() - t0) * 1000))
        raise
    except Exception:
        _finish(entry, "error", int((time.time() - t0) * 1000))
        raise


def apply_row_cap(response: httpx.Response, query: str) -> httpx.Response:
    """SELECT 类结果按上限截断（CSV/JSON），其余透传。"""
    cfg = get_config()
    if _bypass_cap(query):
        return response
    ctype = response.headers.get("content-type", "")
    try:
        if "text/csv" in ctype:
            text, capped = _cap_csv(response.text, cfg.sparql.max_rows)
            if capped:
                response.headers["X-Row-Cap"] = "truncated"
                response._text = None
                response = httpx.Response(status_code=response.status_code, headers=response.headers,
                                          content=text.encode("utf-8"))
        elif "sparql-results+json" in ctype or "application/json" in ctype:
            text, capped = _cap_json(response.text, cfg.sparql.max_rows)
            if capped:
                response.headers["X-Row-Cap"] = "truncated"
                response = httpx.Response(status_code=response.status_code, headers=response.headers,
                                          content=text.encode("utf-8"))
    except Exception:
        pass  # 截断失败就透传原文
    return response
