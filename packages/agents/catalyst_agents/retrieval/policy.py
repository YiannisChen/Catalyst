"""Retrieval policy for Layer 1 + Layer 2 with SQL fallback for P0."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import sqlite3
from typing import Any, Literal

from catalyst_data.config import db_path as default_db_path
from catalyst_data.storage.lancedb_store import DEFAULT_RERANK_TOP_K, hybrid_search, _apply_reranker

MAX_LAYERS_P0 = 2
MAX_EXPANSIONS = 2
DEFAULT_TOP_K = 12
DEFAULT_GEO_CORPUS_TIER = 2
DEFAULT_LANCEDB_DIR = Path(__file__).resolve().parents[4] / "data" / "lancedb_gold" / "eval_frozen"

DIRECT_SOURCE_TYPES = (
    "polygon_news",
    "fmp_news",
    "finnhub_company_news",
    "fmp_fundamentals",
    "sec_filing",
)
MACRO_SOURCE_TYPES = (
    "macro_news",
    "market_news",
    "geopolitical_news",
    "fred_rates",
    "fred_macro",
    "gdelt_news",
    "policy_news",
)


class Layer(str, Enum):
    DIRECT = "direct"
    MACRO = "macro"
    RELATED = "related"


@dataclass
class RetrievalMetadata:
    ticker: str
    trade_date: str
    date_range: tuple[str, str]
    db_path: Path | None = None
    top_k: int = DEFAULT_TOP_K
    table: Any = None
    embedding_fn: Any = None
    lancedb_dir: Path = DEFAULT_LANCEDB_DIR
    layers_attempted: list[Layer] = field(default_factory=list)
    expansion_reasons: list[str] = field(default_factory=list)
    stop_reason: Literal[
        "sufficiency_reached",
        "expansions_exhausted",
        "layer3_not_implemented",
        "system_error",
    ] | None = None
    hit_counts_per_layer: dict[Layer, int] = field(default_factory=dict)
    geo_corpus_tier: Literal[2] = DEFAULT_GEO_CORPUS_TIER
    total_unique_evidence: int = 0
    max_layers: int = MAX_LAYERS_P0
    max_expansions: int = MAX_EXPANSIONS
    _seen_asset_ids: set[str] = field(default_factory=set, repr=False)


def _source_types_for_layer(layer: Layer) -> tuple[str, ...]:
    if layer == Layer.DIRECT:
        return DIRECT_SOURCE_TYPES
    if layer == Layer.MACRO:
        return MACRO_SOURCE_TYPES
    raise NotImplementedError("layer3_not_implemented")


def _record_attempt(metadata: RetrievalMetadata, layer: Layer) -> None:
    metadata.layers_attempted.append(layer)
    if not metadata.expansion_reasons:
        metadata.expansion_reasons.append("initial")
    elif layer == Layer.MACRO and "critic_expand_macro" not in metadata.expansion_reasons:
        metadata.expansion_reasons.append("critic_expand_macro")


def _sql_fallback_query(layer: Layer, metadata: RetrievalMetadata) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(metadata.db_path or default_db_path()))
    start, end = metadata.date_range
    source_types = _source_types_for_layer(layer)

    clauses = [
        "reference_date >= ?",
        "reference_date <= ?",
        f"source_type IN ({','.join('?' for _ in source_types)})",
        "COALESCE(is_duplicate, 0) = 0",
    ]
    params: list[Any] = [start, end, *source_types]

    if layer == Layer.DIRECT:
        clauses.insert(0, "ticker = ?")
        params.insert(0, metadata.ticker)

    rows = conn.execute(
        f"""
        SELECT asset_id, ticker, source_type, reference_date, content_md
        FROM clean_assets
        WHERE {' AND '.join(clauses)}
        ORDER BY reference_date DESC, asset_id ASC
        LIMIT ?
        """,
        (*params, metadata.top_k),
    ).fetchall()
    conn.close()

    results = []
    for idx, row in enumerate(rows, start=1):
        results.append(
            {
                "asset_id": row[0],
                "ticker": row[1],
                "source_type": row[2],
                "reference_date": row[3],
                "content_md": row[4],
                "rrf_score": 1.0 / idx,
            }
        )
    return results


def _lancedb_available(metadata: RetrievalMetadata) -> bool:
    return metadata.table is not None and metadata.lancedb_dir.exists()


def _hybrid_path(query: str, layer: Layer, metadata: RetrievalMetadata) -> list[dict[str, Any]]:
    results = hybrid_search(
        table=metadata.table,
        query=query,
        ticker=metadata.ticker if layer == Layer.DIRECT else None,
        date_range=metadata.date_range,
        top_k=metadata.top_k,
        embedding_fn=metadata.embedding_fn,
    )
    allowed_sources = set(_source_types_for_layer(layer))
    return [row for row in results if row.get("source_type") in allowed_sources][: metadata.top_k]


def _update_metadata(metadata: RetrievalMetadata, layer: Layer, results: list[dict[str, Any]]) -> None:
    metadata.hit_counts_per_layer[layer] = len(results)
    metadata._seen_asset_ids.update(row.get("asset_id", "") for row in results if row.get("asset_id"))
    metadata.total_unique_evidence = len(metadata._seen_asset_ids)


def check_sufficiency(
    chunks: list[dict[str, Any]],
    min_count: int = 5,
    min_mean_score: float = 0.02,
) -> bool:
    if len(chunks) < min_count:
        return False

    scores: list[float] = []
    for chunk in chunks:
        raw_score = chunk.get("rrf_score", 0.0)
        try:
            scores.append(float(raw_score))
        except (TypeError, ValueError):
            scores.append(0.0)

    mean_rrf = sum(scores) / len(scores) if scores else 0.0
    return mean_rrf >= min_mean_score


def retrieve(query: str, layer: Layer, metadata: RetrievalMetadata, *, rerank: Any = None) -> list[dict[str, Any]]:
    """Retrieve evidence for the requested layer, falling back to SQL when needed."""
    _record_attempt(metadata, layer)

    if layer == Layer.RELATED:
        metadata.stop_reason = "layer3_not_implemented"
        raise NotImplementedError("layer3_not_implemented")

    results = (
        _hybrid_path(query, layer, metadata)
        if _lancedb_available(metadata)
        else _sql_fallback_query(layer, metadata)
    )
    _update_metadata(metadata, layer, results)
    metadata.stop_reason = "sufficiency_reached" if check_sufficiency(results) else "expansions_exhausted"

    if rerank is not None and results:
        return _apply_reranker(list(results), query, rerank, top_k=DEFAULT_RERANK_TOP_K)
    return results
