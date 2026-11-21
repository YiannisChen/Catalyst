"""V1.1 run orchestration API surface (M6-9) with bounded migration compat.

Final Migration TSD §20.1: POST /api/live-runs (ACCEPTED + stream_url +
optional Idempotency-Key), GET run (lifecycle + nullable attribution +
manifest refs), paged artifacts, same-run artifact projection,
evidence/claims details. Legacy GET /events, /workspace, /retry and /models/*
remain translated in packages/api only (M6 migration window); cancel-all is
not part of the normal public API.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import sqlite3
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_app.runtime.run_service import (
    RunViewDataError,
    utc_parse,
)
from catalyst_app.runtime.run_service import (
    create_live_run as create_live_run_service,
)
from catalyst_app.runtime.run_service import (
    read_run_view as _read_run_view,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)


def _mark_deprecated(response: Response, *, endpoint: str) -> None:
    """Flag migration-window compat callers for the M8 audit (M6-12)."""
    response.headers["Deprecation"] = "true"
    _logger.info("deprecated compat endpoint called: %s", endpoint)

from catalyst_app.access_control import (
    AccessContext,
    require_admin,
    resolve_access_context,
)
from catalyst_app.api_dto import (
    ArtifactDTO,
    ArtifactRefDTO,
    CancelResponse,
    ClaimDetailDTO,
    EvidenceDetailDTO,
    HealthDTO,
    PublicCacheRunDTO,
    RunAcceptedResponse,
    RunDTO,
    RunFailureDTO,
    RunViewDTO,
    WorkbenchProjectionDTO,
)
from catalyst_app.db_paths import runtime_db_path, served_db_path
from catalyst_app.dependencies import (
    get_credential_store,
    get_live_run_service,
    get_runtime_dependency_loader,
)
from catalyst_app.env_loader import get_provider_env_key
from catalyst_app.featured import featured_health_status, featured_is_ready
from catalyst_app.llm_factory import DEFAULT_MODEL, PUBLIC_RUN_MODEL, SUPPORTED_MODELS
from catalyst_app.model_catalog import build_catalog, catalog_provider_base_url
from catalyst_app.persistence.connect import open_readonly
from catalyst_app.provider_validator import validate_provider
from catalyst_app.public_projection import project_public_run_view
from catalyst_app.runtime.admission import (
    AdmissionController,
    AdmissionValidationError,
    build_verify_source_admission_request,
)
from catalyst_app.runtime.budget import BudgetAdmission, BudgetLedger
from catalyst_app.runtime.composition import RuntimeUnavailableError
from catalyst_app.runtime.public_result_cache import (
    compute_public_cache_key,
    find_completed_public_run,
    production_strategy_identity,
)
from catalyst_app.schemas import (
    CreateRunRequest,
    CredentialSource,
    FailurePayload,
    ModelCatalogResponse,
    ModelOption,
    ModelsResponse,
    ModelValidateRequest,
    ModelValidateResponse,
    RetryRunRequest,
    RetryRunResponse,
    RunEventResponse,
    RunStatus,
    RuntimeHealthResponse,
    WorkspaceResponse,
)
from catalyst_app.turnstile import require_valid_turnstile
from catalyst_app.workspace_projection import project_workspace, project_workspace_v1

router = APIRouter(prefix="/api", tags=["live-runs"])

QUOTA_EXHAUSTED_MESSAGE = "Today's run quota is exhausted"
PUBLIC_MODEL_FIXED_DETAIL = "invalid_request: public_model_fixed"
PUBLIC_BYOK_BASE_URL_DETAIL = "invalid_request: public_byok_base_url_not_allowed"


class RateLimitErrorResponse(BaseModel):
    """429 body for capacity and quota exhaustion.

    ``detail`` preserves the existing contract code. Quota exhaustion adds the
    user-facing message.
    """

    detail: str
    message: str | None = None
    featured_url: str | None = None


class VerifySourceRequest(BaseModel):
    lead_id: str = Field(pattern=r"^[0-9a-f]{64}$")


def _db_path(request: Request) -> Path:
    state_db = getattr(request.app.state, "db_path", None)
    if state_db is not None:
        return Path(state_db)
    return runtime_db_path()


def _served_path(request: Request) -> Path:
    state_db = getattr(request.app.state, "served_db_path", None)
    if state_db is not None:
        return Path(state_db)
    return served_db_path()


def _admission_controller(request: Request) -> AdmissionController:
    controller = getattr(request.app.state, "admission_controller", None)
    if controller is None:
        from catalyst_app.runtime_wiring import build_default_admission_controller

        controller = build_default_admission_controller()
        request.app.state.admission_controller = controller
    return controller


def _cancellation_controller(request: Request):
    controller = getattr(request.app.state, "cancellation_controller", None)
    if controller is None:
        from catalyst_app.runtime_wiring import build_default_cancellation_controller

        controller = build_default_cancellation_controller()
        request.app.state.cancellation_controller = controller
    return controller


def _resolve_runtime_credential(
    request: CreateRunRequest,
    *,
    provider: str,
    credential_source: str,
) -> str | None:
    """Resolve the volatile BYOK credential value (browser_key or server_env).

    server_env resolves only ``get_provider_env_key(provider)``; a missing or
    empty provider-specific env key fails closed before any ACCEPTED run,
    event, or slot is created. Never logs or persists the value.
    """
    if credential_source == CredentialSource.BROWSER_KEY.value:
        if request.model is None:
            return None
        return (request.model.api_key or "").strip() or None
    if credential_source == CredentialSource.SERVER_ENV.value:
        env_key_name = get_provider_env_key(provider)
        if not env_key_name:
            raise HTTPException(status_code=400, detail="env_key_missing")
        value = os.environ.get(env_key_name, "").strip()
        if not value:
            raise HTTPException(status_code=400, detail="env_key_missing")
        return value
    return None


def _public_byok_base_url(provider: str, base_url: str | None) -> str | None:
    """Keep public BYOK on the catalog provider's known endpoint."""
    known = catalog_provider_base_url(provider)
    if known is None:
        raise HTTPException(status_code=400, detail=PUBLIC_BYOK_BASE_URL_DETAIL)
    if base_url is None or not base_url.strip():
        return None
    if base_url.strip().rstrip("/") != known.rstrip("/"):
        raise HTTPException(status_code=400, detail=PUBLIC_BYOK_BASE_URL_DETAIL)
    return known


