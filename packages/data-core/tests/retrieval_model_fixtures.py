from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

import numpy as np

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_REVISION, BGE_RERANKER_REVISION
from catalyst_data.retrieval.result import RetrievalFilters, RetrievalResult

MANIFEST_A = "a" * 64
MANIFEST_B = "b" * 64
COMPLETE_FILTERS = {
    "ticker": "AAPL",
    "evidence_types": [],
    "source_classes": [],
    "corpus_manifest_id": MANIFEST_A,
    "index_manifest_id": "f" * 64,
}
PINNED_RETRIEVAL_CONFIG = {
    "lexical_top_k": 20,
    "dense_top_k": 20,
    "fusion_k": 60,
    "fused_top_k": 20,
    "display_top_k": 8,
    "embedding_revision": BGE_M3_REVISION,
    "reranker_revision": BGE_RERANKER_REVISION,
}


def normalized_vector(seed: int, dimension: int = BGE_M3_DIMENSION) -> np.ndarray:
    values = np.arange(seed, seed + dimension, dtype=np.float32)
    return values / np.linalg.norm(values)


@dataclass
class RecordingEmbedder:
    vectors: dict[str, np.ndarray] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def encode(self, texts: list[str]) -> np.ndarray:
        self.calls.extend(texts)
        return np.vstack([
            self.vectors.get(text, normalized_vector(index + 1))
            for index, text in enumerate(texts)
        ])


@dataclass
class RecordingReranker:
    order: list[str] | None = None
    calls: int = 0
    fail: Exception | None = None

    def score(self, query: str, candidates: list[Any]) -> list[float]:
        self.calls += 1
        if self.fail:
            raise self.fail
        order = self.order or [candidate.chunk_id for candidate in candidates]
        ranks = {chunk_id: len(order) - index for index, chunk_id in enumerate(order)}
        return [float(ranks.get(candidate.chunk_id, 0)) for candidate in candidates]


def make_result(chunk_id: str = "c:1", **kwargs: Any) -> RetrievalResult:
    filters = RetrievalFilters(
        ticker=kwargs.pop("ticker", "AAPL"),
        requested_manifest_id=kwargs.pop("requested_manifest_id", MANIFEST_A),
        cutoff=kwargs.pop("cutoff", "2026-01-15T21:00:00Z"),
    )
    return RetrievalResult(
        chunk_id=chunk_id,
        document_id=kwargs.pop("document_id", "doc"),
        available_at=kwargs.pop("available_at", "2026-01-01T00:00:00Z"),
        cutoff=filters.cutoff,
        content_text=kwargs.pop("content_text", "fixture"),
        filters_applied=filters,
        source_class=kwargs.pop("source_class", "reported_news"),
        lexical_raw_score=kwargs.pop("lexical_raw_score", None),
        lexical_rank=kwargs.pop("lexical_rank", None),
        dense_score=kwargs.pop("dense_score", None),
        dense_rank=kwargs.pop("dense_rank", None),
        fusion_score=kwargs.pop("fusion_score", None),
        fusion_rank=kwargs.pop("fusion_rank", None),
        reranker_score=kwargs.pop("reranker_score", None),
        reranker_rank=kwargs.pop("reranker_rank", None),
        corpus_manifest_id=filters.requested_manifest_id,
        index_manifest_id=kwargs.pop("index_manifest_id", "1" * 64),
        mode_requested=kwargs.pop("mode_requested", "lexical"),
        mode_served=kwargs.pop("mode_served", "fts5"),
        is_degraded=kwargs.pop("is_degraded", False),
        fallback_reason=kwargs.pop("fallback_reason", None),
        timing_ms=kwargs.pop("timing_ms", 1.0),
        **kwargs,
    )


def make_results(n: int = 20, prefix: str = "c", **kwargs: Any) -> list[RetrievalResult]:
    return [make_result(f"{prefix}:{index:02d}", **kwargs) for index in range(n)]


