from __future__ import annotations

from catalyst_eval.adapters import make_catalyst_predict, make_rag_only_predict
from catalyst_eval.schema.result import AttributionResult


class StubGraph:
    def invoke(self, state):
        return {
            **state,
            "causes": [
                {
                    "text": "Demand weakened",
                    "category": "demand",
                    "confidence": 0.8,
                    "evidence_ids": ["chunk-1"],
                    "direction": "negative",
                }
            ],
            "summary_md": "Demand weakened [chunk-1].",
            "reranked_chunks": [
                {
                    "asset_id": "chunk-1",
                    "content_md": "Channel checks weakened.",
                    "source_type": "reported_news",
                    "rrf_score": 0.5,
                }
            ],
        }


def test_agents_adapter_maps_plain_graph_state():
    result = make_catalyst_predict(StubGraph())("AAPL", "2026-01-15")

    assert isinstance(result, AttributionResult)
    assert result.causes[0].evidence_ids == ["chunk-1"]
    assert result.retrieved_evidence[0].asset_id == "chunk-1"


def test_rag_only_adapter_preserves_the_same_contract():
    result = make_rag_only_predict(StubGraph())("AAPL", "2026-01-15")

    assert result.ticker == "AAPL"
    assert result.trade_date == "2026-01-15"
