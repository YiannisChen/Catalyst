"""
Adapter: wraps the MCJ graph into a predict_fn compatible with the catalyst-eval harness.

Usage:
    predict = make_catalyst_predict(graph)
    result = predict("AAPL", "2026-01-15")  # -> AttributionResult
"""
from __future__ import annotations

from typing import Callable

from catalyst_eval.schema.result import AttributionResult, PredictedCause, RetrievedEvidence
from catalyst_agents.retrieval.policy import Layer


def make_catalyst_predict(
    graph,
    model_id: str = "claude-sonnet-4-20250514",
) -> Callable[[str, str], AttributionResult]:
    """
    Create a predict_fn that wraps the LangGraph MCJ workflow.

    The returned callable conforms to the eval harness contract:
        predict_fn(ticker: str, trade_date: str) -> AttributionResult

    Args:
        graph:    Compiled LangGraph StateGraph (or _SequentialRunner fallback)
                  with a .invoke(state: dict) -> dict interface.
        model_id: Model identifier forwarded via state for cost tracking.

    Returns:
        A callable that accepts (ticker, trade_date) and returns AttributionResult.
    """

    def predict(ticker: str, trade_date: str) -> AttributionResult:
        initial_state: dict = {
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
            "current_layer": Layer.DIRECT,
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
                    text=c.get("text", ""),
                    category=c.get("category", "unknown"),
                    confidence=c.get("confidence", 0.0),
                    evidence_ids=c.get("evidence_ids", []),
                    direction=c.get("direction", "unknown"),
                )
                for c in result.get("causes", [])
            ],
            summary=result.get("summary_md", ""),
            retrieved_evidence=[
                RetrievedEvidence(
                    asset_id=c.get("asset_id", ""),
                    content_md=c.get("content_md", ""),
                    source_type=c.get("source_type", ""),
                    rrf_score=c.get("rrf_score", 0.0),
                )
                for c in result.get("reranked_chunks", [])
            ],
            cost_breakdown=result.get("cost_breakdown", []),
            total_cost_usd=result.get("total_cost_usd", 0.0),
            total_tokens=result.get("total_tokens", 0),
        )

    return predict


def make_rag_only_predict(
    graph,
    model_id: str = "claude-sonnet-4-20250514",
) -> Callable[[str, str], AttributionResult]:
    """Alias for the P1 rag_only pipeline route.

    rag_only reuses the same Miner→Critic→Judge graph output contract as
    make_catalyst_predict while allowing callers to register an explicit
    pipeline-mode name.
    """
    return make_catalyst_predict(graph, model_id=model_id)
