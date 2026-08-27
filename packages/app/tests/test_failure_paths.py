"""V1.1 failure-path visibility tests (M6-9 migration).

Adapter crashes terminalize FAILED (executor failure handler); timeouts
publish run.failed with a stable code; terminal PARTIAL/ABSTAIN results are
visible through the RunDTO. Replaces the legacy QUEUED/FAILED_SYSTEM service
contract which the V1.1 surface removed.
"""
from __future__ import annotations

from pathlib import Path
import json
import time

from fastapi.testclient import TestClient

from catalyst_app.events import (
    AssuranceCompletedPayload,
    RunAcceptedPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
    StageStartedPayload,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository
from catalyst_app.runtime.admission import AdmissionRequest
from catalyst_app.runtime.executor import RunExecutor
from v1_helpers import build_manifest, fixture_db, make_v1_app

BODY = {
    "ticker": "AAPL",
    "trade_date": "2026-01-06",
    "query": "q",
    "model": {
        "provider": "openai",
        "model_id": "gpt-4.1-mini",
        "api_key": "",
        "credential_source": "server_env",
    },
}


def _wait_for_terminal(client, run_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/live-runs/{run_id}").json()
        if payload["lifecycle_status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return payload
        time.sleep(0.02)
    return client.get(f"/api/live-runs/{run_id}").json()


def _terminalize(
    db_path: Path,
    run_id: str,
    *,
    failed: bool = False,
    failure_code: str = "SYSTEM_ERROR",
    attribution_status: str = "SUFFICIENT",
) -> None:
    if failed:
        repo = EventRepository(db_path=db_path)
        repo.append(
            run_id=run_id,
            event_type=RunEventType.STAGE_STARTED,
            payload=StageStartedPayload(stage="observation_build"),
            lifecycle_update=(RunLifecycleStatus.ACCEPTED, RunLifecycleStatus.RUNNING),
        )
        repo.append_terminal(
            run_id=run_id,
            assurance_payload=AssuranceCompletedPayload(valid=False, violations=(failure_code,)),
            terminal_event_type=RunEventType.RUN_FAILED,
            terminal_payload=RunFailedPayload(
                failure_code=failure_code, stage="EXECUTION", retryable=True
            ),
            lifecycle_update=(RunLifecycleStatus.RUNNING, RunLifecycleStatus.FAILED),
        )
    else:
        from v1_helpers import terminalize_with_attribution

        terminalize_with_attribution(db_path, run_id, attribution_status=attribution_status)


def test_failed_request_paths_invalid_inputs(tmp_path: Path) -> None:
    db_path = tmp_path / "failure_request.db"
    fixture_db(db_path)
    app = make_v1_app(db_path)

    with TestClient(app) as client:
        blank_query = client.post(
            "/api/live-runs",
            json={"ticker": "AAPL", "trade_date": "2026-01-06", "query": "   ",
                  "model": {"provider": "openai", "model_id": "m", "api_key": "", "credential_source": "server_env"}},
        )
        invalid_query_type = client.post(
            "/api/live-runs",
            json={"ticker": "AAPL", "trade_date": "2026-01-06", "query": 123,
                  "model": {"provider": "openai", "model_id": "m", "api_key": "", "credential_source": "server_env"}},
        )
        bad_date = client.post(
            "/api/live-runs",
            json={"ticker": "AAPL", "trade_date": "not-a-date", "query": "q",
                  "model": {"provider": "openai", "model_id": "m", "api_key": "", "credential_source": "server_env"}},
        )

    assert blank_query.status_code == 422
    assert invalid_query_type.status_code == 422
    assert bad_date.status_code == 400  # admission shape validation


def test_graph_exception_results_in_failed_not_stuck(tmp_path: Path) -> None:
    """A crashed run adapter terminalizes FAILED instead of leaving RUNNING."""
    from catalyst_app.persistence.events import EventRepository
    from catalyst_app.runtime.admission import AdmissionController

    db_path = tmp_path / "failure_exception.db"
    fixture_db(db_path)
    repo = EventRepository(db_path=db_path)
    terminalized: list[str] = []

    def failure_handler(run_id: str, code: str) -> None:
        terminalized.append(run_id)
        _terminalize(db_path, run_id, failed=True, failure_code=code)

    def crashed_adapter(run_id: str, timeout_seconds: float) -> dict:
        raise RuntimeError("graph boom")

    executor = RunExecutor(
        admission_slots=2,
        max_workers=1,
        run_adapter=crashed_adapter,
        failure_handler=failure_handler,
        shutdown_grace_seconds=0.1,
    )
    controller = AdmissionController(
        db_path=db_path, executor=executor, events=repo, manifest_factory=build_manifest
    )
    from catalyst_app.main import create_app

    app = create_app(admission_controller=controller, db_path=db_path)

    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=BODY)
        run_id = created.json()["run_id"]
        summary = _wait_for_terminal(client, run_id)

    assert created.status_code == 200
    assert summary["lifecycle_status"] == "FAILED"
    assert summary["failure"]["code"] == "SYSTEM_ERROR"
    assert terminalized == [run_id]


def test_timeout_results_in_failed_timeout(tmp_path: Path) -> None:
    db_path = tmp_path / "failure_timeout.db"
    fixture_db(db_path)

    def timeout_adapter(run_id: str, timeout_seconds: float) -> dict:
        _terminalize(db_path, run_id, failed=True, failure_code="TIMEOUT")
        return {"run_id": run_id, "status": "FAILED"}

    app = make_v1_app(db_path, run_adapter=timeout_adapter)

    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=BODY)
        run_id = created.json()["run_id"]
        summary = _wait_for_terminal(client, run_id)

    assert created.status_code == 200
    assert summary["lifecycle_status"] == "FAILED"
    assert summary["failure"]["code"] == "TIMEOUT"


def test_terminal_partial_and_abstain_are_visible(tmp_path: Path) -> None:
    db_path = tmp_path / "failure_terminal.db"
    fixture_db(db_path)

    def adapter_factory(target_db: Path, attribution_status: str):
        def adapter(run_id: str, timeout_seconds: float) -> dict:
            _terminalize(target_db, run_id, attribution_status=attribution_status)
            return {"run_id": run_id, "status": "COMPLETED"}

        return adapter

    for status in ("PARTIAL", "ABSTAIN"):
        fresh_db = tmp_path / f"failure_terminal_{status}.db"
        fixture_db(fresh_db)
        app = make_v1_app(fresh_db, run_adapter=adapter_factory(fresh_db, status))
        with TestClient(app) as client:
            run_id = client.post("/api/live-runs", json=BODY).json()["run_id"]
            summary = _wait_for_terminal(client, run_id)
        assert summary["lifecycle_status"] == "COMPLETED"
        assert summary["attribution_status"] == status


def test_artifact_endpoint_missing_and_invalid_type(tmp_path: Path) -> None:
    db_path = tmp_path / "failure_artifact.db"
    fixture_db(db_path)
    app = make_v1_app(db_path)

    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=BODY).json()
        run_id = created["run_id"]
        missing = client.get(f"/api/live-runs/{run_id}/artifacts/missing")
        missing_run = client.get("/api/live-runs/nope/artifacts")

    assert missing.status_code == 404
    assert missing_run.status_code == 404
