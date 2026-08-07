"""Production hybrid retrieval facade with typed arm failure semantics.

Input/filter/manifest/query-vector validation completes before any arm runs.
Only a confirmed backend availability failure (``RetrievalArmUnavailableError``)
may degrade to the surviving arm; contract errors, identity drift, persisted
row corruption, and programmer errors always propagate.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .dense import retrieve_dense
from .fts5 import _validate_inputs, retrieve_lexical
from .fusion import fuse
from .reranker import RerankerGate, rerank
from .result import (
    RetrievalArmUnavailableError,
    RetrievalContractError,
    RetrievalResult,
    RetrievalResultSet,
)


def _validate_hybrid_inputs(
    db: Any,
    *,
    query: str,
    ticker: str,
    cutoff: str,
    mode: str,
    query_embedding: Any,
    requested_manifest_id: str,
    index_manifest_id: str,
    lancedb_table: Any,
    source_classes: tuple[str, ...] | None,
    evidence_types: tuple[str, ...] | None,
) -> None:
    """Validate everything that can fail before entering arm fallback.

    Raises RetrievalContractError for filter/manifest violations and ValueError
    for programmer errors (bad embedding shape, missing dense backend, non-hex
    index identity). SQLite unavailability is deliberately not decided here:
    the lexical arm boundary converts confirmed OperationalError to
    RetrievalArmUnavailableError so a dense-only fallback remains possible.
    """
    if mode not in {"hybrid", "reranked"}:
        raise ValueError("hybrid facade supports hybrid and reranked modes")
    if re.fullmatch(r"[0-9a-f]{64}", index_manifest_id) is None:
        raise RetrievalContractError("invalid_index_manifest_id")
    if lancedb_table is None:
        raise ValueError("identity-bound LanceDB table is required for dense retrieval")
    if not isinstance(query, str) or not query:
        raise RetrievalContractError("invalid_query")
    _validate_inputs(
        requested_manifest_id, ticker, cutoff, 20, 20,
        source_classes, evidence_types,
    )
    try:
        row = db.execute(
            "SELECT 1 FROM corpus_manifest WHERE manifest_id = ?",
            (requested_manifest_id,),
        ).fetchone()
        db_reachable = True
    except sqlite3.OperationalError:
        # An unreachable SQLite backend is not a contract failure; the lexical
        # arm boundary converts confirmed OperationalError to
        # RetrievalArmUnavailableError so a dense-only fallback can serve.
        row = None
        db_reachable = False
    if db_reachable and row is None:
        raise RetrievalContractError("manifest_not_found")
    query_vector = np.asarray(query_embedding, dtype=np.float32)
    if query_vector.ndim != 1 or query_vector.size != 1024:
        raise ValueError("query_embedding must be a one-dimensional 1024-d vector")
    norm = float(np.linalg.norm(query_vector))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("query_embedding must be non-zero")


def _bind_index_manifest(result_set: RetrievalResultSet, index_manifest_id: str) -> RetrievalResultSet:
    values = tuple(item.model_copy(update={"index_manifest_id": index_manifest_id}) for item in result_set.candidates)
    return result_set.model_copy(update={"candidates": values, "results": values[:len(result_set.results)]})


@dataclass(frozen=True)
class HybridRetrievalResult:
    mode_requested: str
    mode_served: str
    lexical_results: RetrievalResultSet | None = None
    dense_results: RetrievalResultSet | None = None
    fusion_results: tuple[RetrievalResult, ...] = ()
    reranker_results: RetrievalResultSet | None = None
    final_results: tuple[RetrievalResult, ...] = ()
    degradation_reasons: tuple[str, ...] = ()


class ProductionHybridRetriever:
    """Retriever protocol adapter used by the agent runtime.

    It binds the approved corpus/index identities once and sends every query
    through the same lexical+dense+RRF+reranker facade.  The adapter does not
    create an embedding lambda or expose the legacy LanceDB search path.
    """

    def __init__(
        self,
        *,
        db: Any,
        lancedb_table: Any,
        embedding_fn: Callable[[str], Any],
        reranker: object | None,
        index_manifest_id: str,
        reranker_timeout_seconds: float = 2.0,
    ) -> None:
        self.db = db
        self.lancedb_table = lancedb_table
        self.embedding_fn = embedding_fn
        self.reranker = reranker
        self.index_manifest_id = index_manifest_id
        self.reranker_timeout_seconds = reranker_timeout_seconds
        self._reranker_gate = RerankerGate()

    def retrieve(
        self,
        query: str,
        *,
        ticker: str,
        cutoff: str,
        requested_manifest_id: str,
        top_k: int = 8,
        candidate_depth: int = 20,
    ) -> list[RetrievalResult]:
        if candidate_depth != 20:
            raise ValueError("production hybrid candidate_depth must be 20")
        result = retrieve_hybrid(
            self.db,
            query=query,
            ticker=ticker,
            cutoff=cutoff,
            mode="reranked",
            reranker=self.reranker,
            query_embedding=self.embedding_fn(query),
            requested_manifest_id=requested_manifest_id,
            index_manifest_id=self.index_manifest_id,
            lancedb_table=self.lancedb_table,
            reranker_timeout_seconds=self.reranker_timeout_seconds,
            reranker_gate=self._reranker_gate,
        )
        return list(result.final_results[:top_k])


def retrieve_hybrid(
    db: Any,
    *,
    query: str,
    ticker: str,
    cutoff: str,
    mode: str = "hybrid",
    reranker: Any = None,
    query_embedding: Any = None,
    requested_manifest_id: str,
    index_manifest_id: str,
    lancedb_table: Any,
    source_classes: tuple[str, ...] | None = None,
    evidence_types: tuple[str, ...] | None = None,
    reranker_timeout_seconds: float = 2.0,
    reranker_gate: RerankerGate | None = None,
) -> HybridRetrievalResult:
    """Run lexical and dense arms directly with identical scope arguments."""
    _validate_hybrid_inputs(
        db,
        query=query,
        ticker=ticker,
        cutoff=cutoff,
        mode=mode,
        query_embedding=query_embedding,
        requested_manifest_id=requested_manifest_id,
        index_manifest_id=index_manifest_id,
        lancedb_table=lancedb_table,
        source_classes=source_classes,
        evidence_types=evidence_types,
    )
    shared_scope = {
        "ticker": ticker,
        "cutoff": cutoff,
        "requested_manifest_id": requested_manifest_id,
        "source_classes": source_classes,
        "evidence_types": evidence_types,
    }
    lexical = dense = None
    reasons: list[str] = []
    try:
        lexical = retrieve_lexical(
            db, query, top_k=20, candidate_depth=20, **shared_scope,
        )
    except RetrievalArmUnavailableError as exc:
        reasons.append(exc.code)
    try:
        dense = retrieve_dense(
            db, query_embedding, top_k=20, index_manifest_id=index_manifest_id,
            lancedb_table=lancedb_table, **shared_scope,
        )
    except RetrievalArmUnavailableError as exc:
        reasons.append(exc.code)
    if lexical is None and dense is None:
        return HybridRetrievalResult(mode, "failed", degradation_reasons=tuple(reasons))
    if lexical is None or dense is None:
        survivor = dense if lexical is None else lexical
        survivor = _bind_index_manifest(survivor, index_manifest_id)
        served = "dense" if lexical is None else survivor.mode_served
        return HybridRetrievalResult(
            mode, served, lexical_results=lexical, dense_results=dense,
            final_results=tuple(survivor.results), degradation_reasons=tuple(reasons),
        )
    lexical = _bind_index_manifest(lexical, index_manifest_id)
    dense = _bind_index_manifest(dense, index_manifest_id)
    fused = tuple(fuse(lexical.results, dense.results, k=60, output_k=20))
    if mode == "hybrid":
        return HybridRetrievalResult(
            mode, "hybrid", lexical_results=lexical, dense_results=dense,
            fusion_results=fused, final_results=fused,
            degradation_reasons=tuple(reasons),
        )
    reranked = rerank(
        query=query, candidates=fused, reranker=reranker,
        timeout_seconds=reranker_timeout_seconds, gate=reranker_gate,
    )
    if reranked.is_degraded:
        reasons.extend(reranked.degradation_reasons)
        return HybridRetrievalResult(
            mode, "hybrid", lexical_results=lexical, dense_results=dense,
            fusion_results=fused, reranker_results=reranked,
            final_results=fused, degradation_reasons=tuple(reasons),
        )
    return HybridRetrievalResult(
        mode, "reranked", lexical_results=lexical, dense_results=dense,
        fusion_results=fused, reranker_results=reranked,
        final_results=tuple(reranked.results), degradation_reasons=tuple(reasons),
    )


__all__ = [
    "HybridRetrievalResult", "ProductionHybridRetriever", "retrieve_hybrid",
]
