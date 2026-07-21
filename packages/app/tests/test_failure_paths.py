from __future__ import annotations

import sqlite3
import time

from fastapi.testclient import TestClient

from catalyst_agents.runtime.service import LiveRunService
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


class ExceptionGraph:
    def invoke(self, state, run_id=None):
        raise RuntimeError("graph boom")


class TimeoutGraph:
    def invoke(self, state, run_id=None):
        time.sleep(0.05)
        return {"output_status": "SUFFICIENT"}


class StatusGraph:
    def __init__(self, db_path, output_status: str):
        self.db_path = db_path
        self.output_status = output_status

    def invoke(self, state, run_id=None):
        with TraceWriter(
            db_path=self.db_path,
            run_id=run_id,
            ticker=state.get("ticker"),
            trade_date=state.get("trade_date"),
            config="mcj_full",
        ) as writer:
            writer.complete({"output_status": self.output_status, "summary_md": self.output_status.lower()})
        return {"output_status": self.output_status, "summary_md": self.output_status.lower()}


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


def _make_client(service, db_path):
    app = create_app(
        service_override=service,
        dependency_loader_override=FakeLoader(),
        workbench_store_override=WorkbenchStore(db_path=db_path),
    )
    return TestClient(app)


def _wait_for_terminal(client, run_id, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/live-runs/{run_id}").json()
        if payload["status"] not in {"QUEUED", "RUNNING"}:
            return payload
        time.sleep(0.01)
    return client.get(f"/api/live-runs/{run_id}").json()


def test_failed_request_paths_invalid_inputs(tmp_path):
    db_path = tmp_path / "failure_request.db"
    _prepare_db(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: StatusGraph(db_path, "SUFFICIENT"))
    client = _make_client(service, db_path)

    unsupported_ticker = client.post(
        "/api/live-runs",
        json={"ticker": "TSLA", "trade_date": "2026-01-15", "query": "q", "model_id": "model-default"},
    )
    non_trading_date = client.post(
        "/api/live-runs",
        json={"ticker": "AAPL", "trade_date": "2026-01-20", "query": "q", "model_id": "model-default"},
    )
    blank_query = client.post(
        "/api/live-runs",
        json={"ticker": "AAPL", "trade_date": "2026-01-15", "query": "   ", "model_id": "model-default"},
    )
    invalid_query_type = client.post(
        "/api/live-runs",
        json={"ticker": "AAPL", "trade_date": "2026-01-15", "query": 123, "model_id": "model-default"},
    )

    assert unsupported_ticker.status_code == 200
    assert unsupported_ticker.json()["status"] == "FAILED_REQUEST"
    assert non_trading_date.status_code == 200
    assert non_trading_date.json()["status"] == "FAILED_REQUEST"
    assert blank_query.status_code == 422
    assert invalid_query_type.status_code == 422


def test_graph_exception_results_in_failed_system_not_stuck(tmp_path):
    db_path = tmp_path / "failure_exception.db"
    _prepare_db(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: ExceptionGraph())
    client = _make_client(service, db_path)

    created = client.post(
        "/api/live-runs",
        json={"ticker": "AAPL", "trade_date": "2026-01-15", "query": "q", "model_id": "model-default"},
    )
    run_id = created.json()["run_id"]
    summary = _wait_for_terminal(client, run_id)

    assert created.status_code == 200
    assert summary["status"] == "FAILED_SYSTEM"


def test_timeout_results_in_failed_system_timeout(tmp_path):
    db_path = tmp_path / "failure_timeout.db"
    _prepare_db(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: TimeoutGraph(), timeout_seconds=0.001)
    client = _make_client(service, db_path)

    created = client.post(
        "/api/live-runs",
        json={"ticker": "AAPL", "trade_date": "2026-01-15", "query": "q", "model_id": "model-default"},
    )
    run_id = created.json()["run_id"]
    summary = _wait_for_terminal(client, run_id)

    assert created.status_code == 200
    assert summary["status"] == "FAILED_SYSTEM"
    assert summary["failure"]["sub_reason"] == "timeout"


def test_terminal_insufficient_and_partial_are_visible(tmp_path):
    db_path = tmp_path / "failure_terminal.db"
    _prepare_db(db_path)

    insufficient_service = LiveRunService(db_path=db_path, graph_factory=lambda **_: StatusGraph(db_path, "INSUFFICIENT"))
    insufficient_client = _make_client(insufficient_service, db_path)
    insufficient_run = insufficient_client.post(
        "/api/live-runs",
        json={"ticker": "AAPL", "trade_date": "2026-01-15", "query": "q", "model_id": "model-default"},
    ).json()["run_id"]
    insufficient_summary = _wait_for_terminal(insufficient_client, insufficient_run)

    partial_service = LiveRunService(db_path=db_path, graph_factory=lambda **_: StatusGraph(db_path, "PARTIAL"))
    partial_client = _make_client(partial_service, db_path)
    partial_run = partial_client.post(
        "/api/live-runs",
        json={"ticker": "AAPL", "trade_date": "2026-01-15", "query": "q", "model_id": "model-default"},
    ).json()["run_id"]
    partial_summary = _wait_for_terminal(partial_client, partial_run)

    assert insufficient_summary["status"] == "INSUFFICIENT"
    assert partial_summary["status"] == "PARTIAL"


def test_artifact_endpoint_empty_missing_and_invalid_type(tmp_path):
    db_path = tmp_path / "failure_artifacts.db"
    _prepare_db(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: StatusGraph(db_path, "SUFFICIENT"))
    client = _make_client(service, db_path)

    missing_run_artifacts = client.get("/api/live-runs/missing-run/artifacts")
    invalid_type = client.get("/api/live-runs/missing-run/artifacts", params={"artifact_type": "invalid"})

    assert missing_run_artifacts.status_code == 200
    assert missing_run_artifacts.json() == []
    assert invalid_type.status_code == 422


def test_retry_semantics_and_lineage(tmp_path):
    db_path = tmp_path / "failure_retry.db"
    _prepare_db(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: StatusGraph(db_path, "SUFFICIENT"))
    client = _make_client(service, db_path)

    queued_run = service.create_run(
        ticker="AAPL",
        trade_date="2026-01-15",
        query="q",
        model="model-default",
    )["run_id"]

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE agent_runs SET status = 'RUNNING' WHERE run_id = ?", (queued_run,))
    conn.commit()
    conn.close()

    running_retry = client.post(f"/api/live-runs/{queued_run}/retry", json={"model_id": "model-fast"})
    assert running_retry.status_code == 200
    assert running_retry.json()["ok"] is False

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE agent_runs SET status = 'FAILED_SYSTEM' WHERE run_id = ?", (queued_run,))
    conn.commit()
    conn.close()

    terminal_retry = client.post(f"/api/live-runs/{queued_run}/retry", json={"model_id": "model-fast"})
    assert terminal_retry.status_code == 200
    assert terminal_retry.json()["ok"] is True
    retry_run_id = terminal_retry.json()["run_id"]

    retry_summary = _wait_for_terminal(client, retry_run_id)
    assert retry_summary["status"] != "QUEUED"

    conn = sqlite3.connect(db_path)
    link_row = conn.execute(
        "SELECT parent_run_id, link_type FROM run_links WHERE run_id = ?",
        (retry_run_id,),
    ).fetchone()
    conn.close()
    assert link_row == (queued_run, "retry")
