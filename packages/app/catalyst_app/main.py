from __future__ import annotations

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from catalyst_app.env_loader import load_env_files

from fastapi import FastAPI

from catalyst_app.dependencies import get_live_run_service, get_runtime_dependency_loader, get_workbench_store
from catalyst_app.routers.live_runs import router as live_runs_router
from catalyst_app.routers.workbench import router as workbench_router


def create_app(*, service_override=None, dependency_loader_override=None, workbench_store_override=None) -> FastAPI:
    load_env_files()
    app = FastAPI(title="Catalyst Live Runtime API", version="0.1.0")
    app.include_router(live_runs_router)
    app.include_router(workbench_router)

    if service_override is not None:
        app.dependency_overrides[get_live_run_service] = lambda: service_override
    if dependency_loader_override is not None:
        app.dependency_overrides[get_runtime_dependency_loader] = lambda: dependency_loader_override
    if workbench_store_override is not None:
        app.dependency_overrides[get_workbench_store] = lambda: workbench_store_override

    return app


app = create_app()
