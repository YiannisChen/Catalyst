from __future__ import annotations

import sqlite3
import time

from catalyst_agents.runtime.runner import LiveRunRunner
from catalyst_agents.runtime.service import LiveRunService
from catalyst_agents.trace.artifacts import write_node_artifact
from catalyst_agents.trace.writer import TraceWriter


def _db_path(tmp_path):
    path = tmp_path / "runtime.db"
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
            volume INTEGER,
            source TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("AAPL", "2026-01-15", 1.0, 2.0, 0.5, 1.5, 1000, "polygon"),
    )
    conn.commit()
    conn.close()
    return path


class RecordingGraph:
    def __init__(self, db_path):
        self.db_path = db_path
        self.calls = []

    def invoke(self, state, run_id=None):
        self.calls.append({"state": state, "run_id": run_id})
        with TraceWriter(
            db_path=self.db_path,
            run_id=run_id,
            ticker=state["ticker"],
            trade_date=state["trade_date"],
            config=state.get("config", "mcj_full"),
        ) as writer:
            event_seq = writer.event(
                node="miner",
                started_at="2026-01-15T00:00:00Z",
                ended_at="2026-01-15T00:00:01Z",
                latency_ms=1000,
                model_id=state.get("model_id"),
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.01,
                decision=None,
                error_type=None,
                error_message=None,
                status_before=None,
                status_after=None,
            )
            write_node_artifact(
                writer.conn,
                run_id=writer.run_id,
                event_seq=event_seq,
                node="miner",
                artifact_type="state_snapshot",
                payload={"state": {"phase": "miner"}},
            )
            writer.complete({"output_status": "SUFFICIENT", "total_cost_usd": 0.01})
        return {"output_status": "SUFFICIENT", "summary_md": "done"}


class SleepingGraph:
    def __init__(self):
        self.calls = []

    def invoke(self, state, run_id=None):
        self.calls.append(run_id)
        time.sleep(0.2)
        return {"output_status": "SUFFICIENT"}


class LateSuccessGraph:
    def __init__(self, db_path):
        self.db_path = db_path
        self.calls = []

    def invoke(self, state, run_id=None):
        self.calls.append(run_id)
        time.sleep(0.15)
        with TraceWriter(db_path=self.db_path, run_id=run_id, ticker=state["ticker"], trade_date=state["trade_date"]) as writer:
            writer.complete({"output_status": "SUFFICIENT", "total_cost_usd": 0.0})
        return {"output_status": "SUFFICIENT"}


class BoomGraph:
    def __init__(self):
        self.calls = []

    def invoke(self, state, run_id=None):
        self.calls.append(run_id)
        raise RuntimeError("boom")


def test_create_run_persists_queued_without_executing_graph(tmp_path):
    db_path = _db_path(tmp_path)
    graph = RecordingGraph(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: graph)

    created = service.create_run(ticker="AAPL", trade_date="2026-01-15", query=None)

    assert created["status"] == "QUEUED"
    assert graph.calls == []
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status, queued_at, started_at, ticker, trade_date FROM agent_runs WHERE run_id = ?", (created["run_id"],)).fetchone()
    conn.close()
    assert row[0] == "QUEUED"
    assert row[1] is not None
    assert row[2] is None
    assert row[3:] == ("AAPL", "2026-01-15")


def test_validation_failure_creates_failed_request_without_graph_execution(tmp_path):
    db_path = _db_path(tmp_path)
    graph = RecordingGraph(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: graph)

    created = service.create_run(ticker="TSLA", trade_date="2026-01-15", query=None)

    assert created["status"] == "FAILED_REQUEST"
    assert created["failure"]["sub_reason"] == "unsupported_ticker"
    assert graph.calls == []
    run = service.get_run(created["run_id"])
    assert run["status"] == "FAILED_REQUEST"
    assert run["failure"]["sub_reason"] == "unsupported_ticker"


