"""本体搭建器路由：草稿读写、导入已保存本体回填、提交生成 .rdf。

草稿是工作区文件（data/files/builder-draft.json），与上传同一落点互相覆盖：
commit 走 store.save_ontology(origin="builder")，拓扑图/端点/物化链路零改动。
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Body, HTTPException

from backend.services import builder_service as bs
from backend.services.project_store import get_store, _now_str

router = APIRouter(prefix="/api/builder", tags=["builder"])


# ---------- 草稿 ----------

@router.get("/draft")
def get_draft():
    p = get_store().builder_draft_path
    if not p.exists():
        return {"exists": False, "draft": None, "counts": None, "updated_at": None}
    try:
        draft = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"exists": False, "draft": None, "counts": None, "updated_at": None, "error": f"草稿损坏：{e}"}
    return {
        "exists": True,
        "draft": draft,
        "counts": bs.draft_counts(draft),
        "updated_at": draft.get("updated_at"),
    }


@router.put("/draft")
def put_draft(draft: dict = Body(...)):
    """自动保存：软校验（issues 非空也存，编辑中间态）；仅结构损坏 422。"""
    try:
        d = bs.normalize_draft(draft)
    except bs.DraftError as e:
        raise HTTPException(422, str(e))
    d["updated_at"] = _now_str()
    get_store().builder_draft_path.write_text(
        json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    errors, warnings = bs.validate_draft(d)
    return {"saved_at": d["updated_at"], "issues": errors + warnings}


@router.delete("/draft")
def delete_draft():
    p = get_store().builder_draft_path
    if p.exists():
        p.unlink()
    return {"ok": True}


# ---------- 导入回填 ----------

@router.post("/import")
def import_from_saved():
    """把当前已保存的本体解析回草稿（有损：不支持的构造丢弃并逐条告知）。"""
    store = get_store()
    if not store.ontology_path.exists():
        raise HTTPException(404, "还没有已保存的本体可导入")
    try:
        result = bs.parse_ontology_for_builder(store.ontology_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


# ---------- 提交 ----------

@router.post("/commit")
def commit_draft(draft: dict = Body(...)):
    """校验 → 生成 RDF/XML → 与上传同一落点保存。端点在跑时返回重启提示。"""
    try:
        d = bs.normalize_draft(draft)
    except bs.DraftError as e:
        raise HTTPException(422, str(e))

    errors, _ = bs.validate_draft(d)
    if not d.get("classes"):
        errors = errors + ["提交前至少需要 1 个类"]
    if errors:
        raise HTTPException(422, {"message": "本体还有问题，先修复再提交", "issues": errors})

    xml, triples = bs.build_rdf(d)
    store = get_store()
    store.save_ontology(xml, "builder.rdf", "builder", origin="builder")

    snap = store.snapshot()
    resp = {
        "state": store.state,
        "triples": triples,
        "counts": bs.draft_counts(d),
        "endpoint_running": bool(snap.get("ontop")),
        "hint": None,
    }
    hints = []
    if resp["endpoint_running"]:
        hints.append("端点正在运行，重启端点后新本体才生效")
    if snap.get("abox"):
        hints.append("已有 ABox 基于旧本体，需要时请重新生成或清除")
    resp["hint"] = "；".join(hints) or None
    return resp
