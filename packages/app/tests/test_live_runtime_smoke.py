from __future__ import annotations

import sqlite3
import time

from fastapi.testclient import TestClient

from catalyst_agents.runtime.service import LiveRunService
from catalyst_agents.trace.artifacts import write_node_artifact
from catalyst_agents.trace.writer import TraceWriter
from catalyst_app.main import create_app
from catalyst_app.workbench_store import WorkbenchStore


class FakeLoader:
    def health(self):
        return {
            "status": "ready",
            "sqlite": {"status": "ready"},
            "lancedb": {"status": "ready"},
            "embedding": {"status": "ready"},
            "reranker": {"status": "ready"},
            "default_model": {"status": "ready", "model": "model-default"},
            "errors": [],
        }


class FastGraph:
    def __init__(self, db_path):
        self.db_path = db_path

    def invoke(self, state, run_id=None):
        with TraceWriter(
            db_path=self.db_path,
            run_id=run_id,
            ticker=state.get("ticker"),
            trade_date=state.get("trade_date"),
            config="mcj_full",
        ) as writer:
            event_seq = writer.event(
                node="critic",
                started_at="2026-01-15T00:00:00Z",
                ended_at="2026-01-15T00:00:01Z",
                latency_ms=100,
                model_id=state.get("model_id"),
                input_tokens=10,
                output_tokens=20,
                cost_usd=0.01,
                decision=None,
                error_type=None,
                error_message=None,
                status_before="RUNNING",
                status_after="SUFFICIENT",
            )
            write_node_artifact(
                writer.conn,
                run_id=writer.run_id,
                event_seq=event_seq,
                node="critic",
                artifact_type="raw_llm_response",
                payload={"text": "mock raw response"},
            )
            writer.complete({"output_status": "SUFFICIENT", "summary_md": "done", "total_cost_usd": 0.01})
        return {"output_status": "SUFFICIENT", "summary_md": "done"}


def _prepare_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ohlcv (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            source TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("AAPL", "2026-01-15", 10.0, 11.0, 9.0, 10.5, 1000.0, "polygon"),
    )
    conn.commit()
    conn.close()


def _wait_for_terminal(client: TestClient, run_id: str, timeout_seconds: float = 1.0):
    deadline = time.monotonic() + timeout_seconds
    response = client.get(f"/api/live-runs/{run_id}")
    while response.json()["status"] in {"QUEUED", "RUNNING"} and time.monotonic() < deadline:
        time.sleep(0.01)
        response = client.get(f"/api/live-runs/{run_id}")
    return response


def test_live_runtime_smoke_end_to_end(tmp_path):
    db_path = tmp_path / "runtime_smoke.db"
    _prepare_db(db_path)

    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: FastGraph(db_path))
    app = create_app(
        service_override=service,
        dependency_loader_override=FakeLoader(),
        workbench_store_override=WorkbenchStore(db_path=db_path),
    )
    client = TestClient(app)

    create_resp = client.post(
        "/api/live-runs",
        json={
            "ticker": "AAPL",
            "trade_date": "2026-01-15",
            "query": "explain",
            "model_id": "model-default",
            "config": "mcj_full",
        },
    )
    assert create_resp.status_code == 200
    run_id = create_resp.json()["run_id"]
    assert create_resp.json()["status"] == "QUEUED"

    run_resp = _wait_for_terminal(client, run_id)
    assert run_resp.status_code == 200
    run_payload = run_resp.json()
    assert run_payload["status"] != "QUEUED"

    events_resp = client.get(f"/api/live-runs/{run_id}/events")
    assert events_resp.status_code == 200
    events_payload = events_resp.json()
    assert len(events_payload) >= 1

    artifacts_resp = client.get(
        f"/api/live-runs/{run_id}/artifacts",
        params={"artifact_type": "raw_llm_response"},
    )
    assert artifacts_resp.status_code == 200
    artifacts_payload = artifacts_resp.json()
    assert len(artifacts_payload) >= 1
    assert artifacts_payload[0]["artifact_type"] == "raw_llm_response"
    assert "payload" in artifacts_payload[0]

    retry_resp = client.post(f"/api/live-runs/{run_id}/retry", json={"model_id": "model-fast"})
    assert retry_resp.status_code == 200
    retry_payload = retry_resp.json()
    assert retry_payload["ok"] is True
    assert retry_payload["run_id"] != run_id
    retry_run_resp = _wait_for_terminal(client, retry_payload["run_id"])
    assert retry_run_resp.status_code == 200
    assert retry_run_resp.json()["status"] != "QUEUED"

    conn = sqlite3.connect(db_path)
    link_row = conn.execute(
        "SELECT parent_run_id, link_type FROM run_links WHERE run_id = ?",
        (retry_payload["run_id"],),
    ).fetchone()
    conn.close()
    assert link_row == (run_id, "retry")
