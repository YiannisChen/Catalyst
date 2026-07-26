"""Tests for local SQLite trace persistence."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import pytest

from attribution_fixtures import FixtureRetriever, RecordingCutoffPolicy, mock_provider_with_ohlcv
from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.trace.artifacts import read_node_artifacts, write_node_artifact
from catalyst_agents.trace.exporter import export_run
from catalyst_agents.trace.projection import project_node_artifacts
from catalyst_agents.trace.schema import init_trace_db
from catalyst_agents.trace.writer import TraceWriter


def test_init_trace_db_creates_tables(tmp_path: Path):
    db_path = tmp_path / "trace.db"
    conn = sqlite3.connect(db_path)

    init_trace_db(conn)

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()

    assert "agent_runs" in tables
    assert "trace_events" in tables
    assert "node_artifacts" in tables
    assert "run_links" in tables


def test_init_trace_db_adds_queued_at_column(tmp_path: Path):
    db_path = tmp_path / "trace.db"
    conn = sqlite3.connect(db_path)

    init_trace_db(conn)

    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
    }
    conn.close()

    assert "queued_at" in columns


def test_trace_writer_persists_run_and_event(tmp_path: Path):
    db_path = tmp_path / "trace.db"

    with TraceWriter(db_path=db_path, ticker="AAPL", trade_date="2026-01-15") as writer:
        event_seq = writer.event(
            node="critic",
            started_at="2026-01-15T00:00:00Z",
            ended_at="2026-01-15T00:00:01Z",
            latency_ms=1000,
            model_id="claude-sonnet-4-20250514",
            input_tokens=100,
            output_tokens=20,
            cost_usd=0.01,
            decision='{"next_action":"proceed"}',
            error_type=None,
            error_message=None,
            status_before=None,
            status_after="PARTIAL",
        )
        writer.complete({"output_status": "PARTIAL", "total_cost_usd": 0.01})
    assert event_seq == 1

    conn = sqlite3.connect(db_path)
    run_row = conn.execute(
        "SELECT trace_id, ticker, trade_date, status FROM agent_runs WHERE run_id = ?",
        (writer.run_id,),
    ).fetchone()
    event_row = conn.execute(
        "SELECT event_seq, node, error_type, status_after FROM trace_events WHERE run_id = ?",
        (writer.run_id,),
    ).fetchone()
    conn.close()

    assert run_row is not None
    assert run_row[1] == "AAPL"
    assert run_row[2] == "2026-01-15"
    assert run_row[3] == "PARTIAL"
    assert event_row == (1, "critic", None, "PARTIAL")


def test_trace_writer_promotes_existing_queued_run(tmp_path: Path):
    db_path = tmp_path / "trace.db"
    run_id = "run-queued-1"
    trace_id = "trace-queued-1"
    queued_at = "2026-01-15T00:00:00Z"
    conn = sqlite3.connect(db_path)
    init_trace_db(conn)
    conn.execute(
        """
        INSERT INTO agent_runs
            (run_id, trace_id, ticker, trade_date, status, queued_at, started_at, config)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, trace_id, "AAPL", "2026-01-15", "QUEUED", queued_at, queued_at, "mcj_full"),
    )
    conn.commit()
    conn.close()

    with TraceWriter(db_path=db_path, run_id=run_id, trace_id=trace_id, ticker="AAPL", trade_date="2026-01-15"):
        pass

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT status, queued_at, started_at FROM agent_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "RUNNING"
    assert row[1] == queued_at
    assert row[2] is not None


