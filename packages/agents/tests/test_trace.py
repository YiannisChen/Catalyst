"""Tests for local SQLite trace persistence."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.trace.exporter import export_run
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


def test_trace_writer_persists_run_and_event(tmp_path: Path):
    db_path = tmp_path / "trace.db"

    with TraceWriter(db_path=db_path, ticker="AAPL", trade_date="2026-01-15") as writer:
        writer.event(
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
    monkeypatch.setenv("CATALYST_DB_PATH", str(db_path))

    chunks = [
        {
            "asset_id": "c1",
            "ticker": "AAPL",
            "source_type": "polygon_news",
            "reference_date": "2026-01-15",
            "content_md": "China chip ban expanded.",
            "rrf_score": 0.5,
        }
    ]

    def mock_retrieve(query, layer, metadata, *, rerank=None):
        return chunks[: metadata.top_k]

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
                                "category": "geopolitical",
                                "temporal_match": True,
                                "reasoning": "Direct cause",
                            }
                        ],
                        "reasoning": "Strong evidence",
                    }
                ),
                json.dumps(
                    {
                        "causes": [
                            {
                                "text": "China chip ban",
                                "category": "geopolitical",
                                "confidence": 0.8,
                                "evidence_ids": ["c1"],
                                "direction": "negative",
                            }
                        ],
                        "summary_md": "AAPL dropped due to [c1] export ban.",
                        "self_grounding_check": {
                            "total_claims": 1,
                            "grounded_claims": 1,
                            "ungrounded_claims": 0,
                        },
                    }
                ),
            ]

        def invoke(self, prompt: str) -> MockResponse:
            idx = min(self.calls, len(self.responses) - 1)
            self.calls += 1
            return MockResponse(self.responses[idx])

    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    graph = build_attribution_graph(use_critic=True, llm=MockLLM())
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
