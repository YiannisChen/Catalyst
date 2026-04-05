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


def _apply_reranker(
    chunks: list[dict],
    query: str,
    reranker: Any,
    top_k: int,
) -> list[dict]:
    """Score chunks with a cross-encoder, attach rerank_score, and return top_k.

    Handles the edge case where some reranker implementations return a bare
    scalar instead of a list when given a single pair.

    Args:
        chunks:   Retrieval results to rerank.
        query:    The search query (first element of each pair).
        reranker: Object with .compute_score(pairs) -> list[float] | float.
        top_k:    Maximum number of chunks to return.

    Returns:
        Top-k chunks sorted by rerank_score descending, with rerank_score set.
    """
    pairs = [(query, chunk["content_md"]) for chunk in chunks]
    scores = reranker.compute_score(pairs)

    # Normalise scalar → single-element list
    if isinstance(scores, (int, float)):
        scores = [scores]

    for chunk, score in zip(chunks, scores):
        chunk["rerank_score"] = score

    chunks.sort(key=lambda c: c.get("rerank_score", 0.0), reverse=True)
    return chunks[:top_k]


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
    from catalyst_data.storage.lancedb_store import hybrid_search  # deferred import for testability

    query = _build_query(state)
    date_range = _compute_date_range(state["trade_date"])

    # Step 1: Hybrid retrieval → top-20
    retrieved: list[dict] = hybrid_search(
        table=table,
        query=query,
        ticker=state["ticker"],
        date_range=date_range,
        top_k=TOP_K_RETRIEVAL,
        embedding_fn=embedding_fn,
    )

    # Step 2: Rerank with cross-encoder → top-8 (graceful fallback if no reranker)
    if reranker and retrieved:
        reranked = _apply_reranker(
            chunks=retrieved,
            query=query,
            reranker=reranker,
            top_k=TOP_K_RERANKED,
        )
    else:
        reranked = retrieved[:TOP_K_RERANKED]

    return {
        "retrieved_chunks": retrieved,
        "reranked_chunks": reranked,
    }