def test_trace_writer_queued_row_reuses_existing_trace_id(tmp_path: Path):
    db_path = tmp_path / "trace.db"
    run_id = "run-queued-2"
    existing_trace_id = "trace-existing-2"
    conn = sqlite3.connect(db_path)
    init_trace_db(conn)
    conn.execute(
        """
        INSERT INTO agent_runs
            (run_id, trace_id, ticker, trade_date, status, queued_at, config)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, existing_trace_id, "AAPL", "2026-01-15", "QUEUED", "2026-01-15T00:00:00Z", "mcj_full"),
    )
    conn.commit()
    conn.close()

    with TraceWriter(db_path=db_path, run_id=run_id, ticker="AAPL", trade_date="2026-01-15") as writer:
        writer.event(
            node="critic",
            started_at="2026-01-15T00:00:00Z",
            ended_at="2026-01-15T00:00:01Z",
            latency_ms=1000,
            model_id="m",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.1,
            decision=None,
            error_type=None,
            error_message=None,
            status_before=None,
            status_after=None,
        )
        assert writer.trace_id == existing_trace_id

    conn = sqlite3.connect(db_path)
    run_trace = conn.execute("SELECT trace_id FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    event_trace = conn.execute("SELECT trace_id FROM trace_events WHERE run_id = ?", (run_id,)).fetchone()[0]
    conn.close()
    assert run_trace == existing_trace_id
    assert event_trace == existing_trace_id


def test_init_trace_db_allows_queued_row_without_started_at(tmp_path: Path):
    db_path = tmp_path / "trace.db"
    conn = sqlite3.connect(db_path)
    init_trace_db(conn)
    conn.execute(
        """
        INSERT INTO agent_runs
            (run_id, trace_id, ticker, trade_date, status, queued_at, config)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("rq1", "tq1", "AAPL", "2026-01-15", "QUEUED", "2026-01-15T00:00:00Z", "mcj_full"),
    )
    conn.commit()
    started_at = conn.execute("SELECT started_at FROM agent_runs WHERE run_id = 'rq1'").fetchone()[0]
    conn.close()
    assert started_at is None


def test_init_trace_db_migrates_old_agent_runs_started_at_to_nullable(tmp_path: Path):
    db_path = tmp_path / "trace.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE agent_runs (
            run_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            ticker TEXT,
            trade_date TEXT,
            status TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            total_latency_ms INTEGER,
            total_cost_usd REAL DEFAULT 0.0,
            model_id_per_role TEXT,
            config TEXT,
            error_type TEXT,
            error_message TEXT
        );
        INSERT INTO agent_runs
            (run_id, trace_id, ticker, trade_date, status, started_at, config)
        VALUES
            ('old-run-1', 'old-trace-1', 'AAPL', '2026-01-15', 'SUFFICIENT', '2026-01-15T00:00:00Z', 'mcj_full');
        """
    )
    conn.commit()

    init_trace_db(conn)

    started_at_info = {
        row[1]: row
        for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
    }["started_at"]
    row = conn.execute(
        "SELECT trace_id, ticker, trade_date, status, started_at, config FROM agent_runs WHERE run_id = 'old-run-1'"
    ).fetchone()
    conn.close()

    assert started_at_info[3] == 0
    assert row == (
        "old-trace-1",
        "AAPL",
        "2026-01-15",
        "SUFFICIENT",
        "2026-01-15T00:00:00Z",
        "mcj_full",
    )


def test_init_trace_db_migrates_old_agent_runs_with_foreign_keys_on_trace_events(tmp_path: Path):
    db_path = tmp_path / "trace.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE agent_runs (
            run_id            TEXT PRIMARY KEY,
            trace_id          TEXT NOT NULL,
            ticker            TEXT,
            trade_date        TEXT,
            status            TEXT,
            started_at        TEXT NOT NULL,
            ended_at          TEXT,
            total_latency_ms  INTEGER,
            total_cost_usd    REAL DEFAULT 0.0,
            model_id_per_role TEXT,
            config            TEXT,
            error_type        TEXT,
            error_message     TEXT
        );
        CREATE TABLE trace_events (
            run_id         TEXT NOT NULL,
            trace_id       TEXT NOT NULL,
            event_seq      INTEGER NOT NULL,
            node           TEXT NOT NULL,
            started_at     TEXT NOT NULL,
            ended_at       TEXT NOT NULL,
            latency_ms     INTEGER NOT NULL,
            model_id       TEXT,
            input_tokens   INTEGER,
            output_tokens  INTEGER,
            cost_usd       REAL,
            decision       TEXT,
            error_type     TEXT,
            error_message  TEXT,
            status_before  TEXT,
            status_after   TEXT,
            PRIMARY KEY (run_id, event_seq),
            FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
        );
        INSERT INTO agent_runs
            (run_id, trace_id, ticker, trade_date, status, started_at, config)
        VALUES
            ('old-run-fk', 'old-trace-fk', 'AAPL', '2026-01-15', 'SUFFICIENT', '2026-01-15T00:00:00Z', 'mcj_full');
        INSERT INTO trace_events
            (run_id, trace_id, event_seq, node, started_at, ended_at, latency_ms)
        VALUES
            ('old-run-fk', 'old-trace-fk', 1, 'critic', '2026-01-15T00:00:00Z', '2026-01-15T00:00:01Z', 1000);
        """
    )
    conn.commit()

    init_trace_db(conn)

    started_at_info = {
        row[1]: row
        for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
    }["started_at"]
    run_row = conn.execute(
        "SELECT trace_id, status, started_at FROM agent_runs WHERE run_id = 'old-run-fk'"
    ).fetchone()
    event_row = conn.execute(
        "SELECT trace_id, node FROM trace_events WHERE run_id = 'old-run-fk' AND event_seq = 1"
    ).fetchone()
    fk_check = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()

    assert started_at_info[3] == 0
    assert run_row == ("old-trace-fk", "SUFFICIENT", "2026-01-15T00:00:00Z")
    assert event_row == ("old-trace-fk", "critic")
    assert fk_check == []


