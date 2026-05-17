"""Miner node — deterministic retrieval, no LLM call.

Hybrid search (BM25 + vector) → RRF merge → top-20 → rerank → top-8.

Spec reference: Section 4.3 — Miner Node.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
import sqlite3
from typing import Any

from catalyst_agents.retrieval.policy import Layer, RetrievalMetadata, retrieve
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


def _extract_query_ticker(query_text: str | None, known_tickers: set[str]) -> str | None:
    if not query_text:
        return None
    ticker_alias = {
        "APPLE": "AAPL",
        "MICROSOFT": "MSFT",
        "TESLA": "TSLA",
        "GOOGLE": "GOOGL",
        "ALPHABET": "GOOGL",
        "NVIDIA": "NVDA",
        "META": "META",
        "AMAZON": "AMZN",
        "JPMORGAN": "JPM",
    }
    tokens = re.findall(r"\b[A-Za-z]{1,12}\b", query_text)
    upper_tokens = [t.upper() for t in tokens]

    for token in upper_tokens:
        if token in known_tickers:
            return token
    for token in upper_tokens:
        mapped = ticker_alias.get(token)
        if mapped and mapped in known_tickers:
            return mapped
    return None


def _is_ticker_consistent(query_ticker_raw: str | None, resolved_ticker: str) -> bool | None:
    if query_ticker_raw is None:
        return None
    return query_ticker_raw == resolved_ticker


def _resolve_layer(state: AttributionState) -> Layer:
    raw = state.get("current_layer")
    if raw is None:
        return Layer.DIRECT
    return raw if isinstance(raw, Layer) else Layer(raw)


def _build_retrieval_metadata(
    state: AttributionState,
    *,
    table: Any,
    embedding_fn: Any,
    date_range: tuple[str, str],
) -> RetrievalMetadata:
    existing = state.get("retrieval_metadata")
    if isinstance(existing, RetrievalMetadata):
        existing.ticker = state["ticker"]
        existing.trade_date = state["trade_date"]
        existing.date_range = date_range
        existing.table = table
        existing.embedding_fn = embedding_fn
        existing.top_k = TOP_K_RETRIEVAL
        return existing

    return RetrievalMetadata(
        ticker=state["ticker"],
        trade_date=state["trade_date"],
        date_range=date_range,
        db_path=None,
        table=table,
        embedding_fn=embedding_fn,
        top_k=TOP_K_RETRIEVAL,
    )


def _merge_unique_chunks(*chunk_groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for group in chunk_groups:
        for chunk in group:
            asset_id = chunk.get("asset_id")
            if asset_id and asset_id in seen:
                continue
            if asset_id:
                seen.add(asset_id)
            merged.append(chunk)
    return merged


def _resolve_db_path(state: AttributionState) -> str | None:
    existing = state.get("retrieval_metadata")
    if isinstance(existing, RetrievalMetadata) and existing.db_path is not None:
        return str(existing.db_path)
    if isinstance(existing, dict):
        db_path = existing.get("db_path")
        if isinstance(db_path, str) and db_path:
            return db_path
    db_path = state.get("db_path")
    return db_path if isinstance(db_path, str) and db_path else None


def _has_ohlcv_session(ticker: str, trade_date: str, db_path: str | None) -> bool:
    if not db_path:
        return False
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM ohlcv WHERE symbol = ? AND date = ? LIMIT 1",
            (ticker, trade_date),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _extract_query_claimed_pct(query_text: str | None) -> float | None:
    if not query_text:
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", query_text)
    return float(match.group(1)) if match else None


def _check_magnitude_plausible(actual_pct: float, claimed_pct: float, tolerance: float = 10.0) -> bool:
    return abs(claimed_pct - actual_pct) <= tolerance


def _get_ohlcv_move_pct(ticker: str, trade_date: str, db_path: str | None) -> float | None:
    if not db_path:
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT open, close FROM ohlcv WHERE symbol = ? AND date = ? LIMIT 1",
            (ticker, trade_date),
        ).fetchone()
        if not row:
            return None
        open_px, close_px = row
        if open_px in (None, 0) or close_px is None:
            return None
        return ((float(close_px) - float(open_px)) / float(open_px)) * 100.0
    finally:
        conn.close()


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
    from catalyst_data.storage.lancedb_store import _apply_reranker

    query = _build_query(state)
    known_tickers = {
        state["ticker"],
        "AAPL",
        "MSFT",
        "TSLA",
        "GOOGL",
        "NVDA",
        "META",
        "AMZN",
        "JPM",
        "MRNA",
    }
    provided_raw = state.get("query_ticker_raw")
    if provided_raw:
        query_ticker_raw = str(provided_raw).upper()
    else:
        query_ticker_raw = _extract_query_ticker(query, known_tickers)
    ticker_consistent = _is_ticker_consistent(query_ticker_raw, state["ticker"])
    layer = _resolve_layer(state)
    db_path = _resolve_db_path(state)
    claimed_pct = _extract_query_claimed_pct(query)
    market_session_valid: bool | None
    if db_path is None:
        market_session_valid = None
    else:
        try:
            market_session_valid = _has_ohlcv_session(state["ticker"], state["trade_date"], db_path)
        except Exception:
            market_session_valid = None
    magnitude_plausible: bool | None = None
    if claimed_pct is not None and db_path is not None:
        try:
            actual_pct = _get_ohlcv_move_pct(state["ticker"], state["trade_date"], db_path)
            if actual_pct is not None:
                magnitude_plausible = _check_magnitude_plausible(actual_pct=actual_pct, claimed_pct=claimed_pct)
        except Exception:
            magnitude_plausible = None

    if market_session_valid is False:
        return {
            "query_ticker_raw": query_ticker_raw,
            "ticker_consistent": ticker_consistent,
            "market_session_valid": False,
            "magnitude_plausible": magnitude_plausible,
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "current_layer": layer,
        }

    date_range = _compute_date_range(state["trade_date"])
    metadata = _build_retrieval_metadata(
        state,
        table=table,
        embedding_fn=embedding_fn,
        date_range=date_range,
    )

    retrieved_for_layer: list[dict] = retrieve(
        query,
        layer,
        metadata,
        rerank=None,
    )
    previous_chunks = state.get("retrieved_chunks", []) if layer == Layer.MACRO else []
    all_retrieved = _merge_unique_chunks(previous_chunks, retrieved_for_layer)

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
        "query_ticker_raw": query_ticker_raw,
        "ticker_consistent": ticker_consistent,
        "market_session_valid": market_session_valid,
        "magnitude_plausible": magnitude_plausible,
        "retrieved_chunks": all_retrieved,
        "reranked_chunks": reranked,
        "retrieval_metadata": metadata,
        "current_layer": layer,
    }
