from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from catalyst_app.env_loader import load_env_files

load_env_files()
from fastapi import FastAPI

from catalyst_app.access_control import install_access_control
from catalyst_app.db_paths import (
    runtime_db_is_split,
    runtime_db_path,
)
from catalyst_app.db_paths import (
    served_db_path as resolve_served_db_path,
)
from catalyst_app.dependencies import (
    get_live_run_service,
    get_runtime_dependency_loader,
    get_workbench_store,
)
from catalyst_app.deployment_configuration import validate_public_deployment_environment  # noqa: E402 (after load_env_files)
from catalyst_app.routers.admin_notify import router as admin_notify_router
from catalyst_app.routers.capabilities import router as capabilities_router
from catalyst_app.routers.data_jobs import router as data_jobs_router
from catalyst_app.routers.data_refresh import mark_interrupted_jobs
from catalyst_app.routers.data_refresh import router as data_refresh_router
from catalyst_app.routers.featured import router as featured_router
from catalyst_app.routers.live_runs import router as live_runs_router
from catalyst_app.routers.public_access import router as public_access_router
from catalyst_app.routers.quality import router as quality_router
from catalyst_app.routers.sessions import router as sessions_router
from catalyst_app.routers.stream import router as stream_router
from catalyst_app.routers.workbench import router as workbench_router

logger = logging.getLogger(__name__)
# uvicorn configures "uvicorn.error" at INFO with a handler; the app logger has none, so INFO
# lines from it would be dropped under systemd.
_request_logger = logging.getLogger("uvicorn.error.http")


def _start_runtime_warmup() -> None:
    if os.getenv("CATALYST_WARM_START", "1") == "0":
        return

    def warm() -> None:
        try:
            get_runtime_dependency_loader().get_dependencies()
        except Exception:
            logger.warning(
                "runtime dependency warm-up failed; request path will load lazily",
                exc_info=True,
            )

    threading.Thread(target=warm, name="catalyst-runtime-warmup", daemon=True).start()


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

    if os.getenv("CATALYST_ALLOW_NON_LOOPBACK") == "1":
        validate_public_deployment_environment()

    bodies_db = os.getenv("CATALYST_NEWS_BODIES_DB")
    if bodies_db:
        mark_interrupted_jobs(bodies_db)

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
            _start_runtime_warmup()
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
    _start_runtime_warmup()
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
    served_db_path: str | Path | None = None,
) -> FastAPI:
    load_env_files()
    app = FastAPI(title="Catalyst Live Runtime API", version="0.1.0", lifespan=_lifespan)

    @app.exception_handler(RequestValidationError)
    async def _safe_model_validation_error(request, exc):
        # FastAPI's default validation detail includes raw body input. These
        # request paths can carry BYOK keys, so keep malformed payloads out of
        # both the response body and any diagnostic representation of errors.
        path = request.url.path
        if (
            path in {"/api/models/validate", "/api/live-runs"}
            or (path.startswith("/api/live-runs/") and path.endswith("/retry"))
        ):
            return JSONResponse(status_code=422, content={"detail": {"code": "invalid_request"}})
        return await request_validation_exception_handler(request, exc)

    app.include_router(live_runs_router)
    app.include_router(public_access_router)
    app.include_router(stream_router)
    app.include_router(workbench_router)
    app.include_router(capabilities_router)
    app.include_router(data_refresh_router)
    app.include_router(data_jobs_router)
    app.include_router(admin_notify_router)
    app.include_router(featured_router)
    app.include_router(sessions_router)

    app.include_router(quality_router)
    install_access_control(app)
    _install_host_origin_guard(app)
    app.state.admission_controller = admission_controller
    app.state.runtime_composition = runtime_composition
    if db_path is not None:
        runtime_path = Path(db_path)
    elif runtime_composition is not None and getattr(runtime_composition, "db_path", None) is not None:
        runtime_path = Path(runtime_composition.db_path)
    elif runtime_db_is_split():
        runtime_path = runtime_db_path()
    elif os.getenv("CATALYST_DB_PATH"):
        runtime_path = resolve_served_db_path()
    else:
        runtime_path = None

    if served_db_path is not None:
        served_path = Path(served_db_path)
    elif runtime_db_is_split():
        served_path = resolve_served_db_path()
    else:
        served_path = runtime_path

    app.state.db_path = runtime_path
    app.state.served_db_path = served_path

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
    Origin are checked for every /api/ endpoint; CORS stays
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
        started = time.perf_counter()
        path = request.url.path
        # Every API route: runs, SSE, health, capabilities, models, data
        # refresh and workbench reads.
        guarded = path.startswith("/api/")
        if guarded:
            host = (request.headers.get("host") or "").split(":")[0].lower()
            origin = request.headers.get("origin")
            if allow_non_loopback:
                # Non-loopback requires the explicit override AND the allowlist.
                if host not in allowed_hosts:
                    response = JSONResponse(status_code=403, content={"detail": "host_forbidden"})
                    _log_request(request, response.status_code, started)
                    return response
                if origin is not None and origin not in allowed_origins:
                    response = JSONResponse(status_code=403, content={"detail": "origin_forbidden"})
                    _log_request(request, response.status_code, started)
                    return response
            else:
                # Loopback mode: a populated allowlist alone must never enable
                # a non-loopback Host/Origin (Finding J; Frozen §9.3).
                if host not in loopback_hosts:
                    response = JSONResponse(status_code=403, content={"detail": "host_forbidden"})
                    _log_request(request, response.status_code, started)
                    return response
                if origin is not None and origin not in loopback_origins:
                    response = JSONResponse(status_code=403, content={"detail": "origin_forbidden"})
                    _log_request(request, response.status_code, started)
                    return response
        try:
            response = await call_next(request)
        except Exception:
            _log_request(request, 500, started)
            raise
        _log_request(request, response.status_code, started)
        return response


def _log_request(request: Any, status_code: int, started: float) -> None:
    route = request.scope.get("route")
    template = getattr(route, "path", "<unmatched>")
    streaming = " stream=true" if template == "/api/live-runs/{run_id}/stream" else ""
    _request_logger.info(
        "http_request method=%s route=%s status=%s duration_ms=%.1f%s",
        request.method,
        template,
        status_code,
        (time.perf_counter() - started) * 1000,
        streaming,
    )


app = create_app()