def _resolve_run_model(
    request: CreateRunRequest, access: AccessContext
) -> tuple[str, str, str | None, str]:
    """Resolve the persisted provider/model/base URL and credential source.

    Anonymous and invite callers using the server key cannot select a model:
    omission means the fixed public model, while any other selection or any
    custom base URL is rejected before admission. BYOK and admin/local callers
    keep the existing request behavior.
    """
    model = request.model
    public_tier = access.tier in {"anonymous", "invite"}
    browser_key = (model.api_key or "").strip() if model is not None else ""
    is_browser_byok = (
        model is not None
        and model.credential_source == CredentialSource.BROWSER_KEY
        and bool(browser_key)
    )
    if public_tier and model is not None and model.credential_source == CredentialSource.BROWSER_KEY:
        if not is_browser_byok:
            return (
                PUBLIC_RUN_MODEL.provider,
                PUBLIC_RUN_MODEL.model_id,
                PUBLIC_RUN_MODEL.base_url,
                CredentialSource.SERVER_ENV.value,
            )
        return (
            model.provider,
            model.model_id,
            _public_byok_base_url(model.provider, model.base_url),
            CredentialSource.BROWSER_KEY.value,
        )

    if public_tier:
        requested_ids = [
            value
            for value in (
                request.model_id,
                model.model_id if model is not None else None,
            )
            if value is not None
        ]
        if any(value != PUBLIC_RUN_MODEL.model_id for value in requested_ids):
            raise HTTPException(status_code=400, detail=PUBLIC_MODEL_FIXED_DETAIL)
        if model is not None:
            if model.provider != PUBLIC_RUN_MODEL.provider:
                raise HTTPException(status_code=400, detail=PUBLIC_MODEL_FIXED_DETAIL)
            if model.base_url is not None:
                raise HTTPException(status_code=400, detail=PUBLIC_MODEL_FIXED_DETAIL)
        return (
            PUBLIC_RUN_MODEL.provider,
            PUBLIC_RUN_MODEL.model_id,
            PUBLIC_RUN_MODEL.base_url,
            CredentialSource.SERVER_ENV.value,
        )

    if (
        model is not None
        and model.credential_source == CredentialSource.BROWSER_KEY
        and not browser_key
    ):
        raise HTTPException(status_code=422, detail={"code": "invalid_request"})
    if model is None and request.model_id is None:
        raise HTTPException(status_code=422, detail={"code": "invalid_request"})
    provider = model.provider if model else "unknown"
    model_id = model.model_id if model else (request.model_id or "unknown")
    base_url = model.base_url if model else None
    credential_source = (
        model.credential_source.value if model is not None else "legacy"
    )
    return provider, model_id, base_url, credential_source


def _quota_caller_key(req: Request) -> str:
    salt = os.environ.get("CATALYST_QUOTA_HASH_KEY", "")
    if not salt:
        raise HTTPException(status_code=503, detail="runtime_unavailable: quota_config_missing")
    peer = req.client.host if req.client is not None else None
    source_ip = peer
    forwarded = req.headers.get("cf-connecting-ip")
    try:
        trusted = [ipaddress.ip_network(value.strip(), strict=False)
                   for value in os.environ.get("CATALYST_TRUSTED_PROXY_CIDRS", "").split(",")
                   if value.strip()]
        peer_ip = ipaddress.ip_address(peer) if peer else None
        if forwarded and peer_ip and any(peer_ip in network for network in trusted):
            source_ip = ipaddress.ip_address(forwarded.strip()).compressed
    except ValueError:
        source_ip = peer
    if not source_ip:
        raise HTTPException(status_code=503, detail="runtime_unavailable: quota_identity_unavailable")
    return hmac.new(salt.encode("utf-8"), source_ip.encode("utf-8"), hashlib.sha256).hexdigest()


def _budget_max_cost(request: CreateRunRequest, ledger: BudgetLedger) -> float:
    value = request.max_cost_usd
    if value is None:
        return ledger.max_run_cost_usd
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=400, detail="budget_invalid")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise HTTPException(status_code=400, detail="budget_invalid")
    return min(float(value), ledger.max_run_cost_usd)


def _http_access_context(req: Request) -> AccessContext:
    """Use the middleware-authenticated tier, with route-test fallback."""
    context = getattr(req.state, "access_context", None)
    return context if context is not None else resolve_access_context(req)