def test_runner_executes_fake_graph_with_precreated_run_id(tmp_path):
    db_path = _db_path(tmp_path)
    graph = RecordingGraph(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: graph)
    created = service.create_run(ticker="AAPL", trade_date="2026-01-15", query="explain")

    result = service.run_next()

    assert result["run_id"] == created["run_id"]
    assert graph.calls[0]["run_id"] == created["run_id"]
    assert graph.calls[0]["state"]["ticker"] == "AAPL"
    run = service.get_run(created["run_id"])
    assert run["status"] == "SUCCEEDED"
    assert run["last_completed_node"] == "miner"
    assurance = service.get_assurance(created["run_id"])
    assert assurance is not None
    assert assurance.run_id == created["run_id"]


def test_event_and_artifact_polling_after_fake_graph_completion(tmp_path):
    db_path = _db_path(tmp_path)
    graph = RecordingGraph(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: graph)
    created = service.create_run(ticker="AAPL", trade_date="2026-01-15", query=None)
    service.run_next()

    all_events = service.get_events(created["run_id"])
    later_events = service.get_events(created["run_id"], after_seq=1)
    artifacts = service.get_artifacts(created["run_id"], event_seq=1, artifact_type="state_snapshot")

    assert [event["node"] for event in all_events] == ["miner"]
    assert later_events == []
    assert artifacts[0]["artifact_type"] == "state_snapshot"
    assert artifacts[0]["payload_json"] == {"state": {"phase": "miner"}}


def test_retry_rejects_running_and_accepts_terminal_with_lineage(tmp_path):
    db_path = _db_path(tmp_path)
    graph = RecordingGraph(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: graph)
    queued = service.create_run(ticker="AAPL", trade_date="2026-01-15", query=None)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE agent_runs SET status = 'RUNNING' WHERE run_id = ?", (queued["run_id"],))
    conn.commit()
    conn.close()

    rejected = service.retry_run(queued["run_id"])
    assert rejected["ok"] is False
    assert rejected["failure"]["sub_reason"] == "run_not_terminal"

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE agent_runs SET status = 'INSUFFICIENT' WHERE run_id = ?", (queued["run_id"],))
    conn.commit()
    conn.close()

    accepted = service.retry_run(queued["run_id"], model="model-b")
    assert accepted["ok"] is True
    assert accepted["run_id"] != queued["run_id"]
    conn = sqlite3.connect(db_path)
    link = conn.execute("SELECT parent_run_id, link_type FROM run_links WHERE run_id = ?", (accepted["run_id"],)).fetchone()
    config = conn.execute("SELECT config FROM agent_runs WHERE run_id = ?", (accepted["run_id"],)).fetchone()[0]
    conn.close()
    assert link == (queued["run_id"], "retry")
    assert "model-b" in config


def test_timeout_marks_run_failed_system_timeout(tmp_path):
    db_path = _db_path(tmp_path)
    graph = SleepingGraph()
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: graph, timeout_seconds=0.01)
    created = service.create_run(ticker="AAPL", trade_date="2026-01-15", query=None)

    result = service.run_next()
    run = service.get_run(created["run_id"])

    assert result["status"] == "FAILED_SYSTEM"
    assert run["status"] == "FAILED_SYSTEM"
    assert run["failure"]["sub_reason"] == "timeout"


def test_timeout_late_success_does_not_override_terminal_timeout(tmp_path):
    db_path = _db_path(tmp_path)
    graph = LateSuccessGraph(db_path)
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: graph, timeout_seconds=0.01)
    created = service.create_run(ticker="AAPL", trade_date="2026-01-15", query=None)

    service.run_next()
    time.sleep(0.25)
    run = service.get_run(created["run_id"])

    assert run["status"] == "FAILED_SYSTEM"
    assert run["failure"]["sub_reason"] == "timeout"


def test_graph_exception_marks_failed_system_and_does_not_leave_queued(tmp_path):
    db_path = _db_path(tmp_path)
    graph = BoomGraph()
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: graph)
    created = service.create_run(ticker="AAPL", trade_date="2026-01-15", query=None)

    result = service.run_next()
    run = service.get_run(created["run_id"])

    assert result["status"] == "FAILED_SYSTEM"
    assert run["status"] == "FAILED_SYSTEM"
    assert "boom" in (run["failure"]["message"] or "")