def _fresh_db_with_chunks_and_embeddings() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("create table corpus_manifest (manifest_id text primary key)")
    conn.execute("insert into corpus_manifest values (?)", (MANIFEST_A,))
    conn.execute(
        """create table dense_chunks (
            chunk_id text primary key, document_id text, content_text text,
            available_at text, ticker text, source_class text,
            manifest_id text, status text, eligibility text, vector text
        )"""
    )
    rows = []
    for index in range(25):
        vector = normalized_vector(index + 1)
        rows.append((
            f"aapl:{index:02d}", f"doc:{index:02d}", f"text-{index:02d}",
            "2026-01-01T00:00:00Z" if index < 24 else "2026-02-01T00:00:00Z",
            "AAPL" if index < 23 else "MSFT", "reported_news", MANIFEST_A,
            "active", "eligible", json.dumps(vector.tolist()),
        ))
    conn.executemany("insert into dense_chunks values (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def make_arm_artifact(**kwargs: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "artifact_id": "",
        "run_id": "run-1",
        "case_id": "B001",
        "query_sha256": sha256(b"AAPL earnings").hexdigest(),
        "cutoff_ts": "2026-01-15T21:00:00Z",
        "filters": COMPLETE_FILTERS,
        "retrieval_config": PINNED_RETRIEVAL_CONFIG,
        "arms": FOUR_COMPLETE_ARM_RESULTS,
        "created_at": "2026-07-22T00:00:00Z",
        **kwargs,
    }


def _literal_arm(mode: str, chunk_ids: tuple[str, ...]) -> dict[str, Any]:
    """Build one arm payload from the literal chunk-id fixture.

    This is input fixture construction only; the expected union order and
    artifact identity oracles in the tests are hardcoded literals.
    """
    results = []
    for position, chunk_id in enumerate(chunk_ids, start=1):
        results.append({
            "chunk_id": chunk_id,
            "document_id": f"doc:{chunk_id}",
            "available_at": "2026-01-01T00:00:00Z",
            "source_class": "reported_news",
            "rank": position,
            "lexical_raw_score": None,
            "lexical_rank": None,
            "dense_score": None,
            "dense_rank": None,
            "fusion_score": None,
            "fusion_rank": None,
            "arm_ranks": [],
            "arm_scores": [],
            "reranker_score": None,
            "reranker_rank": None,
        })
    return {
        "mode_requested": mode,
        "mode_served": mode,
        "status": "ok",
        "latency_ms": 1.0,
        "degradation_reasons": [],
        "results": results,
    }


# Literal four-arm fixture (independent oracle): fts5 [a,b,c], dense [b,d],
# hybrid [d,a,e], reranked [e,f]. Expected union is the literal tuple below;
# it is never derived from arm outputs or production helpers at test time.
FOUR_COMPLETE_ARM_RESULTS = {
    "fts5": _literal_arm("fts5", ("a", "b", "c")),
    "dense": _literal_arm("dense", ("b", "d")),
    "hybrid": _literal_arm("hybrid", ("d", "a", "e")),
    "reranked": _literal_arm("reranked", ("e", "f")),
}
LITERAL_FTS5_CHUNK_IDS = ("a", "b", "c")
LITERAL_DENSE_CHUNK_IDS = ("b", "d")
LITERAL_HYBRID_CHUNK_IDS = ("d", "a", "e")
LITERAL_RERANKED_CHUNK_IDS = ("e", "f")
EXPECTED_UNION_CHUNK_IDS = ("a", "b", "c", "d", "e", "f")
# Literal golden identity of make_arm_artifact() under the literal arms fixture.
EXPECTED_ARM_ARTIFACT_ID = "854c03da5d20e19f9e04a19ede14b0005a5a5d17987f7cd89c78473cde4f44e7"
SEMANTIC_ARTIFACT_MUTATIONS: tuple[Any, ...] = ()
INVALID_ARM_ARTIFACTS: tuple[dict[str, Any], ...] = ()
