from __future__ import annotations

from pydantic import ValidationError
import pytest

from catalyst_app.schemas import (
    ArtifactResponse,
    CreateRunRequest,
    CreateRunResponse,
    FailurePayload,
    RetryRunRequest,
    RetryRunResponse,
    RunEventResponse,
    RunSummaryResponse,
    RuntimeHealthResponse,
)


def test_create_run_request_parses_and_trims_query():
    payload = CreateRunRequest(
        ticker="AAPL",
        trade_date="2026-01-15",
        query="  explain move  ",
        model_id="model-default",
    )
    assert payload.query == "explain move"
    assert payload.model_id == "model-default"


def test_create_run_request_blank_query_fails():
    with pytest.raises(ValidationError):
        CreateRunRequest(ticker="AAPL", trade_date="2026-01-15", query="   ")


def test_create_run_response_with_failure_payload():
    response = CreateRunResponse(
        run_id="r1",
        status="FAILED_REQUEST",
        model_id="model-default",
        failure=FailurePayload(status="FAILED_REQUEST", sub_reason="unsupported_ticker", retryable=False),
    )
    assert response.failure is not None
    assert response.failure.sub_reason == "unsupported_ticker"


def test_run_summary_has_no_current_node_field():
    with pytest.raises(ValidationError):
        RunSummaryResponse(
            run_id="r1",
            status="RUNNING",
            current_node="miner",
        )


def test_run_summary_uses_last_completed_and_predicted_next_node():
    summary = RunSummaryResponse(
        run_id="r1",
        status="RUNNING",
        model_id="model-default",
        last_completed_node="critic",
        predicted_next_node="judge",
        retry={"parent_run_id": "r0", "retry_count": 1, "retryable": False},
    )
    assert summary.last_completed_node == "critic"
    assert summary.predicted_next_node == "judge"
    assert summary.retry is not None and summary.retry.parent_run_id == "r0"


def test_run_event_response_parses_model_id_and_statuses():
    event = RunEventResponse(
        run_id="r1",
        event_seq=1,
        node="miner",
        status_before="RUNNING",
        status_after="RUNNING",
        model_id="model-default",
    )
    assert event.model_id == "model-default"


def test_run_event_response_accepts_internal_trace_statuses():
    sufficient = RunEventResponse(
        run_id="r1",
        event_seq=2,
        node="finalizer",
        status_after="SUFFICIENT",
    )
    system_error = RunEventResponse(
        run_id="r2",
        event_seq=3,
        node="system_error_handler",
        status_after="SYSTEM_ERROR",
    )
    assert sufficient.status_after is not None and sufficient.status_after.value == "SUFFICIENT"
    assert system_error.status_after is not None and system_error.status_after.value == "SYSTEM_ERROR"


def test_invalid_status_fails_validation():
    with pytest.raises(ValidationError):
        RunSummaryResponse(run_id="r1", status="SYSTEM_ERROR")


def test_invalid_artifact_type_fails_validation():
    with pytest.raises(ValidationError):
        ArtifactResponse(
            run_id="r1",
            event_seq=1,
            node="critic",
            artifact_type="unknown_artifact",
            payload={"a": 1},
        )


def test_artifact_response_accepts_payload_json_alias():
    artifact = ArtifactResponse(
        run_id="r1",
        event_seq=1,
        node="miner",
        artifact_type="state_snapshot",
        payload_json={"state": {}},
    )
    assert artifact.payload == {"state": {}}


def test_retry_request_and_response_parse():
    request = RetryRunRequest(model_id="model-fast")
    response = RetryRunResponse(
        ok=True,
        run_id="r2",
        status="QUEUED",
        retry={"parent_run_id": "r1", "retry_count": 2, "retryable": False},
    )
    assert request.model_id == "model-fast"
    assert response.retry is not None
    assert response.retry.retry_count == 2


def test_runtime_health_supports_ready_degraded_failed():
    for status in ["ready", "degraded", "failed"]:
        health = RuntimeHealthResponse(
            status=status,
            sqlite={"status": status},
            lancedb={"status": status},
            embedding={"status": status},
            reranker={"status": status},
            default_model={"status": status, "model": "model-default"},
            errors=[],
        )
        assert health.status.value == status


def test_runtime_health_accepts_loader_active_generation_pointer():
    """RuntimeDependencyLoader emits lancedb.active_generation; the public
    health schema must accept it (real loader health payload regression)."""
    health = RuntimeHealthResponse.model_validate({
        "status": "ready",
        "sqlite": {"status": "ready", "path": "/tmp/runtime.db"},
        "lancedb": {
            "status": "ready",
            "path": "/data/lancedb_gold/b6g_8ffae891b4e1",
            "table": "chunks__staging__b3761f4b943542a8",
            "active_generation": "/data/lancedb_gold/b6g_8ffae891b4e1/active_generation.json",
        },
        "embedding": {"status": "ready", "model": "BAAI/bge-m3", "vector_dim": 1024},
        "reranker": {"status": "ready", "model": "BAAI/bge-reranker-v2-m3"},
        "default_model": {"status": "ready", "model": "deepseek-chat"},
        "retrieval": {"status": "ready"},
        "errors": [],
    })
    assert health.lancedb.active_generation.endswith("active_generation.json")
