from __future__ import annotations

import json
import socket
import sqlite3
import pytest

from attribution_fixtures import FixtureRetriever, RecordingCutoffPolicy, StubModelClient, mock_provider_with_ohlcv
from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.runtime.runner import LiveRunRunner
from catalyst_agents.runtime.service import LiveRunService
from catalyst_agents.state import OutputStatus
from catalyst_agents.trace.schema import init_trace_db


CRITIC_TWO = json.dumps({
    "graded_chunks": [
        {"chunk_id": "c1", "relevance": 0.9, "category": "earnings", "temporal_match": True, "reasoning": "Direct guidance support."},
        {"chunk_id": "c2", "relevance": 0.8, "category": "other", "temporal_match": True, "reasoning": "Reported corroboration."},
    ],
    "reasoning": "Two pieces of evidence support guidance weakness.",
})

JUDGE_TWO = json.dumps({
    "hypotheses": [
        {
            "cause_label": "earnings_guidance",
            "direction": "negative",
            "transmission_mechanism": "Guidance weakness reduced expectations",
            "supporting_evidence_ids": ["c1", "c2"],
            "counter_evidence_ids": [],
            "missing_evidence": [],
            "change_condition": "Reassess if guidance improves",
            "facts": ["Guidance was reduced"],
            "calculations": [],
            "inferences": ["Investors priced lower forward revenue"],
            "unavailable_evidence": [],
        }
    ],
    "summary_md": "AAPL dropped after [c1] guidance weakness, corroborated by [c2].",
})


def _state():
    return {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": None,
        "price_move_pct": None,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "graded_evidence": [],
        "all_graded_chunks": [],
        "critic_reasoning": "",
        "critic_decision": None,
        "error_type": None,
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
        "max_expansions": 0,
        "current_layer": None,
        "retrieval_metadata": None,
        "context_artifact": None,
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "cost_status": "known",
        "total_tokens": 0,
        "model_id": "claude-sonnet-4-20250514",
    }


def _guard_socket(monkeypatch):
    real_socket = socket.socket

    class GuardedSocket(real_socket):
        def connect(self, *args, **kwargs):
            raise AssertionError("network disabled in B5 integration")

    monkeypatch.setattr(socket, "socket", GuardedSocket)


def _graph(model, retriever=None, cutoff_policy=None):
    return build_attribution_graph(
        context_provider=mock_provider_with_ohlcv(),
        retriever=retriever or FixtureRetriever(),
        cutoff_policy=cutoff_policy or RecordingCutoffPolicy(),
        requested_manifest_id="corpus-fixture-v1",
        llm=model,
    )


