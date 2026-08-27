from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from catalyst_app.env_loader import load_env_files

from fastapi import FastAPI

from catalyst_app.dependencies import get_live_run_service, get_runtime_dependency_loader, get_workbench_store
from catalyst_app.routers.live_runs import router as live_runs_router
from catalyst_app.routers.stream import router as stream_router
from catalyst_app.routers.workbench import router as workbench_router


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """M6 runtime lifespan.

    Startup recovery completes before admission reopens; shutdown stops new
    admission, waits one bounded grace period, and leaves unresolved rows for
    the same startup-recovery policy (Final TSD §11/§15).
    """
    from catalyst_app.runtime_wiring import (
        build_default_admission_controller,
        build_default_cancellation_controller,
    )

    controller = getattr(app.state, "admission_controller", None)
    if controller is None:
        controller = build_default_admission_controller()
        app.state.admission_controller = controller
    if getattr(app.state, "cancellation_controller", None) is None:
        app.state.cancellation_controller = build_default_cancellation_controller()
    controller.startup_recovery()
    yield
    controller.shutdown()


def create_app(
    *,
    service_override=None,
    dependency_loader_override=None,
    workbench_store_override=None,
    admission_controller: Any = None,
) -> FastAPI:
    load_env_files()
    app = FastAPI(title="Catalyst Live Runtime API", version="0.1.0", lifespan=_lifespan)
    app.include_router(live_runs_router)
    app.include_router(stream_router)
    app.include_router(workbench_router)
    app.state.admission_controller = admission_controller

    if service_override is not None:
        app.dependency_overrides[get_live_run_service] = lambda: service_override
    if dependency_loader_override is not None:
        app.dependency_overrides[get_runtime_dependency_loader] = lambda: dependency_loader_override
    if workbench_store_override is not None:
        app.dependency_overrides[get_workbench_store] = lambda: workbench_store_override

    return app


app = create_app()
