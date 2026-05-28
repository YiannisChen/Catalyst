from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from catalyst_app.dependencies import get_live_run_service, get_runtime_dependency_loader
from catalyst_app.llm_factory import SUPPORTED_MODELS, DEFAULT_MODEL
from catalyst_app.schemas import (
    ArtifactResponse,
    ArtifactType,
    CreateRunRequest,
    CreateRunResponse,
    ModelOption,
    ModelsResponse,
    RetryRunRequest,
    RetryRunResponse,
    RunEventResponse,
    RunSummaryResponse,
    RuntimeHealthResponse,
)


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
) -> CreateRunResponse:
    result = service.create_run(
        ticker=request.ticker,
        trade_date=request.trade_date,
        query=request.query,
        model=request.model_id,
        config=request.config,
    )
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
) -> RetryRunResponse:
    result = service.retry_run(run_id, model=request.model_id)
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


@router.get("/health/runtime", response_model=RuntimeHealthResponse)
def runtime_health(loader=Depends(get_runtime_dependency_loader)) -> RuntimeHealthResponse:
    return RuntimeHealthResponse.model_validate(loader.health())


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
