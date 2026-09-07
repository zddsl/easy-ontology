"""本体读取路由：原图与视图增强版（?viewer=1，EO-5 实现）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse

from backend.services.project_store import get_store

router = APIRouter(prefix="/api/ontology", tags=["ontology"])


@router.get("/rdf")
def ontology_rdf(viewer: int = 0):
    """viewer=1 返回从映射反推 range 的增强副本（只给图看）；Ontop 永拿原版。"""
    store = get_store()
    p = store.ontology_path
    if not p.exists():
        raise HTTPException(404, "尚未上传本体")
    text = p.read_text(encoding="utf-8")
    if viewer:
        try:
            from backend.services.viewer_enricher import enrich

            text = enrich(text, store.mapping_path.read_text(encoding="utf-8"))
        except Exception as e:
            text = p.read_text(encoding="utf-8")
    # no-store：本体随编辑/上传随时变，绝不允许中间代理缓存旧副本
    return Response(
        content=text,
        media_type="application/rdf+xml; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/summary")
def ontology_summary():
    """返回本体结构摘要（供外部 LLM 做提示词）。

    返回格式：
    {
      "ns": "http://tohi.cn/2026/onto#",
      "classes": [":workorder（工单）：一张检修工单，含状态与描述", ...],
      "obj_props": [":relatefailure（关联故障）：工单申报的故障关联 [workorder → failure]", ...],
      "dt_props": [":wonum（工单号）：工单的业务唯一编号 [workorder → string]", ...]
    }
    """
    store = get_store()
    p = store.ontology_path
    if not p.exists():
        raise HTTPException(404, "尚未上传本体")
    from backend.services.llm_ask import summarize_ontology

    rdf_xml = p.read_text(encoding="utf-8")
    summary = summarize_ontology(rdf_xml)

    return JSONResponse(
        {
            "ns": summary["ns"],
            "classes": summary["classes"],
            "obj_props": summary["obj_props"],
            "dt_props": summary["dt_props"],
        },
        headers={"Cache-Control": "no-store"},
    )
