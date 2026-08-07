"""Gold layer: LanceDB vector store with hybrid BM25 + vector search and RRF merge.

Medallion layer: Gold (vector index on top of Silver clean_assets).

Design decisions (per ADR-003 / Section 6 spec):
- Each non-duplicate clean_asset becomes exactly one chunk (no sub-chunking needed
  because per-spec, every source type already produces appropriately sized assets).
- Hybrid retrieval: two parallel paths (vector cosine + BM25 full-text), merged
  via Reciprocal Rank Fusion (RRF, k=60).
- Reranking (cross-encoder bge-reranker-v2-m3) happens in the Miner node (Task 3.3)
  and is deliberately excluded from this module.
- lancedb and FlagEmbedding are optional deps (extras: vector). Imports are guarded
  inside the functions that require them so RRF and tests work without the heavy deps.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from catalyst_data.config import BGE_RERANKER_REVISION
from catalyst_data.retrieval.result import RetrievalResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (Section 6 spec)
# ---------------------------------------------------------------------------

RRF_K: int = 60
DEFAULT_TOP_K: int = 12
DEFAULT_RERANK_TOP_K: int = 8
EMBEDDING_MODEL: str = "BAAI/bge-m3"
RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"
L2_ELIGIBLE_SOURCE_TYPES: tuple[str, ...] = ("polygon_news",)
L2_MAX_SENTENCES_PER_ASSET: int = 30
L2_MIN_SENTENCE_LENGTH: int = 12

# LanceDB table name for the Gold layer
_TABLE_NAME = "chunks"

# SQLite query: all non-duplicate Silver assets
_SILVER_QUERY = """
    SELECT asset_id, ticker, source_type, reference_date, content_md
    FROM clean_assets
    WHERE is_duplicate = 0
