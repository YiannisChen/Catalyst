"""Shared literal fixtures for post-import four-arm runner tests.

Retrieval result fixtures use literal chunk IDs and pre-computed expected
orders; no production fusion/pool/hash helpers are used to derive expected
values in the tests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_REVISION, BGE_RERANKER_REVISION
from catalyst_data.retrieval.result import (
    RetrievalFilters,
    RetrievalResult,
    RetrievalResultSet,
)

MANIFEST_A = "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc"
MANIFEST_B = "b" * 64
INDEX_MANIFEST = "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083"
FIXED_TIME = "2026-01-02T00:00:00Z"
CUTOFF = "2026-01-15T21:00:00Z"

# Literal four-arm fixture (independent oracle): fts5 [a,b,c], dense [b,d],
# hybrid [d,a,e], reranked [e,f]. Expected union is the literal tuple below.
LITERAL_FTS5 = ("a", "b", "c")
LITERAL_DENSE = ("b", "d")
LITERAL_HYBRID = ("d", "a", "e")
LITERAL_RERANKED = ("e", "f")
EXPECTED_UNION = ("a", "b", "c", "d", "e", "f")


def normalized_vector(seed: int, dimension: int = BGE_M3_DIMENSION) -> np.ndarray:
    values = np.arange(seed, seed + dimension, dtype=np.float32)
    return values / np.linalg.norm(values)


def make_result(chunk_id: str, *, ticker: str = "AAPL", available_at: str = "2025-01-01T00:00:00Z",
                mode_requested: str = "lexical", mode_served: str = "fts5",
                lexical_rank: int | None = None, dense_rank: int | None = None,
                fusion_rank: int | None = None, reranker_rank: int | None = None,
                lexical_raw_score: float | None = None, dense_score: float | None = None,
                fusion_score: float | None = None, reranker_score: float | None = None,
                index_manifest_id: str | None = INDEX_MANIFEST) -> RetrievalResult:
    filters = RetrievalFilters(
        ticker=ticker, requested_manifest_id=MANIFEST_A, cutoff=CUTOFF,
    )
    return RetrievalResult(
        chunk_id=chunk_id,
        document_id=f"doc:{chunk_id}",
        available_at=available_at,
        cutoff=CUTOFF,
        content_text="AAPL earnings guidance revenue",
        filters_applied=filters,
        source_class="reported_news",
        lexical_raw_score=lexical_raw_score,
        lexical_rank=lexical_rank,
        dense_score=dense_score,
        dense_rank=dense_rank,
        fusion_score=fusion_score,
        fusion_rank=fusion_rank,
        arm_ranks=(("lexical", lexical_rank),) if lexical_rank else (),
        arm_scores=(("lexical", lexical_raw_score),) if lexical_raw_score is not None else (),
        reranker_score=reranker_score,
        reranker_rank=reranker_rank,
        corpus_manifest_id=MANIFEST_A,
        index_manifest_id=index_manifest_id,
        mode_requested=mode_requested,
        mode_served=mode_served,
        is_degraded=(
                mode_served
                != {"lexical": "fts5", "dense": "dense", "hybrid": "hybrid", "reranked": "reranked"}[mode_requested]
            ),
        fallback_reason=None,
        timing_ms=1.0,
        ticker_associations=(ticker,),
    )


def make_result_set(mode_requested: str, mode_served: str, ids: tuple[str, ...],
                    *, ticker: str = "AAPL") -> RetrievalResultSet:
    results = tuple(
        make_result(
            chunk_id, ticker=ticker, mode_requested=mode_requested,
            mode_served=mode_served,
            lexical_rank=idx if mode_requested == "lexical" else None,
            dense_rank=idx if mode_requested == "dense" else None,
            fusion_rank=idx if mode_requested == "hybrid" else None,
            reranker_rank=idx if mode_requested == "reranked" else None,
        )
        for idx, chunk_id in enumerate(ids, start=1)
    )
    return RetrievalResultSet(
        candidates=results, results=results, candidate_count=len(results),
        mode_requested=mode_requested, mode_served=mode_served,
        is_degraded=(
                mode_served
                != {"lexical": "fts5", "dense": "dense", "hybrid": "hybrid", "reranked": "reranked"}[mode_requested]
            ),
    )


class MockQueryEmbedder:
    """Deterministic 1024-d L2-normalized embedder for mock_unit_test runs."""

    is_mock = True
    dimension = BGE_M3_DIMENSION
    model_revision = BGE_M3_REVISION

    def embed_query(self, query: str) -> np.ndarray:
        return normalized_vector(len(query))


class RecordingReranker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, candidates: list[Any]) -> list[float]:
        self.calls.append((query, [c.chunk_id for c in candidates]))
        return [float(100 - i) for i in range(len(candidates))]


def fresh_runner_db(tmp_path: Path, *, suffix: str = "runner") -> sqlite3.Connection:
    """Full-migration SQLite DB with manifest + legacy corpus chunks + FTS."""
    from catalyst_data.migrations import run_migrations
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.retrieval.fts5_builder import build_fts5_index

    conn = sqlite3.connect(tmp_path / f"{suffix}.db")
    init_db(conn)
    run_migrations(conn)
    from catalyst_data.corpus.manifest import publish_manifest
    publish_manifest(
        conn,
        manifest_id=MANIFEST_A,
        manifest_json=json.dumps({"manifest_id": MANIFEST_A}),
        created_at="2026-01-01T00:00:00Z",
    )
    rows = []
    for chunk_id in ("a", "b", "c", "d", "e", "f"):
        rows.append((
            chunk_id, f"doc:{chunk_id}", "news_v2", "body", "0001",
            "AAPL earnings guidance revenue", hashlib.sha256(b"text").hexdigest(),
            hashlib.sha256(b"meta").hexdigest(), "reported_news",
            "2026-01-01T00:00:00Z", json.dumps(["AAPL"]), "eligible", MANIFEST_A, "active",
            "paragraph", 0, 10, 0, 0, 0, 0,
            FIXED_TIME, FIXED_TIME,
        ))
    conn.executemany(
        """INSERT INTO corpus_chunks (
             chunk_id, document_id, chunk_profile_version, section_key, ordinal,
             content_text, content_hash, metadata_hash, source_class,
             available_at, ticker_associations, eligibility, manifest_id, status,
             boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
             prefix_token_count, prefix_truncated, section_parse_degraded,
             created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    build_fts5_index(conn, MANIFEST_A, clock=lambda: FIXED_TIME)
    return conn


def fresh_lance_table(tmp_path: Path, *, name: str = "vectors") -> Any:
    """Real LanceDB table with fixture vectors for the six literal chunk ids."""
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    if name in db.list_tables().tables:
        return db.open_table(name)
    rows = []
    for idx, chunk_id in enumerate(("a", "b", "c", "d", "e", "f"), start=1):
        rows.append({
            "chunk_id": chunk_id,
            "document_id": f"doc:{chunk_id}",
            "content_text": "AAPL earnings guidance revenue",
            "content_hash": hashlib.sha256(b"text").hexdigest(),
            "metadata_hash": hashlib.sha256(b"meta").hexdigest(),
            "available_at": "2026-01-01T00:00:00Z",
            "ticker_associations": ["AAPL"],
            "source_class": "reported_news",
            "chunk_profile_version": "news_v2",
            "status": "active",
            "eligibility": "eligible",
            "dedup_cluster_id": None,
            "cluster_first_available_at": None,
            "representative_document_id": None,
            "corpus_manifest_id": MANIFEST_A,
            "source_bundle_id": "b" * 64,
            "snapshot_id": "s" * 64,
            "probe_report_id": "p" * 64,
            "postbuild_readiness_id": "r" * 64,
            "index_manifest_id": INDEX_MANIFEST,
            "vector": normalized_vector(idx).tolist(),
        })
    return db.create_table(name, data=rows)


def make_hybrid_result(
    *,
    mode_requested: str = "reranked",
    mode_served: str = "reranked",
    lexical_ids: tuple[str, ...] = LITERAL_FTS5,
    dense_ids: tuple[str, ...] | None = LITERAL_DENSE,
    fusion_ids: tuple[str, ...] = LITERAL_HYBRID,
    reranked_ids: tuple[str, ...] | None = LITERAL_RERANKED,
    degradation_reasons: tuple[str, ...] = (),
    lexical_override: Any | None = None,
    dense_override: Any | None = None,
    ticker: str = "AAPL",
) -> Any:
    """Construct a real HybridRetrievalResult with literal fixture arms."""
    from catalyst_data.retrieval.hybrid import HybridRetrievalResult

    lexical = lexical_override if lexical_override is not None else make_result_set("lexical", "fts5", lexical_ids, ticker=ticker)
    dense = dense_override if dense_override is not None else (
        None if dense_ids is None else make_result_set("dense", "dense", dense_ids, ticker=ticker)
    )
    fusion = tuple(make_result_set("hybrid", "hybrid", fusion_ids, ticker=ticker).results)
    if mode_served == "hybrid":
        reranked_set = None
        final = fusion
    elif mode_served == "fts5":
        # Surviving-arm fallback: final_results equal the lexical survivor.
        reranked_set = None
        final = tuple(lexical.results)
    elif reranked_ids is None:
        reranked_set = None
        final = ()
    else:
        reranked_set = make_result_set("reranked", "reranked", reranked_ids, ticker=ticker)
        final = tuple(reranked_set.results)
    return HybridRetrievalResult(
        mode_requested=mode_requested,
        mode_served=mode_served,
        lexical_results=lexical,
        dense_results=dense,
        fusion_results=fusion,
        reranker_results=reranked_set,
        final_results=final,
        degradation_reasons=degradation_reasons,
    )
