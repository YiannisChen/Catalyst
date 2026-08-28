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
    """M6 runtime lifespan over the single app-owned composition.

    One ConditionRegistry is created on the running event loop and wired into
    the production EventRepository as the after-commit notifier before worker
    repositories need it. Startup recovery completes before admission reopens;
    shutdown stops new admission, requests cooperative cancellation, waits one
    bounded grace period, and leaves unresolved rows for the same
    startup-recovery policy (Final TSD §11/§15/§18; Finding C).
    """
    import asyncio

    from catalyst_app.runtime.composition import build_runtime_composition
    from catalyst_app.runtime.sse import ConditionRegistry

    composition = getattr(app.state, "runtime_composition", None)
    injected_controller = getattr(app.state, "admission_controller", None)
    if composition is None:
        if injected_controller is None:
            registry = ConditionRegistry(loop=asyncio.get_running_loop())
            composition = build_runtime_composition(
                db_path=app.state.db_path,
                condition_registry=registry,
            )
            app.state.runtime_composition = composition
            app.state.admission_controller = composition.admission
            app.state.cancellation_controller = composition.cancellation
            app.state.condition_registry = registry
            composition.admission.startup_recovery()
            yield
            composition.admission.shutdown()
            return

        # Legacy injection path: keep the injected controller exactly as the
        # caller constructed it (tests and compat callers); only attach the
        # loop-bound registry for SSE wakeups.
        registry = ConditionRegistry(loop=asyncio.get_running_loop())
        app.state.condition_registry = registry
        injected_controller.startup_recovery()
        yield
        injected_controller.shutdown()
        return

    registry = composition.condition_registry
    if registry is None:
        registry = ConditionRegistry(loop=asyncio.get_running_loop())
        composition.condition_registry = registry
        composition.events.set_notifier(registry.notify_from_thread)
    app.state.admission_controller = composition.admission
    app.state.cancellation_controller = composition.cancellation
    app.state.condition_registry = registry
    # The router-level credential store must be the composition's store so
    # BYOK credentials registered at admission are visible to the run adapter
    # (Finding I).
    from catalyst_app.dependencies import get_credential_store

    app.dependency_overrides[get_credential_store] = lambda: composition.credential_store
    composition.admission.startup_recovery()
    yield
    composition.admission.shutdown()


def create_app(
    *,
    service_override=None,
    dependency_loader_override=None,
    workbench_store_override=None,
    admission_controller: Any = None,
    runtime_composition: Any = None,
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
    app.state.runtime_composition = runtime_composition
    if db_path is not None:
        app.state.db_path = Path(db_path)
    elif runtime_composition is not None and getattr(runtime_composition, "db_path", None) is not None:
        app.state.db_path = Path(runtime_composition.db_path)
    else:
        app.state.db_path = None

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
                # Non-loopback requires the explicit override AND the allowlist.
                if host not in allowed_hosts:
                    return JSONResponse(status_code=403, content={"detail": "host_forbidden"})
                if origin is not None and origin not in allowed_origins:
                    return JSONResponse(status_code=403, content={"detail": "origin_forbidden"})
            else:
                # Loopback mode: a populated allowlist alone must never enable
                # a non-loopback Host/Origin (Finding J; Frozen §9.3).
                if host not in loopback_hosts:
                    return JSONResponse(status_code=403, content={"detail": "host_forbidden"})
                if origin is not None and origin not in loopback_origins:
                    return JSONResponse(status_code=403, content={"detail": "origin_forbidden"})
        return await call_next(request)


app = create_app()