def test_node_artifact_roundtrip_and_filters(tmp_path: Path):
    db_path = tmp_path / "trace.db"
    conn = sqlite3.connect(db_path)
    init_trace_db(conn)
    conn.execute("INSERT INTO agent_runs (run_id, trace_id, status) VALUES ('r1', 't1', 'RUNNING')")
    for event_seq, node in ((1, "miner"), (2, "critic")):
        conn.execute(
            """INSERT INTO trace_events
            (run_id, trace_id, event_seq, node, started_at, ended_at, latency_ms)
            VALUES ('r1', 't1', ?, ?, '2026-01-15T00:00:00Z', '2026-01-15T00:00:01Z', 1)""",
            (event_seq, node),
        )

    write_node_artifact(
        conn,
        run_id="r1",
        event_seq=1,
        node="miner",
        artifact_type="retrieved_chunks",
        payload={"chunks": [{"id": "c1"}]},
    )
    write_node_artifact(
        conn,
        run_id="r1",
        event_seq=2,
        node="critic",
        artifact_type="graded_evidence",
        payload={"items": [{"chunk_id": "c1"}]},
    )

    all_rows = read_node_artifacts(conn, run_id="r1")
    seq_rows = read_node_artifacts(conn, run_id="r1", event_seq=2)
    typed_rows = read_node_artifacts(conn, run_id="r1", artifact_type="retrieved_chunks")
    conn.close()

    assert len(all_rows) == 2
    assert len(seq_rows) == 1
    assert seq_rows[0]["artifact_type"] == "graded_evidence"
    assert len(typed_rows) == 1
    assert typed_rows[0]["payload_json"]["chunks"][0]["id"] == "c1"


def test_node_artifact_rejects_non_serializable_payload(tmp_path: Path):
    db_path = tmp_path / "trace.db"
    conn = sqlite3.connect(db_path)
    init_trace_db(conn)

    with pytest.raises(TypeError):
        write_node_artifact(
            conn,
            run_id="r1",
            event_seq=1,
            node="miner",
            artifact_type="state_snapshot",
            payload={"bad": object()},
        )
    conn.close()


