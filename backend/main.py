"""easy_ontology 入口：本机学习/demo 用 OBDA 平台。

组装：本体+映射上传 → 数据源表单 → 起 Ontop 端点 → 拓扑图 + 对外 /sparql。
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.config import get_config
from backend.routers import ask, builder, mapping, materialize, ontology, project, settings, sparql, workspaces


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    print(f"[easy_ontology] startup, data_dir={cfg.paths.data_dir}", flush=True)

    # 工作空间注册表初始化（含 legacy 单项目迁移）——必须先于任何 get_store()
    from backend.services import project_store as ps
    from backend.services.ontop_process import StartError, get_manager
    from backend.services.workspace_registry import get_registry

    reg = get_registry()
    reg.init()
    print(f"[easy_ontology] active_workspace={reg.active_id()}", flush=True)

    # 启动复活：激活空间上次 RUNNING 且 auto_start 开启 → 后台线程重启端点（不阻塞服务启动）。
    # 覆盖场景：容器重启、dev 热重载（reload 先走 shutdown 杀子进程再走这里拉起）。
    store = ps.get_store()
    if store.state == ps.RUNNING and cfg.auto_start_last_project:
        def _revive():
            try:
                info = get_manager().start()
                print(f"[easy_ontology] 端点自动复活：{info}", flush=True)
            except StartError as e:
                store.set_error(f"端点自动复活失败：{e}")
                print(f"[easy_ontology] 复活失败：{e}", flush=True)

        threading.Thread(target=_revive, daemon=True).start()

    yield

    # 关闭：杀掉托管的 ontop 子进程，防 reload/停容器后变孤儿。
    # 注意只杀进程不动 project.json 状态——RUNNING 留给下次启动复活用。
    try:
        m = get_manager()
        if m.is_running():
            m.kill_tree()
            print("[easy_ontology] 已停止托管 ontop 子进程", flush=True)
    except Exception as e:
        print(f"[easy_ontology] shutdown 清理异常：{e}", flush=True)


app = FastAPI(title="easy_ontology", lifespan=lifespan)
app.include_router(project.router)
app.include_router(ontology.router)
app.include_router(sparql.router)
app.include_router(ask.router)
app.include_router(materialize.router)
app.include_router(builder.router)
app.include_router(mapping.router)
app.include_router(workspaces.router)
app.include_router(settings.router)

# 静态资源：路由先注册，挂载放最后（/api、/sparql 优先匹配）
from pathlib import Path

from fastapi.staticfiles import StaticFiles

_static = Path(__file__).resolve().parent / "static"
_playground = _static / "playground"
if (_playground / "index.html").exists():
    app.mount("/playground", StaticFiles(directory=_playground, html=True), name="playground")
app.mount("/", StaticFiles(directory=_static, html=True), name="static")
