"""对外唯一 SPARQL 接口：POST&GET /sparql，协议与 Ontop 端点兼容（Dify 改 URL 即切换）。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from backend.config import get_config
from backend.services import sparql_proxy

router = APIRouter(tags=["sparql"])


async def _handle(request: Request) -> Response:
    if request.method == "POST":
        form = await request.form()
        query = form.get("query")
        route = form.get("route") or request.query_params.get("route") or "virtual"
    else:
        query = request.query_params.get("query")
        route = request.query_params.get("route", "virtual")

    if route not in ("virtual", "materialized"):
        return JSONResponse({"error": f"未知 route: {route}"}, status_code=400)

    if not query:
        return JSONResponse({"error": "缺少 query 参数（POST form 或 GET ?query=）"}, status_code=400)

    accept = request.headers.get("accept", "text/csv")
    try:
        r, _entry_id = await sparql_proxy.forward(query, accept, route=route)
    except sparql_proxy.EndpointUnavailable as e:
        # 虚拟路线懒启动失败（配置不全/启动报错）——端点无需用户手动管理
        return JSONResponse({"error": str(e)}, status_code=503)
    except httpx.TimeoutException:
        return JSONResponse(
            {"error": f"查询超时（>{get_config().sparql.timeout_s}s），已标记挂起；服务端 SQL 可能仍在执行，必要时请重启端点"},
            status_code=504,
        )
    except Exception as e:
        return JSONResponse({"error": f"转发失败：{e}"}, status_code=502)

    r = sparql_proxy.apply_row_cap(r, query)
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "text/plain"),
    )


@router.post("/sparql")
async def sparql_post(request: Request):
    return await _handle(request)


@router.get("/sparql")
async def sparql_get(request: Request):
    return await _handle(request)


@router.get("/api/queries")
def queries():
    return {"queries": sparql_proxy.list_queries()}
