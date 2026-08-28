"""V1.1 live-runtime smoke test (M6-9 migration).

A real admission -> executor task -> durable terminal commit -> RunDTO/SSE
read path against one SQLite runtime DB, using a fake run adapter that
persists V1.1 events/artifacts. Replaces the legacy daemon/QUEUED smoke path.
"""
from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient

from catalyst_app.persistence.events import EventRepository
from catalyst_app.runtime.admission import AdmissionController
from catalyst_app.runtime.executor import RunExecutor
from v1_helpers import build_manifest, fixture_db

BODY = {
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


def _wait_for_terminal(client: TestClient, run_id: str, timeout_seconds: float = 5.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = client.get(f"/api/live-runs/{run_id}").json()
        if payload["lifecycle_status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return payload
        time.sleep(0.02)
    return client.get(f"/api/live-runs/{run_id}").json()


def _run_adapter(db_path: Path):
    from v1_helpers import terminalize_with_attribution

    def adapter(run_id: str, timeout_seconds: float) -> dict:
        terminalize_with_attribution(db_path, run_id, attribution_status="SUFFICIENT")
        return {"run_id": run_id, "status": "COMPLETED"}

    return adapter


def test_live_runtime_smoke_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime_smoke.db"
    fixture_db(db_path)
    repo = EventRepository(db_path=db_path)
    executor = RunExecutor(
        admission_slots=4,
        max_workers=1,
        run_adapter=_run_adapter(db_path),
        shutdown_grace_seconds=0.1,
    )
    controller = AdmissionController(
        db_path=db_path, executor=executor, events=repo, manifest_factory=build_manifest
    )

    from catalyst_app.main import create_app

    app = create_app(admission_controller=controller, db_path=db_path)
    client = TestClient(app)

    with client:
        create_resp = client.post("/api/live-runs", json=BODY)
        assert create_resp.status_code == 200
        run_id = create_resp.json()["run_id"]
        assert create_resp.json()["status"] == "ACCEPTED"

        summary = _wait_for_terminal(client, run_id)
        assert summary["lifecycle_status"] == "COMPLETED"
        assert summary["attribution_status"] == "SUFFICIENT"

        # V1.1 artifacts page reflects the committed run.
        artifacts = client.get(f"/api/live-runs/{run_id}/artifacts")
        assert artifacts.status_code == 200

        # SSE replay of the terminal run returns all frames and closes.
        stream = client.get(f"/api/live-runs/{run_id}/stream")
        assert stream.status_code == 200
        assert "id: " + run_id + ":1" in stream.text
        assert "event: run.completed" in stream.text
