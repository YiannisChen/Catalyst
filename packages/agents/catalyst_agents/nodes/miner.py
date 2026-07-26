"""Miner node using injected B5 Retriever and cutoff policy."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from catalyst_agents.retrieval.policy import DEFAULT_CANDIDATE_DEPTH, DEFAULT_TOP_K, Layer, RetrievalMetadata, retrieve
from catalyst_agents.state import AttributionState, OutputStatus, Phase


TOP_K_RETRIEVAL = DEFAULT_CANDIDATE_DEPTH
TOP_K_RERANKED = DEFAULT_TOP_K


def _compute_date_range(trade_date: str, window_days: int = 3) -> tuple[str, str]:
    """Compatibility helper for eval scripts; B5 Miner retrieval does not use date windows."""
    dt = datetime.strptime(trade_date, "%Y-%m-%d")
    return (
        (dt - timedelta(days=window_days)).strftime("%Y-%m-%d"),
        (dt + timedelta(days=window_days)).strftime("%Y-%m-%d"),
    )


def _build_query(state: AttributionState) -> str:
    nlp_query = state.get("query")
    if nlp_query:
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


def _extract_query_claimed_pct(query_text: str | None) -> float | None:
    if not query_text:
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", query_text)
    return float(match.group(1)) if match else None


def _check_magnitude_plausible(actual_pct: float, claimed_pct: float, tolerance: float = 10.0) -> bool:
    return abs(claimed_pct - actual_pct) <= tolerance


def _resolve_layer(state: AttributionState) -> Layer:
    raw = state.get("current_layer")
    if raw is None:
        return Layer.DIRECT
    return raw if isinstance(raw, Layer) else Layer(raw)


def _build_arm_b_evidence(reranked_chunks: list[dict]) -> dict:
    per_asset = {
        chunk.get("asset_id", ""): chunk.get("content_md", "")
        for chunk in reranked_chunks[:TOP_K_RERANKED]
        if chunk.get("asset_id") and chunk.get("content_md")
    }
    if not per_asset:
        return {"per_asset": {}, "sha256": ""}
    block = "\n\n---\n\n".join(f"### Evidence {asset_id}\n\n{content}" for asset_id, content in per_asset.items())
    return {"per_asset": per_asset, "sha256": hashlib.sha256(block.encode("utf-8")).hexdigest()}


def _evidence_to_chunk(item: Any) -> dict[str, Any]:
    chunk_id = getattr(item, "chunk_id")
    available_at = getattr(item, "available_at")
    return {
        "asset_id": chunk_id,
        "chunk_id": chunk_id,
        "document_id": getattr(item, "document_id"),
        "content_md": getattr(item, "content_text"),
        "content_text": getattr(item, "content_text"),
        "available_at": available_at,
        "reference_date": str(available_at)[:10],
        "source_type": getattr(item, "source_class"),
        "source_class": getattr(item, "source_class"),
        "ticker_associations": tuple(getattr(item, "ticker_associations")),
        "dedup_cluster_id": getattr(item, "dedup_cluster_id"),
        "cluster_first_available_at": getattr(item, "cluster_first_available_at"),
        "representative_document_id": getattr(item, "representative_document_id"),
        "is_novel": getattr(item, "is_novel"),
        "lexical_raw_score": getattr(item, "lexical_raw_score"),
        "lexical_rank": getattr(item, "lexical_rank"),
        "rank": getattr(item, "lexical_rank"),
        "rrf_score": getattr(item, "lexical_raw_score") or 0.0,
        "corpus_manifest_id": getattr(item, "corpus_manifest_id"),
        "index_manifest_id": getattr(item, "index_manifest_id"),
        "mode_requested": getattr(item, "mode_requested"),
        "mode_served": getattr(item, "mode_served"),
        "is_degraded": getattr(item, "is_degraded"),
        "fallback_reason": getattr(item, "fallback_reason"),
    }


def miner(
    state: AttributionState,
    *,
    retriever: Any = None,
    cutoff_policy: Any = None,
    requested_manifest_id: str | None = None,
    table: Any = None,
    embedding_fn: Any = None,
    reranker: Any = None,
) -> dict:
    query = _build_query(state)
    known_tickers = {state["ticker"], "AAPL", "MSFT", "TSLA", "GOOGL", "NVDA", "META", "AMZN", "JPM", "MRNA"}
    query_ticker_raw = str(state.get("query_ticker_raw")).upper() if state.get("query_ticker_raw") else _extract_query_ticker(query, known_tickers)
    ticker_consistent = _is_ticker_consistent(query_ticker_raw, state["ticker"])
    if ticker_consistent is False:
        return {
            "query_ticker_raw": query_ticker_raw,
            "ticker_consistent": False,
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "arm_b_evidence": {"per_asset": {}, "sha256": ""},
            "phase": Phase.MINER,
        }
    if state.get("market_session_valid") is False:
        return {
            "output_status": OutputStatus.ABSTAIN,
            "validation_error": "invalid_market_session_context",
            "summary_md": "No valid market-session context is available for attribution.",
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "arm_b_evidence": {"per_asset": {}, "sha256": ""},
            "phase": Phase.MINER,
        }
    if retriever is None or cutoff_policy is None:
        return {
            "error_type": "system_error",
            "output_status": OutputStatus.SYSTEM_ERROR,
            "validation_error": "missing_retrieval_dependency",
            "summary_md": "System error: missing B5 retrieval dependency.",
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "phase": Phase.MINER,
        }

    layer = _resolve_layer(state)
    if layer == Layer.MACRO:
        query = f"{query} macro market sector rates policy context"
    elif layer == Layer.RELATED:
        query = f"{query} peer supplier customer competitor context"
    cutoff = cutoff_policy.compute_cutoff(ticker=state["ticker"], session_date=state["trade_date"], mode="attribution")
    manifest_id = requested_manifest_id or state.get("corpus_manifest_id")
    if not manifest_id:
        return {
            "error_type": "system_error",
            "output_status": OutputStatus.SYSTEM_ERROR,
            "validation_error": "missing_corpus_manifest_identity",
            "summary_md": "System error: missing corpus manifest identity.",
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "phase": Phase.MINER,
        }
    metadata = RetrievalMetadata(
        ticker=state["ticker"],
        trade_date=state["trade_date"],
        cutoff=cutoff,
        requested_manifest_id=manifest_id,
        retriever=retriever,
        top_k=TOP_K_RERANKED,
        candidate_depth=TOP_K_RETRIEVAL,
    )
    try:
        retrieved = retrieve(query, layer, metadata, rerank=None)
    except Exception as exc:
        return {
            "error_type": "system_error",
            "output_status": OutputStatus.SYSTEM_ERROR,
            "validation_error": "retriever_error",
            "summary_md": f"System error during retrieval: {exc}",
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "retrieval_metadata": metadata,
            "cutoff": cutoff,
            "retrieval_cutoff": cutoff,
            "phase": Phase.MINER,
        }
    chunks = [_evidence_to_chunk(item) for item in retrieved]
    reranked = chunks[:TOP_K_RERANKED]
    corpus_ids = sorted({chunk["corpus_manifest_id"] for chunk in chunks if chunk.get("corpus_manifest_id")})
    index_ids = sorted({chunk["index_manifest_id"] for chunk in chunks if chunk.get("index_manifest_id")})
    return {
        "query_ticker_raw": query_ticker_raw,
        "ticker_consistent": ticker_consistent,
        "market_session_valid": True,
        "retrieved_chunks": chunks,
        "reranked_chunks": reranked,
        "arm_b_evidence": _build_arm_b_evidence(reranked),
        "retrieval_metadata": metadata,
        "current_layer": layer,
        "cutoff": cutoff,
        "retrieval_cutoff": cutoff,
        "corpus_manifest_id": corpus_ids[0] if corpus_ids else manifest_id,
        "index_manifest_id": index_ids[0] if index_ids else None,
        "phase": Phase.MINER,
    }