def _http_budget_admission(
    req: Request,
    access: AccessContext,
    ledger: BudgetLedger,
    *,
    max_cost_usd: float,
    max_tokens: int | None = None,
    global_budget: bool = True,
) -> BudgetAdmission:
    caller_key = (
        _quota_caller_key(req)
        if access.tier == "anonymous"
        else access.token_id if access.tier == "invite" else None
    )
    return BudgetAdmission(
        tier=access.tier,
        caller_key=caller_key,
        max_cost_usd=max_cost_usd,
        max_tokens=max_tokens,
        global_budget=global_budget and access.tier != "admin",
    )


# ── V1.1 run lifecycle ──────────────────────────────────────────────────────

@router.post(
    "/live-runs",
    response_model=RunAcceptedResponse | PublicCacheRunDTO | RunDTO,
    responses={
        429: {
            "model": RateLimitErrorResponse,
            "description": "Capacity or quota limit reached",
        }
    },
)
def create_live_run(
    request: CreateRunRequest,
    req: Request,
    response: Response,
    credential_store=Depends(get_credential_store),
) -> RunAcceptedResponse | RunDTO | PublicCacheRunDTO:
    """Durable admission: validate -> reserve capacity -> ACCEPTED + event."""
    access = _http_access_context(req)
    turnstile_token = req.headers.get("x-turnstile-token")
    if access.tier == "anonymous" and request.query is not None:
        raise HTTPException(status_code=400, detail="query_not_allowed")
    if access.tier != "admin" and request.force_full:
        raise HTTPException(status_code=400, detail="invalid_request: force_full is restricted")
    if re.fullmatch(r"[A-Z.]{1,10}", request.ticker.strip().upper()) is None:
        raise HTTPException(status_code=400, detail="ticker_not_supported")
    try:
        parsed_trade_date = datetime.strptime(request.trade_date, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid_request: trade_date") from exc
    if parsed_trade_date.isoformat() != request.trade_date:
        raise HTTPException(status_code=400, detail="invalid_request: trade_date")
    public_deployment = os.environ.get("CATALYST_ALLOW_NON_LOOPBACK") == "1"
    ready = None
    if public_deployment:
        from catalyst_app.routers.public_access import utc_now
        from catalyst_app.runtime.ready_range import (
            ReadyRangeUnavailable,
            load_ready_range,
        )

        try:
            ready = load_ready_range(now=utc_now())
        except ReadyRangeUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime_unavailable: prepared_range_unavailable",
            ) from exc
        if access.tier != "admin":
            pair = (request.ticker.strip().upper(), request.trade_date)
            if ready.status not in {"ready", "stale"} or pair not in ready.pairs:
                raise HTTPException(status_code=400, detail="not_in_allowlist")

    if access.tier == "anonymous" or turnstile_token is not None:
        require_valid_turnstile(turnstile_token)

    provider, model_id, base_url, credential_source = _resolve_run_model(request, access)
    cache_identity = (
        ready.data_identity
        if public_deployment and ready is not None and ready.status == "ready"
        else None
    )
    cache_key = None
    cache_eligible = access.tier == "anonymous" and credential_source != "browser_key"
    if cache_eligible and cache_identity:
        cache_key = compute_public_cache_key(
            ticker=request.ticker,
            session_date=request.trade_date,
            data_identity=cache_identity,
            provider=provider,
            model_id=model_id,
            base_url=base_url or "",
            config_version=request.config,
            strategy_identity=production_strategy_identity(),
        )
        cached_run_id = find_completed_public_run(_db_path(req), cache_key)
        if cached_run_id is not None:
            cached_dto = _read_run_dto(_db_path(req), cached_run_id)
            if cached_dto is not None:
                # Reuse never creates a run or reserves budget. Link the cached
                # result to this caller's anonymous session so P-7's
                # own-runs-only history rule remains intact.
                from catalyst_app.routers.public_access import (
                    set_anonymous_session_cookie,
                )

                set_anonymous_session_cookie(req, response, cached_run_id)
                return PublicCacheRunDTO(**cached_dto.model_dump())

    controller = _admission_controller(req)
    ledger = getattr(controller, "_budget_ledger", None) or BudgetLedger.from_environment()
    is_browser_key = credential_source == "browser_key"
    if is_browser_key:
        max_tokens = 300_000 if request.max_tokens is None else request.max_tokens
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            # Below 150k the largest single step (writer, about 136k) can never be reserved.
            or not 150_000 <= max_tokens <= 2_000_000
        ):
            raise HTTPException(status_code=400, detail="budget_invalid")
        max_cost = 0.0
    else:
        max_tokens = None
        max_cost = _budget_max_cost(request, ledger)
    if not is_browser_key and public_deployment and access.tier in {"anonymous", "invite"}:
        # A public run must be priceable before it is admitted. The checked-in
        # pricing file supplies both the identity-bound price and the role token
        # bounds; a missing/invalid file or an unknown model creates no run.
        from catalyst_app.runtime.model_pricing import (
            PricingUnavailable,
            load_pricing_config,
            priced_run_max_cost_usd,
        )

        try:
            pricing = load_pricing_config()
            max_cost = priced_run_max_cost_usd(
                pricing,
                provider=provider,
                model_id=model_id,
                ceiling_usd=max_cost,
            )
        except PricingUnavailable as exc:
            raise HTTPException(
                status_code=503, detail="MODEL_PRICE_UNAVAILABLE"
            ) from exc
    # Resolve the volatile credential before admission: browser keys come from
    # the request body; server_env keys come from the provider-specific env
    # var (never the generic AIHUBMIX_API_KEY fallback). The value is passed
    # to the pre-submit hook only, never placed on AdmissionRequest, manifest,
    # SQLite rows, events, artifacts, logs, responses, or exceptions.
    resolved_key = _resolve_runtime_credential(
        request,
        provider=provider,
        credential_source=credential_source,
    )
    pre_submit = (
        (lambda run_id: credential_store.register(run_id, api_key=resolved_key))
        if resolved_key is not None
        else None
    )
    try:
        outcome = create_live_run_service(
            controller=controller,
            request=request,
            provider=provider,
            model_id=model_id,
            base_url=base_url,
            credential_source=credential_source,
            budget=_http_budget_admission(
                req,
                access,
                ledger,
                max_cost_usd=max_cost,
                max_tokens=max_tokens,
                global_budget=credential_source != "browser_key",
            ),
            pre_submit=pre_submit,
            idempotency_key=req.headers.get("idempotency-key"),
            visibility="public" if cache_eligible else "private",
            public_cache_key=cache_key,
        )
    except AdmissionValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_request: {exc}") from exc
    except RuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"runtime_unavailable: {exc}") from exc

    if outcome.kind == "accepted":
        if public_deployment and access.tier == "anonymous":
            from catalyst_app.routers.public_access import set_anonymous_session_cookie

            set_anonymous_session_cookie(req, response, outcome.run_id or "")
        accepted = RunAcceptedResponse(
            run_id=outcome.run_id or "",
            status="ACCEPTED",
            stream_url=outcome.stream_url or "",
            request_hash_prefix=(outcome.request_hash or "")[:8],
            model_capability_label="streaming-v1",
            access={"tier": access.tier, "max_cost_usd": max_cost},
        )
        response.headers["X-Quota-Tier"] = access.tier
        response.headers["X-Quota-Remaining"] = str(
            outcome.quota_remaining if outcome.quota_remaining is not None else -1
        )
        return accepted
    if outcome.kind == "duplicate":
        dto = _read_run_dto(_db_path(req), outcome.run_id or "")
        if dto is None:  # pragma: no cover - duplicate implies a durable row
            raise HTTPException(status_code=404, detail="run_not_found")
        return dto
    if outcome.kind == "conflict":
        raise HTTPException(status_code=409, detail="idempotency_key_conflict")
    if outcome.kind == "capacity_exceeded":
        raise HTTPException(status_code=429, detail="capacity_exceeded")
    if outcome.kind in {"rate_limited_ip", "rate_limited_token", "daily_budget_exhausted"}:
        content = {
            "detail": outcome.kind,
            "message": QUOTA_EXHAUSTED_MESSAGE,
        }
        if featured_is_ready():
            content["featured_url"] = "/api/featured"
        return JSONResponse(status_code=429, content=content)
    if outcome.kind == "unavailable":
        raise HTTPException(status_code=503, detail="executor_unavailable")
    raise HTTPException(status_code=400, detail="invalid_request")  # pragma: no cover


