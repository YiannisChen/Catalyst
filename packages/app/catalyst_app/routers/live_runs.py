"""V1.1 run orchestration API surface (M6-9) with bounded migration compat.

Final Migration TSD §20.1: POST /api/live-runs (ACCEPTED + stream_url +
optional Idempotency-Key), GET run (lifecycle + nullable attribution +
manifest refs), paged artifacts, same-run artifact projection,
evidence/claims details. Legacy GET /events, /workspace, /retry and /models/*
remain translated in packages/app only (M6 migration window); cancel-all is
not part of the normal public API.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

_logger = logging.getLogger(__name__)


def _mark_deprecated(response: Response, *, endpoint: str) -> None:
    """Flag migration-window compat callers for the M8 audit (M6-12)."""
    response.headers["Deprecation"] = "true"
    _logger.info("deprecated compat endpoint called: %s", endpoint)

from catalyst_app.api_dto import (
    ArtifactDTO,
    ArtifactRefDTO,
    CancelResponse,
    ClaimDetailDTO,
    EvidenceDetailDTO,
    HealthDTO,
    RunAcceptedResponse,
    RunDTO,
    RunFailureDTO,
    WorkbenchProjectionDTO,
)
from catalyst_app.dependencies import get_credential_store, get_live_run_service, get_runtime_dependency_loader
from catalyst_app.env_loader import get_provider_env_key
from catalyst_app.llm_factory import SUPPORTED_MODELS, DEFAULT_MODEL
from catalyst_app.model_catalog import build_catalog
from catalyst_app.provider_validator import validate_provider
from catalyst_app.schemas import (
    CredentialSource,
    FailurePayload,
    RunStatus,
    ArtifactType,
    CreateRunRequest,
    CreateRunResponse,
    ModelOption,
    ModelCatalogResponse,
    ModelValidateRequest,
    ModelValidateResponse,
    ModelsResponse,
    RetryRunRequest,
    RetryRunResponse,
    RunEventResponse,
    RunSummaryResponse,
    RuntimeHealthResponse,
    WorkspaceResponse,
)
from catalyst_app.runtime.admission import (
    AdmissionController,
    AdmissionRequest,
    AdmissionValidationError,
)
from catalyst_app.runtime.composition import RuntimeUnavailableError
from catalyst_app.workspace_projection import project_workspace, project_workspace_v1
from catalyst_data.storage.connect import open_readonly

router = APIRouter(prefix="/api", tags=["live-runs"])


def _db_path(request: Request) -> Path:
    state_db = getattr(request.app.state, "db_path", None)
    if state_db is not None:
        return Path(state_db)
    import os

    raw = os.getenv("CATALYST_DB_PATH")
    if raw:
        return Path(raw)
    return Path(".local/live_runtime.db")


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


def _utc_parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_runtime_credential(request: CreateRunRequest) -> str | None:
    """Resolve the volatile BYOK credential value (browser_key or server_env).

    Returns None when no credential is supplied; never logs or persists it.
    """
    model = request.model
    if model is None:
        return None
    if model.credential_source == CredentialSource.BROWSER_KEY:
        return model.api_key or None
    if model.credential_source == CredentialSource.SERVER_ENV:
        env_key_name = get_provider_env_key(model.provider)
        if not env_key_name:
            return None
        return os.environ.get(env_key_name, "") or None
    return None


# ── V1.1 run lifecycle ──────────────────────────────────────────────────────

@router.post("/live-runs", response_model=RunAcceptedResponse | RunDTO)
def create_live_run(
    request: CreateRunRequest,
    req: Request,
    credential_store=Depends(get_credential_store),
) -> RunAcceptedResponse | RunDTO:
    """Durable admission: validate -> reserve capacity -> ACCEPTED + event."""
    controller = _admission_controller(req)
    model = request.model
    provider = model.provider if model else "unknown"
    model_id = model.model_id if model else (request.model_id or "unknown")
    base_url = model.base_url if model else None
    credential_source = model.credential_source.value if model else "legacy"
    admission_request = AdmissionRequest(
        ticker=request.ticker,
        session_date=request.trade_date,
        query=request.query,
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        credential_source_identifier=credential_source,
        workflow_version="v1.1",
        config_version=request.config,
        idempotency_key=req.headers.get("idempotency-key"),
    )
    # Resolve the volatile credential before admission: browser keys come from
    # the request body; server_env keys come from the provider-specific env
    # var (never the generic AIHUBMIX_API_KEY fallback). The value is passed
    # to the pre-submit hook only, never placed on AdmissionRequest, manifest,
    # SQLite rows, events, artifacts, logs, responses, or exceptions.
    resolved_key = _resolve_runtime_credential(request)
    pre_submit = (
        (lambda run_id: credential_store.register(run_id, api_key=resolved_key))
        if resolved_key is not None
        else None
    )
    try:
        outcome = controller.admit(admission_request, pre_submit=pre_submit)
    except AdmissionValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_request: {exc}") from exc
    except RuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"runtime_unavailable: {exc}") from exc

    if outcome.kind == "accepted":
        return RunAcceptedResponse(
            run_id=outcome.run_id or "",
            status="ACCEPTED",
            stream_url=outcome.stream_url or "",
            request_hash_prefix=(outcome.request_hash or "")[:8],
            model_capability_label="streaming-v1",
        )
    if outcome.kind == "duplicate":
        dto = _read_run_dto(_db_path(req), outcome.run_id or "")
        if dto is None:  # pragma: no cover - duplicate implies a durable row
            raise HTTPException(status_code=404, detail="run_not_found")
        return dto
    if outcome.kind == "conflict":
        raise HTTPException(status_code=409, detail="idempotency_key_conflict")
    if outcome.kind == "capacity_exceeded":
        raise HTTPException(status_code=429, detail="capacity_exceeded")
    if outcome.kind == "unavailable":
        raise HTTPException(status_code=503, detail="executor_unavailable")
    raise HTTPException(status_code=400, detail="invalid_request")  # pragma: no cover


@router.get("/live-runs/{run_id}", response_model=RunDTO)
def get_live_run(run_id: str, req: Request) -> RunDTO:
    dto = _read_run_dto(_db_path(req), run_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return dto


@router.post("/live-runs/{run_id}/cancel", response_model=CancelResponse)
def cancel_live_run(run_id: str, req: Request) -> CancelResponse:
    """Idempotent cooperative cancellation (Final Migration TSD §20.1).

    ACCEPTED cancellation is terminal; RUNNING persists CANCEL_REQUESTED and
    signals the shared control token; CANCELLED is reached only when work
    stops or late output is safely discarded. Repeated cancels return an
    acknowledgement. No cancel-all surface.
    """
    controller = _cancellation_controller(req)
    outcome = controller.request_cancel(run_id)
    return CancelResponse(
        run_id=outcome.run_id,
        status=outcome.status,
        acknowledged=outcome.acknowledged,
    )


def _read_run_dto(db_path: Path, run_id: str) -> RunDTO | None:
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
        max_seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()
        terminal_refs_rows = conn.execute(
            "SELECT artifact_id, artifact_type, payload_hash FROM run_artifacts"
            " WHERE run_id = ? AND event_seq = ? ORDER BY artifact_id",
            (run_id, int(max_seq_row[0])),
        ).fetchall()
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

    from catalyst_app.lifecycle import RunLifecycleStatus

    lifecycle = RunLifecycleStatus(row["lifecycle_status"])
    created_at = _utc_parse(row["created_at"])
    updated_at = _utc_parse(row["updated_at"])
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
    payload = _read_typed_artifact(
        _db_path(req), run_id, "evidence_detail", "evidence_id", evidence_id
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="evidence_not_found")
    return EvidenceDetailDTO.model_validate(payload)


@router.get("/live-runs/{run_id}/claims/{claim_id}", response_model=ClaimDetailDTO)
def get_claim_detail(run_id: str, claim_id: str, req: Request) -> ClaimDetailDTO:
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
    runtime_db = "failed"
    event_store = "failed"
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

    controller = getattr(req.app.state, "admission_controller", None)
    executor_status: str = "ready"
    if controller is not None and controller.executor.is_closed:
        executor_status = "degraded"

    # Runtime readiness: the production graph/dependency composition must be
    # wired and identity readiness must pass (Finding J). A composition whose
    # dependency loader is not ready must never report the runtime ready.
    runtime_ready = True
    composition = getattr(req.app.state, "runtime_composition", None)
    if composition is not None:
        try:
            runtime_ready = (
                getattr(composition.dependency_loader, "health", lambda: {"status": "failed"})()
                .get("status")
                == "ready"
            )
        except Exception:
            runtime_ready = False
    elif controller is None:
        runtime_ready = False

    status = "ready"
    if "failed" in (runtime_db, event_store):
        status = "failed"
    elif not runtime_ready:
        status = "failed"
    elif "degraded" in (executor_status,):
        status = "degraded"
    return HealthDTO(
        status=status,
        process="ready",
        executor=executor_status,
        runtime_db=runtime_db,
        event_store=event_store,
    )


# ── migration-window compatibility (packages/app only) ──────────────────────

@router.get("/live-runs/{run_id}/events", response_model=list[RunEventResponse])
def get_live_run_events(
    run_id: str,
    response: Response,
    after_seq: int | None = Query(default=None, ge=0),
    service=Depends(get_live_run_service),
) -> list[RunEventResponse]:
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
    service=Depends(get_live_run_service),
    credential_store=Depends(get_credential_store),
) -> RetryRunResponse:
    """Legacy retry: creates a newly admitted linked run; never reopens a
    terminal row. Remains a compatibility translation during the M6 window."""
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
def validate_model(request: ModelValidateRequest) -> ModelValidateResponse:
    """Probe provider connectivity. API key is never echoed in response."""
    result = validate_provider(
        provider=request.provider,
        model_id=request.model_id,
        api_key=request.api_key,
        base_url=request.base_url,
        credential_source=request.credential_source.value
            if isinstance(request.credential_source, CredentialSource)
            else request.credential_source,
    )
    return ModelValidateResponse(
        ok=result.ok,
        provider=request.provider,
        model_id=request.model_id,
        status=result.status,
        message=result.message,
        latency_ms=result.latency_ms,
    )


@router.get("/models", response_model=ModelsResponse)
def list_models() -> ModelsResponse:
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
