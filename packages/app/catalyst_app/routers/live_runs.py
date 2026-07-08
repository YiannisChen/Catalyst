from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from catalyst_app.dependencies import get_credential_store, get_live_run_service, get_runtime_dependency_loader
from catalyst_app.env_loader import get_provider_env_key
from catalyst_app.llm_factory import SUPPORTED_MODELS, DEFAULT_MODEL
from catalyst_app.model_catalog import build_catalog
from catalyst_app.provider_validator import validate_provider
from catalyst_app.schemas import (
    CredentialSource,
    FailurePayload,
    RunStatus,
    ArtifactResponse,
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
from catalyst_app.workspace_projection import project_workspace


router = APIRouter(prefix="/api", tags=["live-runs"])


def _as_create_run_response(payload: dict[str, Any], *, model_id: str | None) -> CreateRunResponse:
    normalized = dict(payload)
    normalized.setdefault("model_id", model_id)
    return CreateRunResponse.model_validate(normalized)


def _as_run_summary_response(payload: dict[str, Any]) -> RunSummaryResponse:
    allowed = set(RunSummaryResponse.model_fields.keys())
    normalized = {key: value for key, value in payload.items() if key in allowed}
    return RunSummaryResponse.model_validate(normalized)


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


@router.post("/live-runs", response_model=CreateRunResponse)
def create_live_run(
    request: CreateRunRequest,
    service=Depends(get_live_run_service),
    credential_store=Depends(get_credential_store),
) -> CreateRunResponse:
    # Extract model metadata (NEVER include api_key in persisted config)
    runtime_key: str | None = None
    if request.model:
        model_meta = {
            "provider": request.model.provider,
            "model_id": request.model.model_id,
            "base_url": request.model.base_url,
            "credential_source": request.model.credential_source.value,
        }

        # Resolve runtime API key
        if request.model.credential_source == CredentialSource.SERVER_ENV:
            env_key_name = get_provider_env_key(request.model.provider)
            runtime_key = os.environ.get(env_key_name, "") if env_key_name else ""
            if not runtime_key:
                return CreateRunResponse(
                    run_id="",
                    status=RunStatus.FAILED_REQUEST,
                    failure=FailurePayload(
                        status=RunStatus.FAILED_REQUEST,
                        sub_reason="env_key_missing",
                        message=f"Server env key not configured for provider '{request.model.provider}'.",
                        retryable=False,
                    ),
                )
        else:
            runtime_key = request.model.api_key
    else:
        model_meta = request.model_id  # legacy string

    result = service.create_run(
        ticker=request.ticker,
        trade_date=request.trade_date,
        query=request.query,
        model=model_meta,
        config=request.config,
    )

    # Register credential in memory AFTER run_id is created
    if request.model and result.get("run_id") and result.get("status") == "QUEUED" and runtime_key:
        credential_store.register(result["run_id"], api_key=runtime_key)

    if result.get("status") == "QUEUED" and result.get("run_id"):
        t = threading.Thread(target=service.run_one, args=(result["run_id"],), daemon=True)
        t.start()
    return _as_create_run_response(result, model_id=request.model_id)


@router.get("/live-runs/{run_id}", response_model=RunSummaryResponse)
def get_live_run(run_id: str, service=Depends(get_live_run_service)) -> RunSummaryResponse:
    result = service.get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return _as_run_summary_response(result)


@router.get("/live-runs/{run_id}/events", response_model=list[RunEventResponse])
def get_live_run_events(
    run_id: str,
    after_seq: int | None = Query(default=None, ge=0),
    service=Depends(get_live_run_service),
) -> list[RunEventResponse]:
    rows = service.get_events(run_id, after_seq=after_seq)
    return [_as_run_event_response(row) for row in rows]


@router.get(
    "/live-runs/{run_id}/artifacts",
    response_model=list[ArtifactResponse],
    response_model_by_alias=False,
)
def get_live_run_artifacts(
    run_id: str,
    event_seq: int | None = Query(default=None, ge=1),
    artifact_type: ArtifactType | None = Query(default=None),
    service=Depends(get_live_run_service),
) -> list[ArtifactResponse]:
    rows = service.get_artifacts(
        run_id,
        event_seq=event_seq,
        artifact_type=artifact_type.value if artifact_type else None,
    )
    return [ArtifactResponse.model_validate(row) for row in rows]


@router.post("/live-runs/{run_id}/retry", response_model=RetryRunResponse)
def retry_live_run(
    run_id: str,
    request: RetryRunRequest,
    service=Depends(get_live_run_service),
    credential_store=Depends(get_credential_store),
) -> RetryRunResponse:
    # Determine model source: prefer BYOK model config, fall back to legacy model_id
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

    # Register BYOK credential for the new run
    if request.model and result.get("ok") and result.get("run_id") and runtime_key:
        credential_store.register(result["run_id"], api_key=runtime_key)
    if result.get("ok") is True and result.get("run_id"):
        t = threading.Thread(target=service.run_one, args=(result["run_id"],), daemon=True)
        t.start()
    return _as_retry_response(result)


@router.post("/live-runs/{run_id}/cancel")
def cancel_live_run(run_id: str, service=Depends(get_live_run_service)) -> dict:
    return service.cancel_run(run_id)


@router.post("/live-runs/cancel-all")
def cancel_all_runs(service=Depends(get_live_run_service)) -> dict:
    count = service.cancel_active_runs()
    return {"ok": True, "cancelled": count}


@router.get("/live-runs/{run_id}/workspace", response_model=WorkspaceResponse)
def get_workspace(
    run_id: str,
    service=Depends(get_live_run_service),
) -> WorkspaceResponse:
    """Return a projected workspace view for the v4 workbench UI.

    Joins run summary, events, and artifacts into a structured response
    with stages, evidence, result, and diagnostics.
    """
    summary = service.get_run(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="run_not_found")

    events = service.get_events(run_id)
    artifacts = service.get_artifacts(run_id)

    projection = project_workspace(summary, events, artifacts)
    return WorkspaceResponse.model_validate(projection)

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
