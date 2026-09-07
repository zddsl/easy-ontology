"""映射搭建器路由：库表元数据、本体元素清单、草稿读写、生成预览、提交保存 .obda。

草稿是工作区文件（data/files/mapping-draft.json）；commit 走 store.save_mapping(origin="builder")
与上传同一落点互相覆盖。生成在服务端做（表单是唯一事实来源；第 3 步的手改文本原样提交保存）。
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Body, HTTPException, Response
from pydantic import BaseModel

from backend.services import mapping_service as ms
from backend.services.project_store import get_store, _now_str
from backend.services.schema import get_schema, schema_scope

router = APIRouter(prefix="/api/mapping", tags=["mapping"])


def _datasource() -> dict:
    ds = get_store().snapshot().get("datasource")
    if not ds:
        raise HTTPException(422, "请先在主页保存并测试数据源")
    return ds


def _elements() -> dict:
    store = get_store()
    if not store.ontology_path.exists():
        raise HTTPException(404, "还没有已保存的本体，请先上传或平台搭建")
    try:
        return ms.parse_ontology_for_mapping(store.ontology_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(400, f"本体解析失败：{e}")


# ---------- 库表元数据 / 本体元素 ----------

@router.get("/schema")
def get_schema_route(refresh: bool = False):
    """表/字段下拉数据源（TTL 缓存；改了库表后 refresh=1 强制重取）。"""
    ds = _datasource()
    result = get_schema(ds, refresh=refresh)
    result["db_type"] = ds["db_type"]
    if not result.get("ok"):
        raise HTTPException(502, result.get("error") or "读取表结构失败")
    return result


@router.get("/elements")
def get_elements():
    return _elements()


@router.get("/obda")
def mapping_obda():
    """输出当前工作空间已保存映射的 .obda 原文（纯文本）。"""
    store = get_store()
    p = store.mapping_path
    if not p.exists():
        raise HTTPException(404, "当前工作空间还没有已保存的映射")
    return Response(content=p.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")


# ---------- 草稿 ----------

@router.get("/draft")
def get_draft():
    store = get_store()
    p = store.mapping_draft_path
    if not p.exists():
        return {"exists": False, "draft": None, "counts": None, "updated_at": None, "stale": []}
    try:
        draft = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"exists": False, "draft": None, "counts": None, "updated_at": None, "stale": [], "error": f"草稿损坏：{e}"}
    stale: list[str] = []
    if store.ontology_path.exists():
        try:
            elements = ms.parse_ontology_for_mapping(store.ontology_path.read_text(encoding="utf-8"))
            stale = ms.diff_draft_vs_ontology(draft, elements)
            digest_old = (draft.get("ontology") or {}).get("digest")
            if digest_old and digest_old != elements["digest"]:
                stale.insert(0, "本体已更新（与草稿建立时不同）")
        except Exception:
            pass
    return {"exists": True, "draft": draft, "counts": ms.draft_counts(draft),
            "updated_at": draft.get("updated_at"), "stale": stale}


@router.put("/draft")
def put_draft(draft: dict = Body(...)):
    """自动保存：软存（编辑中间态）；仅结构损坏 422。校验由前端本地 + generate 时服务端做。"""
    try:
        d = ms.normalize_mapping_draft(draft)
    except ms.MappingDraftError as e:
        raise HTTPException(422, str(e))
    d["updated_at"] = _now_str()
    get_store().mapping_draft_path.write_text(
        json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"saved_at": d["updated_at"], "issues": []}


@router.delete("/draft")
def delete_draft():
    p = get_store().mapping_draft_path
    if p.exists():
        p.unlink()
    return {"ok": True}


# ---------- 生成 / 提交 ----------

@router.post("/generate")
def generate(draft: dict = Body(...)):
    """草稿 → .obda 文本（不落盘）。软警告随结果返回，硬错误 422。"""
    elements = _elements()
    try:
        d = ms.normalize_mapping_draft(draft)
    except ms.MappingDraftError as e:
        raise HTTPException(422, str(e))
    errors, warnings = ms.validate_draft(d, elements)
    if errors:
        raise HTTPException(422, {"message": errors[0], "issues": errors})
    ds = get_store().snapshot().get("datasource") or {}
    result = ms.generate_obda(d, elements, db_type=ds.get("db_type", "mysql"),
                              scope=schema_scope(ds)[1] if ds else None)
    result["warnings"] = warnings + result["warnings"]
    return result


class CommitBody(BaseModel):
    draft: dict
    obda: str


@router.post("/fk-check")
def fk_check(draft: dict = Body(...)):
    """逐边抽样外键命中率（默认 200 行）：0%=死外键（边会全悬空），<90%=脏外键（部分悬空）。"""
    ds = _datasource()
    try:
        d = ms.normalize_mapping_draft(draft)
    except ms.MappingDraftError as e:
        raise HTTPException(422, str(e))
    return ms.check_fk_hit_rates(d, ds, scope=schema_scope(ds)[1])


@router.post("/commit")
def commit(body: CommitBody):
    """校验草稿与文本 → 与上传同一落点保存。端点在跑时返回重启提示。"""
    try:
        d = ms.normalize_mapping_draft(body.draft)
    except ms.MappingDraftError as e:
        raise HTTPException(422, str(e))
    elements = _elements()
    errors, _ = ms.validate_draft(d, elements)
    ok, err, n = ms.validate_obda_text(body.obda)
    if not ok:
        errors = errors + [err]
    if errors:
        raise HTTPException(422, {"message": errors[0], "issues": errors})

    store = get_store()
    store.save_mapping(body.obda, "mapping-builder.obda", n, origin="builder")

    snap = store.snapshot()
    resp = {
        "state": store.state,
        "n_mappings": n,
        "endpoint_running": bool(snap.get("ontop")),
        "hint": None,
    }
    hints = []
    if resp["endpoint_running"]:
        hints.append("端点正在运行，重启端点后新映射才生效")
    if snap.get("abox"):
        hints.append("已有 ABox 基于旧映射，需要时请重新生成或清除")
    resp["hint"] = "；".join(hints) or None
    return resp
