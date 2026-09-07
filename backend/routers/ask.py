"""自然语言问答路由：POST /api/ask → 两阶段管线（词汇识别→路径过滤→白名单生成→修复→答）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services import llm_ask, settings_store

router = APIRouter(prefix="/api", tags=["ask"])


class AskBody(BaseModel):
    question: str
    route: str = "virtual"      # virtual|materialized
    max_hops: int | None = None  # 路径库最大跳数 1~8，缺省 4（本体最大支持跳数）


@router.post("/ask")
async def ask(body: AskBody):
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")
    if body.route not in ("virtual", "materialized"):
        raise HTTPException(400, f"未知 route: {body.route}")
    api_key = settings_store.get_llm_key()
    if not api_key:
        raise HTTPException(400, "未配置 LLM API Key，请在顶栏「配置」中设置")
    try:
        return await llm_ask.ask(body.question.strip(), api_key,
                                 route=body.route, max_hops=body.max_hops)
    except llm_ask.LLMError as e:
        raise HTTPException(400, str(e))