def test_b5_normal_runtime_persists_trace_and_assurance(tmp_path, monkeypatch):
    _guard_socket(monkeypatch)
    db_path = tmp_path / "trace.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(db_path))
    model = StubModelClient([CRITIC_TWO, JUDGE_TWO])
    cutoff_policy = RecordingCutoffPolicy()
    conn = sqlite3.connect(db_path)
    init_trace_db(conn)
    conn.execute(
        """INSERT INTO agent_runs
        (run_id, trace_id, ticker, trade_date, status, queued_at, config)
        VALUES ('b5-normal', 'trace-b5-normal', 'AAPL', '2026-01-15', 'QUEUED',
                '2026-01-15T20:59:00Z', '{"model":"fixture-model"}')"""
    )
    conn.commit()
    conn.close()
    runner = LiveRunRunner(
        db_path=db_path,
        graph_factory=lambda **_: _graph(model, cutoff_policy=cutoff_policy),
    )
    run_result = runner.run("b5-normal")
    assert run_result["status"] == "COMPLETED"
    result = run_result["result"]

    conn = sqlite3.connect(db_path)
    nodes = [row[0] for row in conn.execute("SELECT node FROM trace_events WHERE run_id = ? ORDER BY event_seq", ("b5-normal",))]
    assurance_rows = conn.execute("SELECT record_json FROM run_assurance WHERE run_id = ?", ("b5-normal",)).fetchall()
    trace_version = conn.execute("SELECT schema_version FROM trace_schema_version WHERE singleton_id = 1").fetchone()[0]
    conn.close()

    assert nodes == ["context_builder", "miner", "critic", "decision_router", "judge", "validator", "finalizer"]
    assert len(model.calls) == 2
    assert cutoff_policy.calls == [
        ("AAPL", "2026-01-15", "attribution"),
        ("AAPL", "2026-01-15", "attribution"),
        ("AAPL", "2026-01-15", "attribution"),
    ]
    assert result["output_status"] == OutputStatus.SUFFICIENT
    assert result["ranked_hypotheses"][0]["cause_label"] == "earnings_guidance"
    assert result["corpus_manifest_id"] == "corpus-fixture-v1"
    assert trace_version == "2.0.0"
    assert len(assurance_rows) == 1
    record = json.loads(assurance_rows[0][0])
    assert record["output_status"] == "SUFFICIENT"
    assert {check["check_name"]: check["status"] for check in record["checks"]} == {
        "cutoff": "pass",
        "citation_resolution": "pass",
        "judge_visibility": "pass",
        "prerequisite_gates": "pass",
        "legal_path": "pass",
        "trace_completeness": "pass",
        "identities": "pass",
        "budget_retry_repair": "pass",
        "degraded_state": "pass",
        "structured_context_support": "pass",
    }
    assert record["source_support_flags"]["issuer_claim_only_support"] is False
    service = LiveRunService(db_path=db_path, graph_factory=lambda **_: None)
    assert service.get_assurance("b5-normal").trace_id == "trace-b5-normal"


def test_b5_abstain_runtime_persists_matching_assurance(tmp_path, monkeypatch):
    _guard_socket(monkeypatch)
    db_path = tmp_path / "trace.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(db_path))
    model = StubModelClient([])
    result = _graph(model, retriever=FixtureRetriever(evidence=())).invoke(_state(), run_id="b5-abstain")
    conn = sqlite3.connect(db_path)
    record = json.loads(conn.execute("SELECT record_json FROM run_assurance WHERE run_id = ?", ("b5-abstain",)).fetchone()[0])
    conn.close()
    assert result["output_status"] == OutputStatus.ABSTAIN
    assert record["output_status"] == "ABSTAIN"
    assert len(model.calls) == 0


def test_b5_system_error_runtime_persists_matching_assurance(tmp_path, monkeypatch):
    _guard_socket(monkeypatch)
    db_path = tmp_path / "trace.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(db_path))
    model = StubModelClient([])
    result = _graph(model, retriever=FixtureRetriever(fail=True)).invoke(_state(), run_id="b5-system")
    conn = sqlite3.connect(db_path)
    record = json.loads(conn.execute("SELECT record_json FROM run_assurance WHERE run_id = ?", ("b5-system",)).fetchone()[0])
    conn.close()
    assert result["output_status"] == OutputStatus.SYSTEM_ERROR
    assert record["output_status"] == "SYSTEM_ERROR"


def test_b5_unhandled_graph_error_still_persists_one_assurance_row(tmp_path, monkeypatch):
    _guard_socket(monkeypatch)
    db_path = tmp_path / "trace.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(db_path))

    class FailingContextProvider:
        def load_context_inputs(self, **kwargs):
            raise RuntimeError("context corruption")

    graph = build_attribution_graph(
        context_provider=FailingContextProvider(),
        retriever=FixtureRetriever(),
        cutoff_policy=RecordingCutoffPolicy(),
        requested_manifest_id="corpus-fixture-v1",
        llm=StubModelClient([]),
    )
    with pytest.raises(RuntimeError, match="context corruption"):
        graph.invoke(_state(), run_id="b5-unhandled")

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT record_json FROM run_assurance WHERE run_id='b5-unhandled'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["output_status"] == "SYSTEM_ERROR"
