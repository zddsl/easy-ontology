"""工作空间管理：列表（含摘要）/ 新建（建+切）/ 切换（自动停端点）/ 删除。

锁顺序硬性：外层 ontop_process.op_lock，内层 registry._lock（单向嵌套防死锁）。
物化进行中（materialize busy）一律 409，防 rmtree 正在写的 abox / 重建误判。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.ontop_process import get_manager, op_lock
from backend.services.workspace_registry import get_registry

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


class CreateBody(BaseModel):
    name: str


def _summary(ws_id: str) -> dict:
    """读 <ws>/project.json 出摘要（不构造 ProjectStore，避免摘要产生 mkdir 副作用）。"""
    reg = get_registry()
    active = reg.active_id()
    snap = {}
    pj = reg.ws_dir(ws_id) / "project.json"
    if pj.exists():
        try:
            snap = json.loads(pj.read_text(encoding="utf-8"))
        except Exception:
            snap = {}
    running = ws_id == active and get_manager().is_running()
    return {
        "id": ws_id,
        "created_at": next((w.get("created_at") for w in reg.list_all() if w["id"] == ws_id), None),
        "active": ws_id == active,
        "state": snap.get("state"),
        "has_ontology": bool(snap.get("ontology")),
        "has_mapping": bool(snap.get("mapping")),
        "has_datasource": bool(snap.get("datasource")),
        "has_abox": bool(snap.get("abox")),
        # endpoint_running 只认激活空间 + 端点实况（非激活空间残留的 RUNNING 是崩溃遗留，展示不认）
        "endpoint_running": running,
    }


@router.get("")
def list_workspaces():
    reg = get_registry()
    return {"active": reg.active_id(), "workspaces": [_summary(i) for i in reg.list_ids()]}


@router.post("", status_code=201)
def create_workspace(body: CreateBody):
    from backend.services.materialize import is_busy

    reg = get_registry()
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "工作空间名不能为空")
    from backend.services.workspace_registry import NAME_RE

    if not NAME_RE.match(name):
        raise HTTPException(400, "名字只允许字母、数字、下划线、连字符，1-32 位")
    if any(w["id"].lower() == name.lower() for w in reg.list_all()):
        raise HTTPException(409, f"工作空间「{name}」已存在")
    if is_busy():
        raise HTTPException(409, "ABox 物化/清除进行中，稍后再试")
    with op_lock:
        if is_busy():  # 锁内复查：物化在 op_lock 保护下抢 _busy，杜绝插队窗口
            raise HTTPException(409, "ABox 物化/清除进行中，稍后再试")
        endpoint_was_running = get_manager().is_running()
        if endpoint_was_running:
            get_manager().stop()  # 新建即切换 → 先停端点（用户拍板）
        try:
            reg.register_new(name)
        except FileExistsError as e:
            raise HTTPException(409, str(e))
        reg.set_active(name)
    return {"active": name, "endpoint_was_running": endpoint_was_running}


@router.post("/{ws_id}/switch")
def switch_workspace(ws_id: str):
    from backend.services.materialize import is_busy

    reg = get_registry()
    if not reg.exists(ws_id):
        raise HTTPException(404, f"工作空间不存在：{ws_id}")
    if ws_id == reg.active_id():
        return {"active": ws_id, "endpoint_was_running": get_manager().is_running(), "note": "已是当前空间"}
    if is_busy():
        raise HTTPException(409, "ABox 物化/清除进行中，稍后再试")
    with op_lock:
        if is_busy():  # 锁内复查（同上）
            raise HTTPException(409, "ABox 物化/清除进行中，稍后再试")
        endpoint_was_running = get_manager().is_running()
        if endpoint_was_running:
            get_manager().stop()  # 停在旧空间（此刻 active 未变，状态回写正确）
        try:
            reg.set_active(ws_id)  # 含 QLever 指针
        except KeyError as e:
            raise HTTPException(404, str(e))
    return {"active": ws_id, "endpoint_was_running": endpoint_was_running}


@router.delete("/{ws_id}")
def delete_workspace(ws_id: str):
    from backend.services.materialize import is_busy

    reg = get_registry()
    if not reg.exists(ws_id):
        raise HTTPException(404, f"工作空间不存在：{ws_id}")
    if len(reg.list_ids()) <= 1:
        raise HTTPException(409, "至少保留一个工作空间")
    if is_busy():
        raise HTTPException(409, "ABox 物化/清除进行中，稍后再试")
    with op_lock:
        if is_busy():  # 锁内复查（同上）
            raise HTTPException(409, "ABox 物化/清除进行中，稍后再试")
        if ws_id == reg.active_id() and get_manager().is_running():
            get_manager().stop()
        try:
            reg.remove(ws_id)  # 删激活空间时在 registry 内原子改绑+指针归位
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))
    return {"ok": True, "active": get_registry().active_id()}
