"""Adapt a Catalyst attribution graph to the evaluation harness contract."""
from __future__ import annotations

from typing import Any, Callable

from catalyst_eval.schema.result import AttributionResult, PredictedCause, RetrievedEvidence


def make_catalyst_predict(
    graph: Any,
    model_id: str = "claude-sonnet-4-20250514",
) -> Callable[[str, str], AttributionResult]:
    def predict(ticker: str, trade_date: str) -> AttributionResult:
        initial_state = {
            "ticker": ticker,
            "trade_date": trade_date,
            "query": None,
            "price_move_pct": None,
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
            "model_id": model_id,
        }
        result = graph.invoke(initial_state)
        return AttributionResult(
            ticker=result["ticker"],
            trade_date=result["trade_date"],
            causes=[
                PredictedCause(
                    text=cause.get("text", ""),
                    category=cause.get("category", "unknown"),
                    confidence=cause.get("confidence", 0.0),
                    evidence_ids=cause.get("evidence_ids", []),
                    direction=cause.get("direction", "unknown"),
                )
                for cause in result.get("causes", [])
            ],
            summary=result.get("summary_md", ""),
            retrieved_evidence=[
                RetrievedEvidence(
                    asset_id=chunk.get("asset_id", ""),
                    content_md=chunk.get("content_md", ""),
                    source_type=chunk.get("source_type", ""),
                    rrf_score=chunk.get("rrf_score", 0.0),
                )
                for chunk in result.get("reranked_chunks", [])
            ],
            cost_breakdown=result.get("cost_breakdown", []),
            total_cost_usd=result.get("total_cost_usd", 0.0),
            total_tokens=result.get("total_tokens", 0),
        )

    return predict


def make_rag_only_predict(
    graph: Any,
    model_id: str = "claude-sonnet-4-20250514",
) -> Callable[[str, str], AttributionResult]:
    return make_catalyst_predict(graph, model_id=model_id)
