"""Miner node — deterministic retrieval, no LLM call.

Hybrid search (BM25 + vector) → RRF merge → top-20 → rerank → top-8.

Spec reference: Section 4.3 — Miner Node.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from catalyst_agents.state import AttributionState

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOP_K_RETRIEVAL = 20
TOP_K_RERANKED = 8
DATE_WINDOW_DAYS = 3


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _compute_date_range(trade_date: str, window_days: int = DATE_WINDOW_DAYS) -> tuple[str, str]:
    """Return (start_date, end_date) for a ±window_days window around trade_date.

    Args:
        trade_date:   ISO date string "YYYY-MM-DD".
        window_days:  Symmetric window size in calendar days.

    Returns:
        Tuple of ISO strings (start_date, end_date), both inclusive.
    """
    dt = datetime.strptime(trade_date, "%Y-%m-%d")
    start = (dt - timedelta(days=window_days)).strftime("%Y-%m-%d")
    end = (dt + timedelta(days=window_days)).strftime("%Y-%m-%d")
    return start, end


def _build_query(state: AttributionState) -> str:
    """Derive a search query from state.

    Uses the NLP query when available; otherwise falls back to a
    template query built from ticker + trade_date.

    Args:
        state: Current attribution state.

    Returns:
        Non-empty query string suitable for hybrid_search.
    """
    nlp_query = state.get("query")
    if nlp_query:  # truthy check — rejects None and ""
        return nlp_query
    return f"Why did {state['ticker']} move on {state['trade_date']}?"


# ---------------------------------------------------------------------------
# Public node
# ---------------------------------------------------------------------------

def miner(
    state: AttributionState,
    *,
    table: Any = None,
    embedding_fn: Any = None,
    reranker: Any = None,
) -> dict:
    """Miner node for the LangGraph attribution pipeline.

    Performs deterministic retrieval — no LLM call:
      1. Hybrid search (BM25 + vector, merged with RRF) → top-20.
      2. Cross-encoder reranking → top-8.
      3. Ticker + date ±3-day filtering is handled inside hybrid_search.

    Args:
        state:        Current AttributionState.
        table:        LanceDB table object (injected by graph config or closure).
        embedding_fn: Callable(str) -> list[float] for vector search.
        reranker:     Object exposing .compute_score(pairs) matching the
                      FlagEmbedding FlagReranker API. If None, falls back to
                      RRF ordering for graceful degradation.

    Returns:
        Partial state dict with:
          - retrieved_chunks: top-20 results from hybrid_search.
          - reranked_chunks:  top-8 results after cross-encoder reranking
                              (or top-8 by RRF score when no reranker).
    """
    # deferred imports for testability
    from catalyst_data.storage.lancedb_store import hybrid_search, _apply_reranker

    query = _build_query(state)
    date_range = _compute_date_range(state["trade_date"])

    # Step 1: Hybrid retrieval → top-20 (RRF)
    all_retrieved: list[dict] = hybrid_search(
        table=table,
        query=query,
        ticker=state["ticker"],
        date_range=date_range,
        top_k=TOP_K_RETRIEVAL,
        embedding_fn=embedding_fn,
    )

    # Step 2: Rerank with cross-encoder → top-8 (graceful fallback if no reranker)
    if reranker and all_retrieved:
        reranked = _apply_reranker(
            chunks=list(all_retrieved),  # copy to avoid mutating retrieved_chunks
            query=query,
            reranker=reranker,
            top_k=TOP_K_RERANKED,
        )
    else:
        reranked = all_retrieved[:TOP_K_RERANKED]

    return {
        "retrieved_chunks": all_retrieved,
        "reranked_chunks": reranked,
    }
