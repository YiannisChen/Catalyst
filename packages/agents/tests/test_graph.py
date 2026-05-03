"""Tests for graph.py — LangGraph MCJ workflow assembly.

Spec reference: Section 4.4 — Graph Construction.

TDD: tests written before implementation.
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.state import OutputStatus
from catalyst_agents.retrieval.policy import Layer


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
            "category": "geopolitical",
            "temporal_match": True,
            "reasoning": "Direct cause — chip ban impacts Apple supply chain.",
        }
    ],
    "reasoning": "Strong geopolitical evidence directly linked to the price move.",
})

JUDGE_RESPONSE = json.dumps({
    "causes": [
        {
            "text": "China chip ban impacted Apple supply chain",
            "category": "geopolitical",
            "confidence": 0.8,
            "evidence_ids": ["c1"],
            "direction": "negative",
        }
    ],
    "summary_md": "AAPL dropped due to [c1] China export ban affecting supply chain.",
    "self_grounding_check": {
        "total_claims": 1,
        "grounded_claims": 1,
        "ungrounded_claims": 0,
    },
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


class FailingLLM:
    def invoke(self, prompt: str) -> MockResponse:
        raise RuntimeError("llm unavailable")


def mock_hybrid_search(table, query, ticker=None, date_range=None, top_k=20, embedding_fn=None):
    return MOCK_CHUNKS * min(top_k, 8)


def mock_retrieve(query, layer, metadata, *, rerank=None):
    return MOCK_CHUNKS[: metadata.top_k]


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
    }


def test_mcj_graph_full_pipeline(monkeypatch):
    """Full MCJ path: miner → critic → judge produces valid attribution."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    graph = build_attribution_graph(use_critic=True, llm=MockLLM())

    result = graph.invoke(_base_state())

    assert len(result["causes"]) > 0
    assert result["summary_md"] != ""
    assert result["grounding_rate"] is not None
    assert result["grounding_rate"] >= 0.0
    assert result["output_status"] in {OutputStatus.SUFFICIENT, OutputStatus.PARTIAL}


def test_mcj_graph_accumulates_total_costs(monkeypatch):
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    graph = build_attribution_graph(use_critic=True, llm=MockLLM())
    result = graph.invoke(_base_state())

    assert len(result["cost_breakdown"]) == 2
    assert result["total_cost_usd"] > 0
    assert result["total_tokens"] > 0


def test_baseline_graph_skips_critic(monkeypatch):
    """With use_critic=False, critic node is bypassed and no critic cost is recorded."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    llm = MockLLM()
    llm._responses = [JUDGE_RESPONSE]  # Only judge response — critic never called
    graph = build_attribution_graph(use_critic=False, llm=llm)

    result = graph.invoke(_base_state())

    assert len(result["causes"]) > 0
    # Critic was skipped, so no critic cost entry
    critic_costs = [c for c in result["cost_breakdown"] if c["node"] == "critic"]
    assert len(critic_costs) == 0


def test_baseline_graph_passes_usable_evidence_to_judge(monkeypatch):
    """Baseline Miner->Judge must still expose evidence IDs for grounding and citations."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    llm = MockLLM()
    llm._responses = [JUDGE_RESPONSE]  # Judge only in baseline mode
    graph = build_attribution_graph(use_critic=False, llm=llm)

    result = graph.invoke(_base_state())

    assert result["causes"][0]["evidence_ids"] == ["c1"]
    assert result["grounding_rate"] == 1.0


def test_mcj_insufficient_evidence_path(monkeypatch):
    """When all chunks have relevance <= 0.5, routing goes to insufficient_handler."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

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
    graph = build_attribution_graph(use_critic=True, llm=llm)

    result = graph.invoke(_base_state())

    assert result["causes"][0]["category"] == "unknown"
    assert "Insufficient evidence" in result["causes"][0]["text"]
    assert result["grounding_rate"] is None


def test_mcj_graph_critic_failure_routes_to_system_error(monkeypatch):
    """LLM failure must route to system_error_handler, not insufficient_handler (BUG-005)."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)

    graph = build_attribution_graph(use_critic=True, llm=FailingLLM())
    result = graph.invoke(_base_state())

    assert result["causes"] == []
    assert "System error" in result["summary_md"]
    assert result["grounding_rate"] is None


def test_mcj_graph_expand_macro_loops_back_through_miner(monkeypatch):
    captured_layers = []

    def fake_retrieve(query, layer, metadata, *, rerank=None):
        captured_layers.append(layer)
        return MOCK_CHUNKS

    decisions = iter(
        [
            ("partial", "expand_macro", 0.45, "Need macro expansion."),
            ("partial", "proceed", 0.65, "Macro evidence is enough to judge."),
        ]
    )

    def fake_decision(filtered, reasoning):
        sufficiency, next_action, magnitude_coverage, reason = next(decisions)
        from catalyst_agents.state import CriticDecision

        return CriticDecision(
            sufficiency=sufficiency,
            next_action=next_action,
            magnitude_coverage=magnitude_coverage,
            reasoning=reason,
        )

    llm = MockLLM()
    llm._responses = [CRITIC_RESPONSE, CRITIC_RESPONSE, JUDGE_RESPONSE]

    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", fake_retrieve)
    monkeypatch.setattr("catalyst_agents.nodes.critic._build_critic_decision", fake_decision)

    graph = build_attribution_graph(use_critic=True, llm=llm)
    result = graph.invoke(_base_state())

    assert captured_layers == [Layer.DIRECT, Layer.MACRO]
    assert result["summary_md"] != ""


def test_baseline_graph_judge_failure_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)

    graph = build_attribution_graph(use_critic=False, llm=FailingLLM())
    result = graph.invoke(_base_state())

    assert result["causes"][0]["category"] == "unknown"
    assert result["grounding_rate"] is None


def test_build_graph_returns_invokable_object():
    """build_attribution_graph must always return an object with .invoke()."""
    graph = build_attribution_graph(use_critic=True)
    assert callable(getattr(graph, "invoke", None))


def test_build_graph_no_critic_returns_invokable_object():
    """build_attribution_graph(use_critic=False) must return an object with .invoke()."""
    graph = build_attribution_graph(use_critic=False)
    assert callable(getattr(graph, "invoke", None))