@router.get("/live-runs/{run_id}", response_model=RunDTO)
def get_live_run(run_id: str, req: Request) -> RunDTO:
    require_admin(req)
    dto = _read_run_dto(_db_path(req), run_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return dto


@router.get("/live-runs/{run_id}/view", response_model=RunViewDTO)
def get_live_run_view(run_id: str, req: Request) -> RunViewDTO:
    access = _http_access_context(req)
    if not _can_read_run_view(req, access, run_id):
        raise HTTPException(status_code=404, detail="run_not_found")
    try:
        view = _read_run_view(_db_path(req), run_id)
    except RunViewDataError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if view is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    # The run policy limit lives in the admission reservation; consumed tokens
    # live with the provider-accounting artifact, alongside its cost snapshot.
    try:
        with open_readonly(_db_path(req)) as conn:
            reservation = conn.execute(
                "SELECT max_run_tokens FROM run_budget_reservations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            accounting_row = conn.execute(
                "SELECT payload_json FROM run_artifacts WHERE run_id=? "
                "AND artifact_type='run_diagnostics' "
                "ORDER BY event_seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        max_tokens = reservation["max_run_tokens"] if reservation is not None else None
        provider_accounting = {}
        if accounting_row is not None:
            payload = json.loads(accounting_row["payload_json"])
            provider_accounting = payload.get("provider") or {}
        view = view.model_copy(
            update={
                # Only a token-bounded run gains a policy entry; an old run keeps policy None.
                "run": (
                    view.run.model_copy(
                        update={"policy": {**(view.run.policy or {}), "max_tokens": max_tokens}}
                    )
                    if max_tokens is not None
                    else view.run
                ),
                "cost": view.cost.model_copy(
                    update={
                        "max_tokens": max_tokens,
                        # Diagnostics record reported input and output tokens; either one
                        # missing means the provider did not report usage for some call.
                        "used_tokens": (
                            provider_accounting["total_tokens_in"] + provider_accounting["total_tokens_out"]
                            if isinstance(provider_accounting.get("total_tokens_in"), int)
                            and isinstance(provider_accounting.get("total_tokens_out"), int)
                            else None
                        ),
                        "token_method": (
                            "upper_bound_charged"
                            if provider_accounting.get("cost_method") == "upper_bound_charged"
                            else "reported"
                        ),
                    }
                ),
            }
        )
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        # A malformed optional accounting artifact must not make the run view
        # unavailable; the base view still reports its cost and lifecycle.
        pass
    if access.tier != "admin":
        view = project_public_run_view(view)
    return view


def _can_read_run_view(
    req: Request, access: AccessContext, run_id: str, *, db_path: Path | None = None,
    allow_public: bool = True,
) -> bool:
    """Apply the run-list session rule to direct result-page reads."""
    if access.tier == "admin":
        return True
    path = db_path if db_path is not None else _db_path(req)
    try:
        with open_readonly(path) as conn:
            run = conn.execute(
                "SELECT visibility FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                return False
            if access.tier == "invite":
                if allow_public and run["visibility"] == "public":
                    return True
                return conn.execute(
                    "SELECT 1 FROM run_budget_reservations"
                    " WHERE run_id=? AND tier='invite' AND caller_key=? LIMIT 1",
                    (run_id, access.token_id),
                ).fetchone() is not None
            from catalyst_app.routers.public_access import _session_identity

            session = _session_identity(req)
            if session is None:
                return False
            return conn.execute(
                "SELECT 1 FROM anonymous_session_runs"
                " WHERE session_hash=? AND utc_day=? AND run_id=? LIMIT 1",
                (*session, run_id),
            ).fetchone() is not None
    except Exception:
        return False


@router.post("/live-runs/{run_id}/verify-source", response_model=RunAcceptedResponse)
def verify_live_source(
    run_id: str,
    body: VerifySourceRequest,
    req: Request,
    credential_store=Depends(get_credential_store),
) -> RunAcceptedResponse:
    """Start a user-selected, one-page verification as a child run."""
    require_admin(req)
    access = _http_access_context(req)
    db_path = _db_path(req)
    with open_readonly(db_path) as conn:
        parent = conn.execute(
            "SELECT ticker, provider, base_url FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        manifest_row = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id=? AND artifact_type='run_manifest' "
            "ORDER BY event_seq ASC LIMIT 1", (run_id,)
        ).fetchone()
    if parent is None or manifest_row is None:
        raise HTTPException(status_code=404, detail={"code": "run_not_found"})
    from catalyst_data.ingestion.collectors.web_search import get_pre_lead

    lead = get_pre_lead(body.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail={"code": "pre_lead_not_found"})
    import json

    try:
        parent_manifest = json.loads(manifest_row["payload_json"])
        session_date = str(parent_manifest["temporal_identity"]["session_date"])
        model_id = str(parent_manifest["analyst_model_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=409, detail={"code": "parent_run_context_unavailable"})
    ticker = str(parent["ticker"] or "").upper()
    if lead["ticker"] != ticker or lead["session_date"] != session_date:
        raise HTTPException(status_code=409, detail={"code": "lead_run_mismatch"})
    provider = str(parent["provider"] or "unknown")
    env_key_name = get_provider_env_key(provider)
    runtime_key = os.environ.get(env_key_name, "").strip() if env_key_name else ""
    if not runtime_key:
        raise HTTPException(status_code=400, detail={"code": "model_server_key_missing"})
    admission_request = build_verify_source_admission_request(
        ticker=ticker,
        session_date=session_date,
        provider=provider,
        model_id=model_id,
        base_url=parent["base_url"],
        lead_id=body.lead_id,
        idempotency_key=req.headers.get("idempotency-key"),
        parent_run_id=run_id,
    )
    controller = _admission_controller(req)
    ledger = getattr(controller, "_budget_ledger", None) or BudgetLedger.from_environment()
    admission_request = dataclass_replace(
        admission_request,
        budget=_http_budget_admission(
            req,
            access,
            ledger,
            max_cost_usd=ledger.max_run_cost_usd,
        ),
        origin="http",
    )
    try:
        outcome = controller.admit(
            admission_request,
            pre_submit=lambda child_id: credential_store.register(child_id, api_key=runtime_key),
        )
    except (AdmissionValidationError, RuntimeUnavailableError) as exc:
        raise HTTPException(status_code=400, detail={"code": "source_check_admission_failed"}) from exc
    if outcome.kind == "accepted":
        return RunAcceptedResponse(
            run_id=outcome.run_id or "", status="ACCEPTED", stream_url=outcome.stream_url or "",
            request_hash_prefix=(outcome.request_hash or "")[:8], model_capability_label="streaming-v1",
        )
    if outcome.kind == "capacity_exceeded":
        raise HTTPException(status_code=429, detail={"code": "capacity_exceeded"})
    if outcome.kind == "unavailable":
        raise HTTPException(status_code=503, detail={"code": "executor_unavailable"})
    if outcome.kind == "conflict":
        raise HTTPException(status_code=409, detail={"code": "idempotency_key_conflict"})
    if outcome.kind == "duplicate":
        return RunAcceptedResponse(
            run_id=outcome.run_id or "", status="ACCEPTED",
            stream_url=f"/api/live-runs/{outcome.run_id}/stream",
            request_hash_prefix=(outcome.request_hash or "")[:8], model_capability_label="streaming-v1",
        )
    raise HTTPException(status_code=400, detail={"code": "source_check_admission_failed"})


@router.post("/live-runs/{run_id}/cancel", response_model=CancelResponse)
def cancel_live_run(run_id: str, req: Request) -> CancelResponse:
    """Idempotent cooperative cancellation (Final Migration TSD §20.1).

    ACCEPTED cancellation is terminal; RUNNING persists CANCEL_REQUESTED and
    signals the shared control token; CANCELLED is reached only when work
    stops or late output is safely discarded. Repeated cancels return an
    acknowledgement. No cancel-all surface.
    """
    access = _http_access_context(req)
    if not _can_read_run_view(req, access, run_id, allow_public=False):
        raise HTTPException(status_code=404, detail="run_not_found")
    controller = _cancellation_controller(req)
    from catalyst_app.persistence.events import RunNotFoundError

    try:
        outcome = controller.request_cancel(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="run_not_found") from exc
    return CancelResponse(
        run_id=outcome.run_id,
        status=outcome.status,
        acknowledged=outcome.acknowledged,
    )


def _read_run_dto(db_path: Path, run_id: str) -> RunDTO | None:
    from catalyst_app.lifecycle import RunLifecycleStatus

    with open_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at,"
            " failure_code, failure_message, owner, task_token"
            " FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        lifecycle = RunLifecycleStatus(row["lifecycle_status"])
        max_seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()
        terminal_refs_rows = conn.execute(
            "SELECT artifact_id, artifact_type, payload_hash FROM run_artifacts"
            " WHERE run_id = ? AND event_seq = ? ORDER BY artifact_id",
            (run_id, int(max_seq_row[0])),
        ).fetchall()
        attribution_row = None
        if lifecycle is RunLifecycleStatus.COMPLETED:
            attribution_row = conn.execute(
                "SELECT payload_json FROM run_artifacts"
                " WHERE run_id = ? AND artifact_type = 'attribution_result' ORDER BY event_seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
    attribution_status: str | None = None
    attribution_type: str | None = None
    if attribution_row is not None:
        try:
            attribution = json.loads(attribution_row["payload_json"])
            attribution_status = attribution.get("attribution_status")
            attribution_type = attribution.get("attribution_type")
        except (json.JSONDecodeError, TypeError):
            pass

    created_at = utc_parse(row["created_at"])
    updated_at = utc_parse(row["updated_at"])
    duration_ms = None
    if created_at is not None and updated_at is not None:
        duration_ms = int((updated_at - created_at).total_seconds() * 1000)

    failure = None
    if lifecycle is RunLifecycleStatus.FAILED:
        failure = RunFailureDTO(
            code=row["failure_code"] or "FAILED",
            message=row["failure_message"],
        )

    return RunDTO(
        run_id=row["run_id"],
        lifecycle_status=lifecycle,
        attribution_status=attribution_status,
        attribution_type=attribution_type,
        created_at=created_at or datetime.now(timezone.utc),
        duration_ms=duration_ms,
        failure=failure,
        manifest_summary={
            "run_manifest_id": row["run_manifest_id"],
            "manifest_hash": row["manifest_hash"],
            "request_hash": row["request_hash"],
        },
        terminal_artifact_refs=tuple(
            ArtifactRefDTO(
                artifact_id=r["artifact_id"],
                artifact_type=r["artifact_type"],
                schema_version="v1",
                content_sha256=r["payload_hash"],
                run_id=run_id,
            )
            for r in terminal_refs_rows
        ),
    )


# ── artifacts / evidence / claims ───────────────────────────────────────────

class ArtifactRefItem(BaseModel):
    """Paged artifact ref: metadata only, never the payload body."""

    model_config = {"extra": "forbid"}

    artifact_id: str
    artifact_type: str
    stage: str | None = None
    created_at: datetime | None = None
    ref: ArtifactRefDTO


@router.get("/live-runs/{run_id}/artifacts")
def get_live_run_artifacts(
    run_id: str,
    req: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    artifact_type: str | None = Query(default=None),
) -> dict[str, Any]:
    require_admin(req)
    db_path = _db_path(req)
    if not _run_exists(db_path, run_id):
        raise HTTPException(status_code=404, detail="run_not_found")
    with open_readonly(db_path) as conn:
        where = "WHERE run_id = ?"
        params: list[Any] = [run_id]
        if artifact_type:
            where += " AND artifact_type = ?"
            params.append(artifact_type)
        total = conn.execute(
            f"SELECT COUNT(*) FROM run_artifacts {where}", tuple(params)
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT artifact_id, artifact_type, event_seq, payload_hash, payload_json"
            f" FROM run_artifacts {where} ORDER BY artifact_id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {
        "items": [
            ArtifactRefItem(
                artifact_id=row["artifact_id"],
                artifact_type=row["artifact_type"],
                stage=None,
                created_at=None,
                ref=ArtifactRefDTO(
                    artifact_id=row["artifact_id"],
                    artifact_type=row["artifact_type"],
                    schema_version="v1",
                    content_sha256=row["payload_hash"],
                    run_id=run_id,
                ),
            )
            for row in rows
        ],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


@router.get("/live-runs/{run_id}/artifacts/{artifact_id}", response_model=ArtifactDTO)
def get_live_run_artifact(run_id: str, artifact_id: str, req: Request) -> ArtifactDTO:
    require_admin(req)
    with open_readonly(_db_path(req)) as conn:
        row = conn.execute(
            "SELECT artifact_id, artifact_type, event_seq, payload_hash, payload_json"
            " FROM run_artifacts WHERE run_id = ? AND artifact_id = ?",
            (run_id, artifact_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="artifact_not_found")
    return _artifact_dto(run_id, row, include_payload=True)


def _artifact_dto(run_id: str, row: Any, *, include_payload: bool = False) -> ArtifactDTO:
    payload: dict[str, Any] | None = None
    if include_payload:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            payload = None
    return ArtifactDTO(
        artifact_id=row["artifact_id"],
        artifact_type=row["artifact_type"],
        stage=None,
        created_at=None,
        ref=ArtifactRefDTO(
            artifact_id=row["artifact_id"],
            artifact_type=row["artifact_type"],
            schema_version="v1",
            content_sha256=row["payload_hash"],
            run_id=run_id,
        ),
        payload=payload,
    )


@router.get("/live-runs/{run_id}/evidence/{evidence_id}", response_model=EvidenceDetailDTO)
def get_evidence_detail(run_id: str, evidence_id: str, req: Request) -> EvidenceDetailDTO:
    require_admin(req)
    payload = _read_typed_artifact(
        _db_path(req), run_id, "evidence_detail", "evidence_id", evidence_id
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="evidence_not_found")
    return EvidenceDetailDTO.model_validate(payload)


@router.get("/live-runs/{run_id}/claims/{claim_id}", response_model=ClaimDetailDTO)
def get_claim_detail(run_id: str, claim_id: str, req: Request) -> ClaimDetailDTO:
    require_admin(req)
    payload = _read_typed_artifact(
        _db_path(req), run_id, "claim_detail", "claim_id", claim_id
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="claim_not_found")
    return ClaimDetailDTO.model_validate(payload)


def _read_typed_artifact(
    db_path: Path, run_id: str, artifact_type: str, payload_key: str, payload_id: str
) -> dict[str, Any] | None:
    """Read one typed artifact whose payload carries the canonical ID.

    The artifact envelope id is app-assigned; the canonical evidence/claim ID
    lives inside the typed payload (Final TSD §20.1).
    """
    with open_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM run_artifacts"
            " WHERE run_id = ? AND artifact_type = ?"
            " AND json_extract(payload_json, ?) = ?"
            " ORDER BY event_seq ASC LIMIT 1",
            (run_id, artifact_type, f"$.{payload_key}", payload_id),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _run_exists(db_path: Path, run_id: str) -> bool:
    with open_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT run_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    return row is not None


# ── health ──────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthDTO)
def health(req: Request) -> HealthDTO:
    """Process/SQLite/executor/event-store/runtime readiness (Final TSD §20.1)."""
    db_path = _db_path(req)
    served_path = _served_path(req)
    runtime_db = "failed"
    event_store = "failed"
    if db_path.is_file():
        try:
            with open_readonly(db_path) as conn:
                run_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
                ).fetchone()
                if run_table is not None:
                    runtime_db = "ready"
                event_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='run_events'"
                ).fetchone()
                if event_table is not None:
                    event_store = "ready"
        except Exception:
            runtime_db = "failed"
            event_store = "failed"

    served_db = "failed"
    if served_path.is_file():
        try:
            with open_readonly(served_path) as conn:
                conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            served_db = "ready"
        except Exception:
            served_db = "failed"

    controller = getattr(req.app.state, "admission_controller", None)
    executor_status: str = "ready"
    if controller is not None and controller.executor.is_closed:
        executor_status = "degraded"

    # Runtime readiness: the production graph/dependency composition must be
    # wired and identity readiness must pass (Finding J). A composition whose
    # dependency loader is not ready must never report the runtime ready.
    runtime_ready = True
    runtime_reason: str | None = None
    runtime_warming = False
    composition = getattr(req.app.state, "runtime_composition", None)
    if composition is not None:
        try:
            loader = composition.dependency_loader
            runtime_health = getattr(loader, "cached_health", None)
            if runtime_health is None:
                cached = getattr(loader, "cached_dependencies", None)
                runtime_health = cached.health if cached is not None else None
            if runtime_health is None:
                runtime_health = {"status": "degraded"}
                runtime_ready = False
                runtime_warming = True
                runtime_reason = "runtime warming up"
            runtime_ready = runtime_health.get("status") == "ready"
            errors = runtime_health.get("errors") or []
            if errors and not runtime_warming:
                runtime_reason = str(errors[0].get("message") or "runtime dependency readiness failed")
        except Exception:
            runtime_ready = False
            runtime_reason = "runtime dependency health check failed"
    elif controller is None:
        runtime_ready = False

    # Public admission requires the checked-in pricing configuration. A missing
    # or malformed file (or an override with missing production roles) must
    # never report the runtime ready.
    pricing_ready = True
    try:
        from catalyst_app.runtime.model_pricing import load_pricing_config

        pricing = load_pricing_config()
        pricing.price_for(PUBLIC_RUN_MODEL.provider, PUBLIC_RUN_MODEL.model_id)
    except Exception:
        pricing_ready = False

    status = "ready"
    if "failed" in (served_db, runtime_db, event_store):
        status = "failed"
    elif runtime_warming:
        status = "degraded"
    elif not runtime_ready or not pricing_ready:
        status = "failed"
    elif "degraded" in (executor_status,):
        status = "degraded"
    from catalyst_app.runtime.composition import (
        provider_account_error_count,
        store_read_failure_count,
    )

    return HealthDTO(
        status=status,
        process="ready",
        executor=executor_status,
        served_db=served_db,
        runtime_db=runtime_db,
        event_store=event_store,
        store_read_failures=store_read_failure_count(),
        provider_account_errors=provider_account_error_count(),
        reason=runtime_reason,
        press_release_archive={"status": "disabled", "reason": "retired"},
        featured=featured_health_status(cached_only=True),
    )


# ── migration-window compatibility (packages/api only) ──────────────────────

@router.get("/live-runs/{run_id}/events", response_model=list[RunEventResponse])
def get_live_run_events(
    run_id: str,
    req: Request,
    response: Response,
    after_seq: int | None = Query(default=None, ge=0),
    service=Depends(get_live_run_service),
) -> list[RunEventResponse]:
    require_admin(req)
    _mark_deprecated(response, endpoint="events")
    rows = service.get_events(run_id, after_seq=after_seq)
    return [_as_run_event_response(row) for row in rows]


@router.get(
    "/live-runs/{run_id}/workspace",
    response_model=WorkbenchProjectionDTO | WorkspaceResponse,
)
def get_workspace(
    run_id: str,
    req: Request,
    response: Response,
    service=Depends(get_live_run_service),
) -> WorkbenchProjectionDTO | WorkspaceResponse:
    """Authoritative V1.1 workspace projection over the V1 tables (Finding G).

    V1 runs return the typed ``WorkbenchProjectionDTO`` from
    ``project_workspace_v1``. Legacy saved runs fall through to the bounded
    migration adapter (``project_workspace`` -> ``WorkspaceResponse``) through
    the same endpoint; only that legacy translation is marked deprecated.
    """
    require_admin(req)
    db_path = _db_path(req)
    try:
        projection = project_workspace_v1(db_path, run_id)
    except KeyError:
        pass  # not a V1 run; try the legacy saved-artifact translation
    else:
        return WorkbenchProjectionDTO.model_validate(projection)
    _mark_deprecated(response, endpoint="workspace")
    summary = service.get_run(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    events = service.get_events(run_id)
    artifacts = service.get_artifacts(run_id)
    projection = project_workspace(summary, events, artifacts)
    return WorkspaceResponse.model_validate(projection)


@router.post("/live-runs/{run_id}/retry", response_model=RetryRunResponse)
def retry_live_run(
    run_id: str,
    request: RetryRunRequest,
    req: Request,
    service=Depends(get_live_run_service),
    credential_store=Depends(get_credential_store),
) -> RetryRunResponse:
    """Legacy retry: creates a newly admitted linked run; never reopens a
    terminal row. Remains a compatibility translation during the M6 window."""
    require_admin(req)
    model_payload = None
    runtime_key: str | None = None
    if request.model:
        model_payload = {
            "provider": request.model.provider,
            "model_id": request.model.model_id,
            "base_url": request.model.base_url,
            "credential_source": request.model.credential_source.value,
        }
        if request.model.credential_source == CredentialSource.SERVER_ENV:
            env_key_name = get_provider_env_key(request.model.provider)
            runtime_key = os.environ.get(env_key_name, "") if env_key_name else ""
            if not runtime_key:
                return RetryRunResponse(
                    ok=False,
                    failure=FailurePayload(
                        status=RunStatus.FAILED_REQUEST,
                        sub_reason="env_key_missing",
                        message=f"Server env key not configured for provider '{request.model.provider}'.",
                        retryable=False,
                    ),
                )
        else:
            runtime_key = request.model.api_key
    elif request.model_id:
        model_payload = request.model_id

    result = service.retry_run(run_id, model=model_payload)
    if request.model and result.get("ok") and result.get("run_id") and runtime_key:
        credential_store.register(result["run_id"], api_key=runtime_key)
    return _as_retry_response(result)


@router.get("/models/catalog", response_model=ModelCatalogResponse)
def model_catalog() -> ModelCatalogResponse:
    """Return sanitized model catalog. Never includes env key values."""
    return build_catalog()


@router.get("/health/runtime", response_model=RuntimeHealthResponse)
def runtime_health(loader=Depends(get_runtime_dependency_loader)) -> RuntimeHealthResponse:
    return RuntimeHealthResponse.model_validate(loader.health())


@router.post("/models/validate", response_model=ModelValidateResponse)
def validate_model(
    request: ModelValidateRequest,
    x_provider_key: str | None = Header(default=None, alias="X-Provider-Key"),
    x_tavily_key: str | None = Header(default=None, alias="X-Tavily-Key"),
) -> ModelValidateResponse:
    """Probe a provider using a header-only credential."""
    api_key = x_tavily_key if request.provider == "tavily" else x_provider_key
    if not api_key:
        return ModelValidateResponse(ok=False, error_code="key_missing")
    result = validate_provider(
        provider=request.provider,
        model_id=request.model_id,
        api_key=api_key or "",
    )
    return ModelValidateResponse(
        ok=result.ok,
        error_code=None if result.ok else (
            "unsupported_provider" if result.status == "invalid" else result.status
        ),
    )


@router.get("/models", response_model=ModelsResponse)
def list_models(req: Request) -> ModelsResponse:
    access = _http_access_context(req)
    if access.tier in {"anonymous", "invite"}:
        return ModelsResponse(
            models=[
                ModelOption(
                    model_id=PUBLIC_RUN_MODEL.model_id,
                    label=PUBLIC_RUN_MODEL.model_id,
                    is_default=True,
                )
            ],
            default_model_id=PUBLIC_RUN_MODEL.model_id,
        )
    options = [
        ModelOption(
            model_id=model_id,
            label=model_id,
            is_default=(model_id == DEFAULT_MODEL),
        )
        for model_id in SUPPORTED_MODELS
    ]
    return ModelsResponse(models=options, default_model_id=DEFAULT_MODEL)


# ── legacy response adapters ────────────────────────────────────────────────

def _as_run_event_response(payload: dict[str, Any]) -> RunEventResponse:
    allowed = set(RunEventResponse.model_fields.keys())
    normalized = {key: value for key, value in payload.items() if key in allowed}
    return RunEventResponse.model_validate(normalized)


def _as_retry_response(payload: dict[str, Any]) -> RetryRunResponse:
    normalized = dict(payload)
    failure = normalized.get("failure")
    if isinstance(failure, dict):
        failure.setdefault("status", "FAILED_REQUEST")
        failure.setdefault("retryable", False)
    return RetryRunResponse.model_validate(normalized)
