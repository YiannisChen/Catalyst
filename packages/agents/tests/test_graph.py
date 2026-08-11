"""Tests for graph.py — LangGraph MCJ workflow assembly.

Spec reference: Section 4.4 — Graph Construction.

TDD: tests written before implementation.
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch, MagicMock

from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.trace.artifacts import read_node_artifacts
from catalyst_agents.state import OutputStatus
from catalyst_agents.retrieval.policy import Layer
from attribution_fixtures import FixtureRetriever, RecordingCutoffPolicy, mock_provider_with_ohlcv


# ---------------------------------------------------------------------------
# Integration fixture data
# ---------------------------------------------------------------------------

MOCK_CHUNKS = [
    {
        "asset_id": "c1",
        "ticker": "AAPL",
        "source_type": "polygon_news",
        "reference_date": "2026-01-15",
        "content_md": "China chip ban caused significant concern for Apple supply chain.",
        "rrf_score": 0.5,
    }
]

CRITIC_RESPONSE = json.dumps({
    "graded_chunks": [
        {
            "chunk_id": "c1",
            "relevance": 0.9,
            "category": "earnings",
            "temporal_match": True,
            "reasoning": "Direct cause — chip ban impacts Apple supply chain.",
        }
    ],
    "reasoning": "Strong earnings evidence directly linked to the price move.",
})

JUDGE_RESPONSE = json.dumps({
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
    "summary_md": "AAPL dropped after [c1] guidance weakness.",
})


class MockUsage:
    def __init__(self):
        self.input_tokens = 3000
        self.output_tokens = 500
        self.total_tokens = 3500


class MockResponse:
    def __init__(self, content: str):
        self.content = content
        self.usage = MockUsage()


class MockLLM:
    """Returns responses sequentially — first call gets critic response, second gets judge."""

    def __init__(self):
        self._call_count = 0
        self._responses = [CRITIC_RESPONSE, JUDGE_RESPONSE]

    def invoke(self, prompt: str) -> MockResponse:
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return MockResponse(self._responses[idx])

    @property
    def call_count(self) -> int:
        return self._call_count


class FailingLLM:
    def invoke(self, prompt: str) -> MockResponse:
        raise RuntimeError("llm unavailable")


def mock_hybrid_search(table, query, ticker=None, date_range=None, top_k=20, embedding_fn=None):
    return MOCK_CHUNKS * min(top_k, 8)


def mock_retrieve(query, layer, metadata, *, rerank=None):
    return MOCK_CHUNKS[: metadata.top_k]


def mock_retrieve_empty(query, layer, metadata, *, rerank=None):
    return []


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

def _base_state() -> dict:
    return {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": None,
        "price_move_pct": -4.2,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "graded_evidence": [],
        "critic_reasoning": "",
        "causes": [],
        "summary_md": "",
        "grounding_rate": None,
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
        "model_id": "claude-sonnet-4-20250514",
        "cost_status": "known",
    }


def _graph(llm=None, **kwargs):
    return build_attribution_graph(
        context_provider=mock_provider_with_ohlcv(),
        retriever=FixtureRetriever(),
        cutoff_policy=RecordingCutoffPolicy(),
        requested_manifest_id="corpus-fixture-v1",
        llm=llm or MockLLM(),
        **kwargs,
    )


def test_mcj_graph_full_pipeline(monkeypatch):
    """Full MCJ path: miner → critic → judge produces valid attribution."""
    graph = _graph(use_critic=True, llm=MockLLM())

    result = graph.invoke(_base_state())

    assert len(result["causes"]) > 0
    assert result["summary_md"] != ""
    assert result["grounding_rate"] is not None
    assert result["grounding_rate"] >= 0.0
    assert result["output_status"] in {OutputStatus.SUFFICIENT, OutputStatus.PARTIAL}


def test_mcj_graph_accumulates_total_costs(monkeypatch):
    graph = _graph(use_critic=True, llm=MockLLM())
    result = graph.invoke(_base_state())

    charged_nodes = [entry["node"] for entry in result["cost_breakdown"]]
    assert charged_nodes == ["critic", "judge"]
    assert result["total_cost_usd"] > 0
    assert result["total_tokens"] > 0


def test_baseline_graph_skips_critic(monkeypatch):
    """With use_critic=False, critic node is bypassed and no critic cost is recorded."""
    llm = MockLLM()
    llm._responses = [JUDGE_RESPONSE]  # Only judge response — critic never called
    graph = _graph(use_critic=False, llm=llm)

    result = graph.invoke(_base_state())

    assert len(result["causes"]) > 0
    # Critic was skipped, so no critic cost entry
    critic_costs = [c for c in result["cost_breakdown"] if c["node"] == "critic"]
    assert len(critic_costs) == 0


def test_baseline_graph_passes_usable_evidence_to_judge(monkeypatch):
    """Baseline Miner->Judge must still expose evidence IDs for grounding and citations."""
    llm = MockLLM()
    llm._responses = [JUDGE_RESPONSE]  # Judge only in baseline mode
    graph = _graph(use_critic=False, llm=llm)

    result = graph.invoke(_base_state())

    assert result["causes"][0]["evidence_ids"] == ["c1"]
    assert result["grounding_rate"] == 1.0


def test_mcj_insufficient_evidence_path(monkeypatch):
    """When all chunks have relevance <= 0.5, routing goes to insufficient_handler."""

    all_low = json.dumps({
        "graded_chunks": [
            {
                "chunk_id": "c1",
                "relevance": 0.1,
                "category": "technical",
                "temporal_match": False,
                "reasoning": "Tangential reference, not directly relevant.",
            }
        ],
        "reasoning": "No evidence above the threshold.",
    })

    llm = MockLLM()
    llm._responses = [all_low]  # Critic only — judge must NOT be called
    graph = _graph(use_critic=True, llm=llm)

    result = graph.invoke(_base_state())

    assert result["causes"][0]["category"] == "unknown"
    assert "Abstained" in result["causes"][0]["text"]
    assert result["grounding_rate"] is None


def test_mcj_zero_retrieved_evidence_does_not_call_judge(monkeypatch):
    llm = MockLLM()
    graph = build_attribution_graph(context_provider=mock_provider_with_ohlcv(), retriever=FixtureRetriever(evidence=()), cutoff_policy=RecordingCutoffPolicy(), requested_manifest_id="corpus-fixture-v1", use_critic=True, llm=llm)

    result = graph.invoke(_base_state())

    assert result["output_status"] == OutputStatus.ABSTAIN
    assert "Abstained" in result["causes"][0]["text"]
    assert llm.call_count == 0


def test_mcj_all_rejected_evidence_does_not_call_judge(monkeypatch):

    all_low = json.dumps({
        "graded_chunks": [
            {
                "chunk_id": "c1",
                "relevance": 0.1,
                "category": "technical",
                "temporal_match": False,
                "reasoning": "Tangential reference, not directly relevant.",
            }
        ],
        "reasoning": "No evidence above the threshold.",
    })

    llm = MockLLM()
    llm._responses = [all_low, JUDGE_RESPONSE]
    graph = _graph(use_critic=True, llm=llm)

    result = graph.invoke(_base_state())

    assert result["output_status"] == OutputStatus.ABSTAIN
    assert "Abstained" in result["causes"][0]["text"]
    assert llm.call_count == 1


def test_mcj_graph_critic_failure_routes_to_system_error(monkeypatch):
    """LLM failure must route to system_error_handler, not insufficient_handler (BUG-005)."""
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)

    graph = _graph(use_critic=True, llm=FailingLLM())
    result = graph.invoke(_base_state())

    assert result["causes"] == []
    assert "System error" in result["summary_md"]
    assert result["grounding_rate"] is None


def test_mcj_graph_starts_with_context_builder_then_miner(tmp_path, monkeypatch):
    db_path = tmp_path / "trace_graph.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(db_path))
    graph = _graph(use_critic=True, llm=MockLLM())
    result = graph.invoke(_base_state(), run_id="order-run")
    conn = sqlite3.connect(db_path)
    nodes = [row[0] for row in conn.execute("SELECT node FROM trace_events WHERE run_id = ? ORDER BY event_seq", ("order-run",)).fetchall()]
    conn.close()
    assert nodes[:2] == ["context_builder", "miner"]
    assert result["summary_md"] != ""


def test_baseline_graph_judge_failure_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)

    graph = _graph(use_critic=False, llm=FailingLLM())
    result = graph.invoke(_base_state())

    assert result["causes"][0]["category"] == "unknown"
    assert result["grounding_rate"] is None


def test_build_graph_returns_invokable_object():
    """build_attribution_graph must always return an object with .invoke()."""
    graph = _graph(use_critic=True)
    assert callable(getattr(graph, "invoke", None))


def test_build_graph_no_critic_returns_invokable_object():
    """build_attribution_graph(use_critic=False) must return an object with .invoke()."""
    graph = _graph(use_critic=False)
    assert callable(getattr(graph, "invoke", None))


def test_graph_invoke_uses_provided_run_id_and_persists_artifacts(tmp_path, monkeypatch):
    db_path = tmp_path / "trace_graph.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(db_path))
    run_id = "external-run-1"

    graph = _graph(use_critic=True, llm=MockLLM())
    result = graph.invoke(_base_state(), run_id=run_id)

    conn = sqlite3.connect(db_path)
    run = conn.execute("SELECT run_id FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    artifacts = read_node_artifacts(conn, run_id=run_id)
    conn.close()
    types_by_node = {}
    for artifact in artifacts:
        types_by_node.setdefault(artifact["node"], set()).add(artifact["artifact_type"])

    assert result["run_id"] == run_id
    assert run is not None
    assert "retrieved_chunks" in types_by_node.get("miner", set())
    assert "graded_evidence" in types_by_node.get("critic", set())
    assert "retrieved_chunks" not in types_by_node.get("critic", set())


def test_graph_persists_raw_llm_response_artifacts(tmp_path, monkeypatch):
    db_path = tmp_path / "trace_graph.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(db_path))
    run_id = "external-run-2"

    graph = _graph(use_critic=True, llm=MockLLM())
    graph.invoke(_base_state(), run_id=run_id)

    conn = sqlite3.connect(db_path)
    critic_rows = read_node_artifacts(conn, run_id=run_id, artifact_type="raw_llm_response")
    conn.close()
    nodes = {row["node"] for row in critic_rows}

    assert "critic" in nodes
    assert "judge" in nodes


def test_graph_ignores_artifact_write_failure_and_keeps_mcj_result(monkeypatch):

    def _boom(*args, **kwargs):
        raise TypeError("artifact write failed")

    monkeypatch.setattr("catalyst_agents.graph.write_node_artifact", _boom)
    graph = _graph(use_critic=True, llm=MockLLM())
    result = graph.invoke(_base_state())

    assert len(result["causes"]) > 0
    assert result["summary_md"] != ""


def test_graph_with_production_exchange_cutoff_policy(monkeypatch):
    """Regression: the runtime graph must work with the real ExchangeCutoffPolicy,
    whose compute_cutoff contract matches the module-level cutoff function."""
    from catalyst_data.retrieval.cutoff import ExchangeCutoffPolicy

    graph = build_attribution_graph(
        context_provider=mock_provider_with_ohlcv(),
        retriever=FixtureRetriever(),
        cutoff_policy=ExchangeCutoffPolicy(),
        requested_manifest_id="corpus-fixture-v1",
        use_critic=True,
        llm=MockLLM(),
    )
    result = graph.invoke(_base_state())
    assert result["cutoff"] == "2026-01-15T21:00:00Z"
    assert result["output_status"] in {OutputStatus.SUFFICIENT, OutputStatus.PARTIAL, OutputStatus.ABSTAIN}
