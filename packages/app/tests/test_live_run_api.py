"""V1.1 live-run API surface tests (M6-9).

POST /api/live-runs (durable ACCEPTED admission + Idempotency-Key), GET run
(RunDTO lifecycle + attribution), V1.1 paged artifacts, migration-window
/events and /retry compatibility translations, and /health/runtime.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.schema import init_runtime_db
from v1_helpers import fixture_db, make_v1_app

LEGACY_BODY = {
    "ticker": "AAPL",
    "trade_date": "2026-01-06",
    "query": "explain",
    "model": {
        "provider": "openai",
        "model_id": "gpt-4.1-mini",
        "api_key": "",
        "credential_source": "server_env",
    },
}


def test_post_live_runs_accepts_and_returns_accepted(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    app = make_v1_app(db_path)

    with TestClient(app) as client:
        response = client.post("/api/live-runs", json=LEGACY_BODY)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ACCEPTED"
    assert payload["run_id"]
    assert payload["stream_url"] == f"/api/live-runs/{payload['run_id']}/stream"


def test_post_live_runs_validation_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    app = make_v1_app(db_path)

    with TestClient(app) as client:
        blank = client.post(
            "/api/live-runs",
            json={"ticker": "AAPL", "trade_date": "2026-01-06", "query": "   ",
                  "model": {"provider": "openai", "model_id": "m", "api_key": "", "credential_source": "server_env"}},
        )
        missing_model = client.post(
            "/api/live-runs",
            json={"ticker": "AAPL", "trade_date": "2026-01-06", "query": "q"},
        )

    assert blank.status_code == 422  # pydantic blank-query validation
    assert missing_model.status_code == 422  # model or model_id required


def test_get_live_run_dto_and_not_found(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    app = make_v1_app(db_path)

    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=LEGACY_BODY).json()
        run_id = created["run_id"]
        ok = client.get(f"/api/live-runs/{run_id}")
        missing = client.get("/api/live-runs/unknown")

    assert ok.status_code == 200
    payload = ok.json()
    assert payload["run_id"] == run_id
    assert payload["lifecycle_status"] in {"ACCEPTED", "RUNNING", "CANCEL_REQUESTED", "COMPLETED", "FAILED", "CANCELLED"}
    assert "last_completed_node" not in payload
    assert "predicted_next_node" not in payload
    assert missing.status_code == 404


def test_get_live_run_events_compat_translation(tmp_path: Path) -> None:
    """Legacy /events stays a migration-window translation in packages/app."""
    from catalyst_app.runtime.admission import AdmissionController, AdmissionRequest
    from catalyst_app.runtime.executor import RunExecutor
    from catalyst_app.persistence.events import EventRepository
    from catalyst_agents.runtime.service import LiveRunService
    from v1_helpers import build_manifest

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    repo = EventRepository(db_path=db_path)
    executor = RunExecutor(
        admission_slots=4, max_workers=1,
        run_adapter=lambda run_id, t: {"ok": True},
        shutdown_grace_seconds=0.1,
    )
    controller = AdmissionController(
        db_path=db_path, executor=executor, events=repo, manifest_factory=build_manifest
    )
    app = create_app(admission_controller=controller, db_path=db_path)

    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=LEGACY_BODY).json()
        run_id = created["run_id"]
        # The V1.1 run has no legacy trace rows, so /events translates to empty.
        events = client.get(f"/api/live-runs/{run_id}/events")
    assert events.status_code == 200
    assert events.json() == []


def test_get_live_run_artifacts_v11_paged(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:arts', 'COMPLETED', NULL, 'a'*64, 'm', 'b'*64, 0, 't', 't')"
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES ('run:arts', 1, 't', 'run.completed', 'TERMINAL', '{}', 'v1')"
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)"
            " VALUES ('artifact:1', 'run:arts', 1, 'state_snapshot', ?, ?, 0)",
            ("a" * 64, json.dumps({"state": {"phase": "miner"}})),
        )
        conn.commit()
    app = make_v1_app(db_path)

    with TestClient(app) as client:
        response = client.get("/api/live-runs/run:arts/artifacts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["artifact_type"] == "state_snapshot"
    assert payload["items"][0]["ref"]["run_id"] == "run:arts"
    assert "payload" not in payload["items"][0]  # paged refs omit payload


def test_retry_live_run_compat(tmp_path: Path) -> None:
    """Legacy /retry remains a compatibility translation (no new daemon)."""
    from catalyst_app.dependencies import get_live_run_service

    class LegacyService:
        def retry_run(self, run_id, *, model=None):
            if run_id == "missing":
                return {"ok": False, "failure": {"sub_reason": "run_not_found", "message": "Run not found.", "retryable": False}}
            return {"ok": True, "run_id": "run-retry", "status": "QUEUED"}

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    app = make_v1_app(db_path)
    app.dependency_overrides[get_live_run_service] = lambda: LegacyService()

    with TestClient(app) as client:
        ok = client.post("/api/live-runs/run-1/retry", json={"model_id": "model-fast"})
        missing = client.post("/api/live-runs/missing/retry", json={"model_id": "model-fast"})

    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert missing.status_code == 200
    assert missing.json()["ok"] is False


def test_runtime_health_degraded_returns_200(tmp_path: Path) -> None:
    from catalyst_app.dependencies import get_runtime_dependency_loader

    class FakeLoader:
        def health(self):
            return {
                "status": "degraded",
                "sqlite": {"status": "ready"},
                "lancedb": {"status": "ready"},
                "embedding": {"status": "ready"},
                "reranker": {"status": "degraded"},
                "default_model": {"status": "ready", "model": "model-default"},
                "errors": [],
            }

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    app = make_v1_app(db_path)
    app.dependency_overrides[get_runtime_dependency_loader] = lambda: FakeLoader()

    with TestClient(app) as client:
        response = client.get("/api/health/runtime")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["reranker"]["status"] == "degraded"
