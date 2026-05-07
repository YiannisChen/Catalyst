"""Tests for adapter.py — wraps MCJ graph into eval harness predict_fn.

Spec reference: Section 3.7 — Catalyst Adapter.

TDD: tests written before implementation.
"""
from __future__ import annotations

import json

from catalyst_agents.adapter import make_catalyst_predict, make_rag_only_predict
from catalyst_agents.graph import build_attribution_graph
from catalyst_eval.schema.result import AttributionResult
from catalyst_agents.state import OutputStatus


# ---------------------------------------------------------------------------
# Mock infrastructure (reuses patterns from test_graph.py)
# ---------------------------------------------------------------------------

MOCK_CHUNKS = [
    {
        "asset_id": "c1",
        "ticker": "AAPL",
        "source_type": "polygon_news",
        "reference_date": "2026-01-15",
        "content_md": "China chip ban expanded",
        "rrf_score": 0.5,
    }
]


class MockUsage:
    def __init__(self):
        self.input_tokens = 3000
        self.output_tokens = 500
        self.total_tokens = 3500


class MockResponse:
    def __init__(self, content):
        self.content = content
        self.usage = MockUsage()


CRITIC_RESPONSE = json.dumps({
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
})

JUDGE_RESPONSE = json.dumps({
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
})


class MockLLM:
    """Returns responses sequentially — first call: critic, second call: judge."""

    def __init__(self):
        self._call_count = 0
        self._responses = [CRITIC_RESPONSE, JUDGE_RESPONSE]

    def invoke(self, prompt):
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return MockResponse(self._responses[idx])


def mock_hybrid_search(table, query, ticker=None, date_range=None, top_k=20, embedding_fn=None):
    return MOCK_CHUNKS * min(top_k, 8)


def mock_retrieve(query, layer, metadata, *, rerank=None):
    return MOCK_CHUNKS[: metadata.top_k]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_adapter_returns_attribution_result(monkeypatch):
    """make_catalyst_predict must return an AttributionResult for a valid ticker/date."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    graph = build_attribution_graph(use_critic=True, llm=MockLLM())
    predict = make_catalyst_predict(graph)
    result = predict("AAPL", "2026-01-15")

    assert isinstance(result, AttributionResult)
    assert result.ticker == "AAPL"
    assert result.trade_date == "2026-01-15"


def test_adapter_maps_causes_correctly(monkeypatch):
    """Adapter must translate graph causes dicts into PredictedCause instances."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    graph = build_attribution_graph(use_critic=True, llm=MockLLM())
    predict = make_catalyst_predict(graph)
    result = predict("AAPL", "2026-01-15")

    assert len(result.causes) > 0
    cause = result.causes[0]
    assert cause.text == "China chip ban"
    assert cause.category == "geopolitical"
    assert cause.confidence == 0.8
    assert "c1" in cause.evidence_ids


def test_adapter_tracks_cost(monkeypatch):
    """Adapter must propagate cost tracking fields from graph state."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    graph = build_attribution_graph(use_critic=True, llm=MockLLM())
    predict = make_catalyst_predict(graph)
    result = predict("AAPL", "2026-01-15")

    assert result.total_cost_usd > 0
    assert result.total_tokens > 0
    assert len(result.cost_breakdown) == 2  # critic + judge


def test_adapter_includes_retrieved_evidence(monkeypatch):
    """Adapter must expose reranked chunks as RetrievedEvidence with asset_id and content_md."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    graph = build_attribution_graph(use_critic=True, llm=MockLLM())
    predict = make_catalyst_predict(graph)
    result = predict("AAPL", "2026-01-15")

    assert len(result.retrieved_evidence) > 0
    ev = result.retrieved_evidence[0]
    assert ev.asset_id == "c1"
    assert ev.content_md == "China chip ban expanded"


def test_adapter_works_with_eval_harness(monkeypatch):
    """predict_fn produced by make_catalyst_predict must be compatible with evaluate()."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    from catalyst_eval.harness.runner import evaluate
    from catalyst_eval.metrics.attribution_f1 import AttributionF1
    from catalyst_eval.schema.golden_event import GoldenEvent, Cause, CauseCategory

    golden_set = [
        GoldenEvent(
            id="t1",
            ticker="AAPL",
            trade_date="2026-01-15",
            price_move_pct=-4.2,
            causes=[
                Cause(
                    text="China export ban",
                    category=CauseCategory.GEOPOLITICAL,
                    weight=0.7,
                    temporal_anchor="pre-market",
                    evidence_ids=[],
                )
            ],
        )
    ]

    graph = build_attribution_graph(use_critic=True, llm=MockLLM())
    predict = make_catalyst_predict(graph)

    report = evaluate(predict_fn=predict, golden_set=golden_set, metrics=[AttributionF1()])
    assert "attribution_f1" in report.scores
    assert report.scores["attribution_f1"] > 0


def test_rag_only_predict_uses_retrieval_outputs(monkeypatch):
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", mock_retrieve)

    graph = build_attribution_graph(use_critic=True, llm=MockLLM())
    predict = make_rag_only_predict(graph)
    result = predict("AAPL", "2026-01-15")

    assert len(result.retrieved_evidence) > 0
    assert result.retrieved_evidence[0].asset_id == "c1"


def test_rag_only_predict_preserves_insufficient_status_mapping():
    class StubGraph:
        def invoke(self, state):
            return {
                **state,
                "ticker": state["ticker"],
                "trade_date": state["trade_date"],
                "causes": [
                    {
                        "text": "Insufficient evidence in available data sources",
                        "category": "unknown",
                        "confidence": 1.0,
                        "evidence_ids": [],
                        "direction": "unknown",
                    }
                ],
                "summary_md": "No evidence meeting relevance threshold.",
                "reranked_chunks": [],
                "output_status": OutputStatus.INSUFFICIENT,
                "cost_breakdown": [],
                "total_cost_usd": 0.0,
                "total_tokens": 0,
            }

    predict = make_rag_only_predict(StubGraph())
    result = predict("AAPL", "2026-01-15")

    assert result.causes[0].category == "unknown"
    assert result.retrieved_evidence == []