def test_project_node_artifacts_mapping_and_fields():
    merged_state = {
        "retrieved_chunks": [
            {
                "asset_id": "c1",
                "ticker": "AAPL",
                "source_type": "polygon_news",
                "reference_date": "2026-01-15",
                "rrf_score": 0.91,
                "content_md": "## AAPL: Headline One\nBody text for chunk one.",
            }
        ],
        "reranked_chunks": [
            {
                "asset_id": "c1",
                "ticker": "AAPL",
                "source_type": "polygon_news",
                "reference_date": "2026-01-15",
                "rrf_score": 0.91,
                "rerank_score": 0.88,
                "content_md": "## AAPL: Headline One\nBody text for reranked one.",
            }
        ],
        "graded_evidence": [
            {
                "chunk_id": "c1",
                "relevance": 0.77,
                "category": "geopolitical",
                "temporal_match": True,
                "reasoning": "Directly linked",
                "event_specificity": 0.8,
                "temporal_alignment": 0.7,
                "evidence_granularity": 0.6,
                "conflict_signal": 0.1,
                "original_relevance": 0.9,
            }
        ],
        "all_graded_chunks": [],
        "critic_decision": {"next_action": "proceed"},
        "retrieval_metadata": object(),
        "table": object(),
        "embedding_fn": lambda _: [0.1],
        "reranker": object(),
    }
    result = {"graded_evidence": merged_state["graded_evidence"]}

    critic_artifacts = project_node_artifacts("critic", merged_state, result)
    miner_artifacts = project_node_artifacts("miner", merged_state, {})

    critic_types = {a["artifact_type"] for a in critic_artifacts}
    miner_types = {a["artifact_type"] for a in miner_artifacts}
    retrieved = next(a for a in miner_artifacts if a["artifact_type"] == "retrieved_chunks")["payload_json"]
    graded = next(a for a in critic_artifacts if a["artifact_type"] == "graded_evidence")["payload_json"]

    assert "retrieved_chunks" not in critic_types
    assert {"graded_evidence", "critic_decision", "state_snapshot"} <= critic_types
    assert {"retrieved_chunks", "reranked_chunks", "state_snapshot"} <= miner_types
    assert retrieved["chunks"][0]["headline"] == "Headline One"
    assert isinstance(graded["items"][0]["temporal_match"], bool)
    assert graded["items"][0]["event_specificity"] == 0.8


def test_state_snapshot_is_compact_and_excludes_large_fields():
    merged_state = {
        "phase": "critic",
        "output_status": "PARTIAL",
        "retrieved_chunks": [{"asset_id": "c1", "content_md": "x"}],
        "reranked_chunks": [{"asset_id": "c1", "content_md": "x"}],
        "graded_evidence": [{"chunk_id": "c1"}],
        "all_graded_chunks": [{"chunk_id": "c1"}],
        "critic_raw_llm_response": "x" * 5000,
        "judge_raw_llm_response": "y" * 5000,
    }
    artifacts = project_node_artifacts("critic", merged_state, {})
    snapshot = next(a for a in artifacts if a["artifact_type"] == "state_snapshot")["payload_json"]["state"]
    assert "retrieved_chunks" not in snapshot
    assert "reranked_chunks" not in snapshot
    assert "graded_evidence" not in snapshot
    assert "critic_raw_llm_response" not in snapshot
    assert snapshot["phase"] == "critic"


def test_exporter_writes_json_file(tmp_path: Path):
    db_path = tmp_path / "trace.db"
    out_path = tmp_path / "trace.json"

    with TraceWriter(db_path=db_path, ticker="AAPL", trade_date="2026-01-15") as writer:
        writer.event(
            node="decision_router",
            started_at="2026-01-15T00:00:00Z",
            ended_at="2026-01-15T00:00:00Z",
            latency_ms=0,
            model_id=None,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            decision='{"router_edge":"judge"}',
            error_type=None,
            error_message=None,
            status_before=None,
            status_after=None,
        )
        writer.complete({"output_status": "SUFFICIENT", "total_cost_usd": 0.0})

    payload = export_run(writer.run_id, out_path=out_path, db_path=db_path)

    loaded = json.loads(out_path.read_text())
    assert payload["run_id"] == writer.run_id
    assert loaded["trace_id"] == writer.trace_id
    assert loaded["events"][0]["node"] == "decision_router"


