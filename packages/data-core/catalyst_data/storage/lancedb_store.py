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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (Section 6 spec)
# ---------------------------------------------------------------------------

RRF_K: int = 60
DEFAULT_TOP_K: int = 20
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
    # Accumulate scores and preserve the first-seen metadata for each asset_id.
    scores: dict[str, float] = {}
    metadata: dict[str, dict[str, Any]] = {}

    for ranked_list in result_lists:
        for rank_0based, item in enumerate(ranked_list):
            aid = item["asset_id"]
            rank_1based = rank_0based + 1
            scores[aid] = scores.get(aid, 0.0) + 1.0 / (k + rank_1based)
            # Keep metadata from first occurrence; subsequent dupes just add score.
            if aid not in metadata:
                metadata[aid] = {key: val for key, val in item.items() if key != "rrf_score"}

    merged: list[dict[str, Any]] = []
    for aid, score in scores.items():
        entry = dict(metadata[aid])
        entry["rrf_score"] = score
        merged.append(entry)

    merged.sort(key=lambda x: x["rrf_score"], reverse=True)
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
        reranker = CrossEncoder(model_name)
        return reranker
    except Exception as exc:
        logger.warning("Failed to load reranker %s: %s", model_name, exc)
        return None


def _apply_reranker(
    chunks: list[dict[str, Any]],
    query: str,
    reranker: Any,
    top_k: int = DEFAULT_RERANK_TOP_K,
) -> list[dict[str, Any]]:
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

    pairs = [(query, chunk["content_md"]) for chunk in chunks]

    # Support both CrossEncoder.predict() and FlagReranker.compute_score()
    if hasattr(reranker, "predict"):
        scores = reranker.predict(pairs)
    elif hasattr(reranker, "compute_score"):
        scores = reranker.compute_score(pairs)
    else:
        logger.warning("Reranker has no predict() or compute_score() method; skipping")
        return chunks[:top_k]

    # Normalise scalar → single-element list
    if isinstance(scores, (int, float)):
        scores = [scores]

    # Convert numpy arrays to plain list for uniform iteration
    if hasattr(scores, "tolist"):
        scores = scores.tolist()

    for chunk, score in zip(chunks, scores):
        chunk["rerank_score"] = float(score)

    chunks.sort(key=lambda c: c.get("rerank_score", 0.0), reverse=True)
    return chunks[:top_k]


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
    try:
        import lancedb  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "lancedb is required for build_index. "
            "Install it with: pip install 'catalyst-data[vector]'"
        ) from exc

    try:
        from FlagEmbedding import BGEM3FlagModel  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "FlagEmbedding is required for build_index. "
            "Install it with: pip install 'catalyst-data[vector]'"
        ) from exc

    db_path = Path(db_path)
    lancedb_path = Path(lancedb_path)
    lancedb_path.mkdir(parents=True, exist_ok=True)

    # --- Fetch Silver rows ---
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(_SILVER_QUERY).fetchall()
    finally:
        conn.close()

    if not rows:
        return 0

    tokenizer = _load_punkt_tab_tokenizer()
    records = _build_chunk_records(rows, tokenizer=tokenizer)
    texts = [record["content_md"] for record in records]

    # --- Embed all L1+L2 texts with bge-m3 ---
    model = BGEM3FlagModel(embedding_model, use_fp16=True)
    # encode returns a dict; "dense_vecs" is the 1024-dim dense embedding
    encoded = model.encode(texts, batch_size=32, max_length=8192)
    vectors = encoded["dense_vecs"].tolist()

    for idx, vector in enumerate(vectors):
        records[idx]["vector"] = vector

    # --- Upsert into LanceDB (overwrite table for idempotent re-indexing) ---
    db = lancedb.connect(str(lancedb_path))

    if _TABLE_NAME in db.table_names():
        db.drop_table(_TABLE_NAME)

    table = db.create_table(_TABLE_NAME, data=records)

    # Create FTS index on content_md for BM25 path
    table.create_fts_index("content_md", replace=True)

    return len(records)


# ---------------------------------------------------------------------------
# hybrid_search: BM25 + vector → RRF
# ---------------------------------------------------------------------------


def hybrid_search(
    table: Any,
    query: str,
    ticker: str | None = None,
    date_range: tuple[str, str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    embedding_fn: Any = None,
) -> list[dict[str, Any]]:
    """BM25 + vector search in parallel, merged with RRF.

    Reranking is NOT applied here — per ADR-002, cross-encoder reranking
    is the Miner node's responsibility (Section 4.3). This function only
    handles retrieval and RRF fusion.

    Spec reference: Section 6.1 — hybrid retrieval paths.

    Args:
        table:        LanceDB table object (already opened).
        query:        Natural-language search query.
        ticker:       Optional ticker filter (SQL WHERE clause).
        date_range:   Optional (start_date, end_date) ISO strings (inclusive).
        top_k:        Number of results to return after RRF merge.
        embedding_fn: Callable(str) -> list[float]. Injected for testability;
                      if None, lancedb_store attempts to load bge-m3 internally
                      (requires FlagEmbedding installed).

    Returns:
        List of dicts with keys: asset_id, ticker, source_type,
        reference_date, content_md, rrf_score. Length <= top_k.
    """
    # Resolve embedding function if not injected
    if embedding_fn is None:
        try:
            from FlagEmbedding import BGEM3FlagModel  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "FlagEmbedding is required when embedding_fn is not provided. "
                "Install it with: pip install 'catalyst-data[vector]'"
            ) from exc
        _model = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)
        embedding_fn = lambda text: _model.encode([text])["dense_vecs"][0].tolist()

    query_vector = embedding_fn(query)

    # Build optional WHERE filter expression
    where_parts: list[str] = []
    if ticker is not None:
        safe_ticker = ticker.replace("'", "''")
        where_parts.append(f"ticker = '{safe_ticker}'")
    if date_range is not None:
        start, end = date_range
        safe_start = start.replace("'", "''")
        safe_end = end.replace("'", "''")
        where_parts.append(f"reference_date >= '{safe_start}'")
        where_parts.append(f"reference_date <= '{safe_end}'")
    where_clause = " AND ".join(where_parts) if where_parts else None

    # --- Vector path ---
    vector_query = table.search(query_vector, query_type="vector").limit(top_k)
    if where_clause:
        vector_query = vector_query.where(where_clause, prefilter=True)
    vector_df = vector_query.to_pandas()

    # --- BM25 full-text path ---
    fts_query = table.search(query, query_type="fts").limit(top_k)
    if where_clause:
        fts_query = fts_query.where(where_clause, prefilter=True)
    fts_df = fts_query.to_pandas()

    # Convert DataFrames to list[dict], keeping only required fields
    _keep = {
        "asset_id",
        "parent_asset_id",
        "chunk_level",
        "sentence_index",
        "ticker",
        "source_type",
        "reference_date",
        "content_md",
    }

    def _df_to_dicts(df: Any) -> list[dict[str, Any]]:
        results = []
        for _, row in df.iterrows():
            results.append({k: row[k] for k in _keep if k in row})
        return results

    vector_results = _df_to_dicts(vector_df)
    fts_results = _df_to_dicts(fts_df)

    merged = reciprocal_rank_fusion(vector_results, fts_results, k=RRF_K)
    available_rrf_candidates = len(merged)
    effective_top_k = min(top_k, available_rrf_candidates)
    if effective_top_k < top_k:
        logger.warning(
            "RRF guardrail fallback: requested top_k=%d but only %d candidates available; using effective_top_k=%d",
            top_k,
            available_rrf_candidates,
            effective_top_k,
        )
    return merged[:effective_top_k]