"""

_L1_CHUNK_LEVEL = "l1"
_L2_CHUNK_LEVEL = "l2"
_EMBED_BATCH_SIZE = 32


# ---------------------------------------------------------------------------
# Pure helper: Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    *result_lists: list[dict[str, Any]],
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """Merge multiple ranked result lists using Reciprocal Rank Fusion.

    Formula (1-indexed ranks):
        RRF_score(d) = sum_over_lists( 1 / (k + rank(d, list)) )

    Args:
        *result_lists: Any number of ranked lists. Each element is a dict
                       that must contain an 'asset_id' key used as identity.
        k: RRF smoothing constant (default 60, per spec Section 6.1).

    Returns:
        Single merged list, sorted by rrf_score descending, with each
        asset_id appearing exactly once. The 'rrf_score' key is added (or
        overwritten) in each returned dict.
    """
    if not result_lists:
        return []
    from catalyst_data.retrieval.fusion import reciprocal_rank_score

    records: dict[str, dict[str, Any]] = {}
    index_identity_set = False
    index_identity = None
    for arm_index, ranked_list in enumerate(result_lists, start=1):
        arm_name = "lexical" if arm_index == 1 else "dense" if arm_index == 2 else f"arm_{arm_index}"
        for position, item in enumerate(ranked_list, start=1):
            asset_id = item["asset_id"]
            item_index_identity = item.get("index_manifest_id")
            if not index_identity_set:
                index_identity = item_index_identity
                index_identity_set = True
            elif item_index_identity != index_identity:
                raise ValueError("legacy RRF arms must use one index manifest identity")
            record = records.setdefault(
                asset_id,
                {key: value for key, value in item.items() if key != "rrf_score"},
            )
            ranks = record.setdefault("arm_ranks", {})
            ranks[arm_name] = min(ranks.get(arm_name, position), position)
            scores = record.setdefault("arm_scores", {})
            score_key = "lexical_raw_score" if arm_name == "lexical" else "dense_score" if arm_name == "dense" else "fusion_score"
            scores[arm_name] = item.get(score_key)
            record.setdefault("_ranks", []).append(position)
    merged = sorted(
        records.values(),
        key=lambda record: (-reciprocal_rank_score(record["_ranks"], k=k), min(record["_ranks"]), record["asset_id"]),
    )
    for record in merged:
        record["rrf_score"] = reciprocal_rank_score(record.pop("_ranks"), k=k)
    return merged


# ---------------------------------------------------------------------------
# Reranker: cross-encoder for relevance scoring (ADR-002 Step 3)
# ---------------------------------------------------------------------------


def load_reranker(model_name: str = RERANKER_MODEL) -> Any | None:
    """Load a cross-encoder reranker model.

    Tries sentence_transformers.CrossEncoder first. Returns None with a
    warning if the model cannot be loaded (missing deps, CPU/memory issues).

    Args:
        model_name: HuggingFace model ID for the cross-encoder.

    Returns:
        A CrossEncoder instance, or None if loading fails.
    """
    try:
        from sentence_transformers import CrossEncoder
    except ImportError:
        logger.warning(
            "sentence-transformers not installed; reranker unavailable. "
            "Install with: pip install sentence-transformers"
        )
        return None

    try:
        if model_name != RERANKER_MODEL:
            raise ValueError("reranker model must use the pinned BGE reranker identity")
        reranker = CrossEncoder(model_name, revision=BGE_RERANKER_REVISION, device="cuda")
        return reranker
    except Exception as exc:
        logger.warning("Failed to load reranker %s: %s", model_name, exc)
        return None


def _apply_reranker(
    chunks: list[RetrievalResult],
    query: str,
    reranker: Any,
    top_k: int = DEFAULT_RERANK_TOP_K,
    timeout_seconds: float = 2.0,
) -> list[RetrievalResult]:
    """Score chunks with a cross-encoder, attach rerank_score, return top_k.

    Supports two reranker interfaces:
      - CrossEncoder with .predict(pairs) -> list[float]
      - FlagReranker with .compute_score(pairs) -> list[float] | float

    Args:
        chunks:   RRF-fused retrieval results to rerank.
        query:    The search query.
        reranker: Cross-encoder model instance.
        top_k:    Maximum number of chunks to return.

    Returns:
        Top-k chunks sorted by rerank_score descending.
    """
    if not chunks:
        return []
    from catalyst_data.retrieval.reranker import rerank

    result = rerank(
        query=query,
        candidates=chunks,
        reranker=reranker,
        timeout_seconds=timeout_seconds,
        display_top_k=top_k,
    )
    return list(result.results)


# ---------------------------------------------------------------------------
# L2 chunking helpers: nltk punkt_tab splitter
# ---------------------------------------------------------------------------


def _load_punkt_tab_tokenizer() -> Any:
    try:
        from nltk.tokenize.punkt import PunktTokenizer
    except ImportError as exc:
        raise ImportError(
            "nltk is required for L2 sentence splitting. "
            "Install it with: pip install 'catalyst-data[vector]'"
        ) from exc

    try:
        return PunktTokenizer("english")
    except LookupError as exc:
        raise ImportError(
            "NLTK punkt_tab resource is required for L2 sentence splitting. "
            "Run: python -c \"import nltk; nltk.download('punkt_tab')\""
        ) from exc


def _stable_l2_asset_id(parent_asset_id: str, sentence_index: int) -> str:
    return f"{parent_asset_id}::l2s{sentence_index:04d}"


def _split_l2_sentences(
    content_md: str,
    *,
    tokenizer: Any,
    max_sentences_per_asset: int = L2_MAX_SENTENCES_PER_ASSET,
    min_sentence_length: int = L2_MIN_SENTENCE_LENGTH,
) -> list[str]:
    if not content_md:
        return []

    raw_sentences = tokenizer.tokenize(content_md)
    filtered = [s.strip() for s in raw_sentences if s and s.strip() and len(s.strip()) >= min_sentence_length]
    return filtered[:max_sentences_per_asset]


def _build_chunk_records(
    rows: list[tuple[str, str, str, str, str]],
    *,
    tokenizer: Any,
    max_sentences_per_asset: int = L2_MAX_SENTENCES_PER_ASSET,
    min_sentence_length: int = L2_MIN_SENTENCE_LENGTH,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for asset_id, ticker, source_type, reference_date, content_md in rows:
        content_text = content_md or ""
        records.append(
            {
                "asset_id": asset_id,
                "parent_asset_id": asset_id,
                "chunk_level": _L1_CHUNK_LEVEL,
                "sentence_index": 0,
                "ticker": ticker,
                "source_type": source_type,
                "reference_date": reference_date,
                "content_md": content_text,
            }
        )

        if source_type not in L2_ELIGIBLE_SOURCE_TYPES:
            continue

        l2_sentences = _split_l2_sentences(
            content_text,
            tokenizer=tokenizer,
            max_sentences_per_asset=max_sentences_per_asset,
            min_sentence_length=min_sentence_length,
        )
        for sentence_index, sentence in enumerate(l2_sentences, start=1):
            records.append(
                {
                    "asset_id": _stable_l2_asset_id(asset_id, sentence_index),
                    "parent_asset_id": asset_id,
                    "chunk_level": _L2_CHUNK_LEVEL,
                    "sentence_index": sentence_index,
                    "ticker": ticker,
                    "source_type": source_type,
                    "reference_date": reference_date,
                    "content_md": sentence,
                }
            )
    return records


def _encode_dense_vectors(
    model: Any,
    texts: list[str],
    *,
    batch_size: int = _EMBED_BATCH_SIZE,
    max_length: int = 8192,
) -> list[list[float]]:
    """Encode texts in chunks to avoid large-batch tokenizer edge cases."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        chunk = texts[start : start + batch_size]
        encoded = model.encode(chunk, batch_size=min(batch_size, len(chunk)), max_length=max_length)
        dense = encoded["dense_vecs"].tolist()
        vectors.extend(dense)
    return vectors