def test_graph_invoke_persists_trace_rows(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "trace_graph.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(db_path))

    class MockUsage:
        def __init__(self) -> None:
            self.input_tokens = 3000
            self.output_tokens = 500
            self.total_tokens = 3500

    class MockResponse:
        def __init__(self, content: str) -> None:
            self.content = content
            self.usage = MockUsage()

    class MockLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.responses = [
                json.dumps(
                    {
                        "graded_chunks": [
                            {
                                "chunk_id": "c1",
                                "relevance": 0.9,
                                "category": "earnings",
                                "temporal_match": True,
                                "reasoning": "Direct cause",
                            }
                        ],
                        "reasoning": "Strong evidence",
                    }
                ),
                json.dumps(
                    {
                        "hypotheses": [
                            {
                                "cause_label": "earnings_guidance",
                                "direction": "negative",
                                "transmission_mechanism": "Guidance weakness reduced expectations",
                                "supporting_evidence_ids": ["c1"],
                                "counter_evidence_ids": [],
                                "missing_evidence": [],
                                "change_condition": "Reassess if guidance improves",
                                "facts": ["Guidance was reduced"],
                                "calculations": [],
                                "inferences": ["Investors priced lower forward revenue"],
                                "unavailable_evidence": [],
                            }
                        ],
                        "summary_md": "AAPL dropped due to [c1] export ban.",
                    }
                ),
            ]

        def invoke(self, prompt: str) -> MockResponse:
            idx = min(self.calls, len(self.responses) - 1)
            self.calls += 1
            return MockResponse(self.responses[idx])

    graph = build_attribution_graph(
        context_provider=mock_provider_with_ohlcv(),
        retriever=FixtureRetriever(),
        cutoff_policy=RecordingCutoffPolicy(),
        requested_manifest_id="corpus-fixture-v1",
        use_critic=True,
        llm=MockLLM(),
    )
    result = graph.invoke(
        {
            "ticker": "AAPL",
            "trade_date": "2026-01-15",
            "query": None,
            "price_move_pct": -4.2,
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "graded_evidence": [],
            "critic_reasoning": "",
            "critic_decision": None,
            "causes": [],
            "summary_md": "",
            "grounding_rate": None,
            "output_status": None,
            "validation_error": None,
            "validator_attempts": 0,
            "phase": None,
            "router_edge": None,
            "router_reason": None,
            "expansions_used": 0,
            "max_expansions": 2,
            "current_layer": "direct",
            "retrieval_metadata": None,
            "cost_breakdown": [],
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "model_id": "claude-sonnet-4-20250514",
            "error_type": None,
        }
    )

    conn = sqlite3.connect(db_path)
    run_row = conn.execute("SELECT run_id, trace_id, status FROM agent_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    event_count = conn.execute("SELECT COUNT(*) FROM trace_events WHERE run_id = ?", (run_row[0],)).fetchone()[0]
    nodes = [row[0] for row in conn.execute("SELECT node FROM trace_events WHERE run_id = ? ORDER BY event_seq", (run_row[0],)).fetchall()]
    conn.close()

    assert result["summary_md"] != ""
    assert run_row is not None
    assert run_row[2] in {"SUFFICIENT", "PARTIAL"}
    assert event_count >= 6
    assert "miner" in nodes
    assert "finalizer" in nodes


def test_unknown_run_cost_persists_as_null(tmp_path: Path, monkeypatch):
    from catalyst_agents.trace.writer import TraceWriter

    db_path = tmp_path / "trace_unknown_cost.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(db_path))
    with TraceWriter(run_id="unknown-cost", ticker="AAPL", trade_date="2026-01-15", config="test") as writer:
        writer.complete({
            "output_status": "SYSTEM_ERROR",
            "error_type": "system_error",
            "validation_error": "unknown model price",
            "total_cost_usd": None,
            "cost_status": "unknown",
            "cost_breakdown": [{"node": "judge", "model_id": "unknown", "cost_status": "unknown", "cost_usd": None}],
            "cutoff": "2026-01-15T21:00:00Z",
            "context_cutoff": "2026-01-15T21:00:00Z",
            "retrieval_cutoff": "2026-01-15T21:00:00Z",
            "validator_cutoff": "2026-01-15T21:00:00Z",
            "context_artifact": {"schema_version": "1.0.0"},
        })

    conn = sqlite3.connect(db_path)
    stored = conn.execute("SELECT total_cost_usd FROM agent_runs WHERE run_id='unknown-cost'").fetchone()[0]
    conn.close()
    assert stored is None
