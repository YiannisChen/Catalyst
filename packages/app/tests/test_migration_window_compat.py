"""Migration-window compatibility contract (M6-12).

New writes always use V1.1 events/artifacts + lifecycle fields; legacy /events
and /workspace translate old saved artifacts only (no new legacy writes) and
return a Deprecation header for the M8 caller audit. The default frontend uses
SSE + versioned DTOs; polling hooks are not the default live path.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from catalyst_app.runtime.admission import AdmissionController
from catalyst_app.runtime.executor import RunExecutor
from catalyst_app.persistence.events import EventRepository
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


def _v1_app(db_path: Path) -> TestClient:
    repo = EventRepository(db_path=db_path)
    executor = RunExecutor(
        admission_slots=4, max_workers=1,
        run_adapter=lambda run_id, t: {"ok": True},
        shutdown_grace_seconds=0.1,
    )
    controller = AdmissionController(
        db_path=db_path, executor=executor, events=repo, manifest_factory=build_manifest
    )
    return TestClient(create_app(admission_controller=controller, db_path=db_path))


def test_new_writes_use_v11_lifecycle_and_events(tmp_path: Path) -> None:
    import sqlite3

    from catalyst_agents.trace.schema import init_trace_db

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    # Legacy tables exist (baseline readers) but new writes must never touch them.
    conn = sqlite3.connect(db_path)
    init_trace_db(conn)
    conn.close()
    client = _v1_app(db_path)

    with client:
        created = client.post("/api/live-runs", json=BODY)
        run_id = created.json()["run_id"]

    assert created.status_code == 200
    assert created.json()["status"] == "ACCEPTED"

    with open_rw(db_path) as conn:
        run = conn.execute(
            "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        event = conn.execute(
            "SELECT event_type FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()
        legacy_count = conn.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]

    assert run["lifecycle_status"] == "ACCEPTED"
    assert event["event_type"] == "run.accepted"
    assert legacy_count == 0  # no new legacy write


def test_legacy_events_compat_translates_old_saved_artifacts_only(tmp_path: Path) -> None:
    from catalyst_app.dependencies import get_live_run_service

    class LegacyService:
        def get_run(self, run_id):
            return None

        def get_events(self, run_id, *, after_seq=None):
            return [
                {
                    "run_id": run_id,
                    "trace_id": "t1",
                    "event_seq": 1,
                    "node": "miner",
                    "status_before": "RUNNING",
                    "status_after": "SUFFICIENT",
                    "model_id": "model-default",
                }
            ]

        def get_artifacts(self, run_id, *, event_seq=None, artifact_type=None):
            return []

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    app = create_app(db_path=db_path)
    app.dependency_overrides[get_live_run_service] = lambda: LegacyService()

    with TestClient(app) as client:
        events = client.get("/api/live-runs/legacy:1/events")

    assert events.status_code == 200
    assert events.headers.get("deprecation") == "true"
    assert events.json()[0]["status_after"] == "SUFFICIENT"


def test_workspace_compat_returns_deprecation_header(tmp_path: Path) -> None:
    from catalyst_app.dependencies import get_live_run_service

    class LegacyService:
        def get_run(self, run_id):
            return {
                "run_id": run_id,
                "status": "SUCCEEDED",
                "ticker": "AAPL",
                "trade_date": "2026-01-06",
            }

        def get_events(self, run_id, *, after_seq=None):
            return []

        def get_artifacts(self, run_id, *, event_seq=None, artifact_type=None):
            return []

    db_path = tmp_path / "runtime.db"
    fixture_db(db_path)
    app = create_app(db_path=db_path)
    app.dependency_overrides[get_live_run_service] = lambda: LegacyService()

    with TestClient(app) as client:
        workspace = client.get("/api/live-runs/legacy:1/workspace")

    assert workspace.status_code == 200
    assert workspace.headers.get("deprecation") == "true"


def test_app_default_is_sse_liveworkbench_not_demo(tmp_path: Path) -> None:
    from pathlib import Path as P

    app_source = P("apps/workbench/src/App.tsx").read_text(encoding="utf-8")
    assert "LiveWorkbench" in app_source
    assert "VITE_ENABLE_DEMO" in app_source
    # DemoWorkbench must be gated; the default path is the SSE live workbench.
    assert app_source.index("VITE_ENABLE_DEMO") < app_source.index("DemoWorkbench") or (
        "DemoWorkbench" not in app_source
    )
    # The live workbench never imports the polling hook as its default path.
    live_source = P("apps/workbench/src/components/workbench/LiveWorkbench.tsx").read_text(
        encoding="utf-8"
    )
    assert "useLiveRunPolling" not in live_source
