"""全局设置路由：LLM API Key 等平台级配置。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services import settings_store

router = APIRouter(prefix="/api", tags=["settings"])


class SettingsBody(BaseModel):
    llm_api_key: str | None = None


@router.get("/settings")
def get_settings():
    key = settings_store.get_llm_key()
    return {"llm_api_key_set": key is not None}


@router.put("/settings")
def put_settings(body: SettingsBody):
    settings_store.update_settings(llm_api_key=body.llm_api_key)
    return {"ok": True, "llm_api_key_set": body.llm_api_key not in (None, "")}
