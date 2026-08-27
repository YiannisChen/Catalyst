from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from catalyst_app.env_loader import load_env_files

from fastapi import FastAPI

from catalyst_app.dependencies import get_live_run_service, get_runtime_dependency_loader, get_workbench_store
from catalyst_app.routers.capabilities import router as capabilities_router
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
    db_path: str | Path | None = None,
) -> FastAPI:
    load_env_files()
    app = FastAPI(title="Catalyst Live Runtime API", version="0.1.0", lifespan=_lifespan)
    app.include_router(live_runs_router)
    app.include_router(stream_router)
    app.include_router(workbench_router)
    app.include_router(capabilities_router)
    _install_host_origin_guard(app)
    app.state.admission_controller = admission_controller
    app.state.db_path = Path(db_path) if db_path is not None else None

    if service_override is not None:
        app.dependency_overrides[get_live_run_service] = lambda: service_override
    if dependency_loader_override is not None:
        app.dependency_overrides[get_runtime_dependency_loader] = lambda: dependency_loader_override
    if workbench_store_override is not None:
        app.dependency_overrides[get_workbench_store] = lambda: workbench_store_override

    return app


def _install_host_origin_guard(app: FastAPI) -> None:
    """Frozen §9.3 local security: loopback default; non-loopback requires an
    explicit override plus nonempty CATALYST_ALLOWED_HOSTS/ORIGINS. Host and
    Origin are checked for run/SSE/health/capability endpoints; CORS stays
    closed otherwise. Credentials never enter responses or events.
    """
    allow_non_loopback = os.getenv("CATALYST_ALLOW_NON_LOOPBACK") == "1"
    allowed_hosts = {
        h.strip().lower()
        for h in os.getenv("CATALYST_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    }
    allowed_origins = {
        o.strip()
        for o in os.getenv("CATALYST_ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    }
    if allow_non_loopback and (not allowed_hosts or not allowed_origins):
        raise RuntimeError(
            "CATALYST_ALLOW_NON_LOOPBACK=1 requires explicit nonempty "
            "CATALYST_ALLOWED_HOSTS and CATALYST_ALLOWED_ORIGINS"
        )
    # "testserver" is the standard local test-client host (httpx/TestClient);
    # it is only accepted in loopback mode where the app binds localhost.
    loopback_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
    loopback_origins = {
        "http://localhost",
        "http://127.0.0.1",
        "http://[::1]",
    }

    @app.middleware("http")
    async def _guard(request: Any, call_next: Any) -> Any:
        path = request.url.path
        guarded = (
            path.startswith("/api/live-runs")
            or path.startswith("/api/health")
            or path.startswith("/api/capabilities")
        )
        if guarded:
            host = (request.headers.get("host") or "").split(":")[0].lower()
            origin = request.headers.get("origin")
            if allow_non_loopback:
                if host not in allowed_hosts:
                    return JSONResponse(status_code=403, content={"detail": "host_forbidden"})
                if origin is not None and origin not in allowed_origins:
                    return JSONResponse(status_code=403, content={"detail": "origin_forbidden"})
            else:
                if host not in loopback_hosts | allowed_hosts:
                    return JSONResponse(status_code=403, content={"detail": "host_forbidden"})
                if origin is not None and origin not in loopback_origins | allowed_origins:
                    return JSONResponse(status_code=403, content={"detail": "origin_forbidden"})
        return await call_next(request)


app = create_app()