# ---------------------------------------------------------------------------
# build_index: Silver → Gold
# ---------------------------------------------------------------------------


def build_index(
    db_path: str | Path,
    lancedb_path: str | Path,
    embedding_model: str = EMBEDDING_MODEL,
) -> int:
    """Read non-duplicate clean_assets from SQLite, embed, and upsert into LanceDB.

    LanceDB table schema ('chunks'):
        asset_id      : str
        ticker        : str
        source_type   : str
        reference_date: str
        content_md    : str   (stored for BM25 full-text search)
        vector        : list[float]  (bge-m3 1024-dim embedding)

    Args:
        db_path:        Path to the SQLite Silver database.
        lancedb_path:   Directory where the LanceDB Gold store lives.
        embedding_model: HuggingFace model ID for FlagEmbedding BGEM3FlagModel.

    Returns:
        Number of chunks indexed.

    Raises:
        ImportError: If lancedb or FlagEmbedding are not installed.
    """
    del db_path, lancedb_path, embedding_model
    raise RuntimeError(
        "legacy build_index is disabled; use the identity-bound B6-L GPU driver "
        "and import_vectors_to_lancedb instead"
    )


# ---------------------------------------------------------------------------
# hybrid_search: BM25 + vector → RRF
# ---------------------------------------------------------------------------


def hybrid_search(
    table: Any,
    query: str,
    *,
    db: Any,
    ticker: str,
    cutoff: str,
    requested_manifest_id: str,
    index_manifest_id: str,
    query_embedding: Any,
    reranker: object | None = None,
    source_classes: tuple[str, ...] | None = None,
    evidence_types: tuple[str, ...] | None = None,
    mode: str = "hybrid",
    reranker_timeout_seconds: float = 2.0,
):
    """Compatibility name that delegates directly to the B6-L facade.

    All identity and scope arguments are required.  The former optional
    embedding/model path was removed so callers cannot bypass LanceDB
    prefilters, IndexManifest binding, or the real reranker timeout.
    """
    from catalyst_data.retrieval.hybrid import retrieve_hybrid

    return retrieve_hybrid(
        db,
        query=query,
        ticker=ticker,
        cutoff=cutoff,
        mode=mode,
        reranker=reranker,
        query_embedding=query_embedding,
        requested_manifest_id=requested_manifest_id,
        index_manifest_id=index_manifest_id,
        lancedb_table=table,
        source_classes=source_classes,
        evidence_types=evidence_types,
        reranker_timeout_seconds=reranker_timeout_seconds,
    )
