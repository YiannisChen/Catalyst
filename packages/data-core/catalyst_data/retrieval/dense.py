"""LanceDB-backed dense retrieval for the promoted corpus."""

from __future__ import annotations

import math
import re
import time
from typing import Any

import numpy as np

from .result import (
    RetrievalArmUnavailableError,
    RetrievalFilters,
    RetrievalResult,
    RetrievalResultSet,
)
from .fts5 import _validate_inputs


_SEARCHABLE_STATUSES = ("active", "pending_embedding", "embedded", "metadata_only")
_DENSE_TOP_K = 20


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _is_lancedb_availability_error(exc: BaseException) -> bool:
    """Confirmed LanceDB availability failures only (never filter/programmer errors).

    LanceDB 0.30.2 surfaces storage failures as RuntimeError containing
    ``LanceError(IO)``/``LanceError(NotFound)``, a missing table as a
    ValueError whose message says ``was not found``, and connection/storage
    failures as OSError/FileNotFoundError. Everything else (bad filter
    expressions, schema drift, identity corruption) propagates unchanged.
    """
    if isinstance(exc, (FileNotFoundError, OSError)):
        return True
    if isinstance(exc, ValueError):
        return "was not found" in str(exc)
    if isinstance(exc, RuntimeError):
        message = str(exc)
        return "LanceError(IO)" in message or "LanceError(NotFound)" in message
    return False


def _lancedb_prefilter(
    *,
    ticker: str,
    cutoff: str,
    requested_manifest_id: str,
    index_manifest_id: str,
    source_classes: tuple[str, ...] | None,
    evidence_types: tuple[str, ...] | None,
) -> str:
    clauses = [
        f"array_contains(ticker_associations, {_sql_string(ticker)})",
        f"available_at <= {_sql_string(cutoff)}",
        f"corpus_manifest_id = {_sql_string(requested_manifest_id)}",
        f"index_manifest_id = {_sql_string(index_manifest_id)}",
        "status IN (" + ", ".join(_sql_string(value) for value in _SEARCHABLE_STATUSES) + ")",
        "eligibility = 'eligible'",
    ]
    if source_classes is not None:
        clauses.append(
            "source_class IN ("
            + ", ".join(_sql_string(value) for value in source_classes)
            + ")"
        )
    if evidence_types is not None:
        clauses.append(
            "chunk_profile_version IN ("
            + ", ".join(_sql_string(value) for value in evidence_types)
            + ")"
        )
    return " AND ".join(clauses)


def _distance_to_score(row: dict[str, Any]) -> float:
    if "_distance" in row:
        score = 1.0 - float(row["_distance"])
    elif "distance" in row:
        score = 1.0 - float(row["distance"])
    elif "_score" in row:
        score = float(row["_score"])
    else:
        raise ValueError("LanceDB vector result is missing distance/score")
    if not math.isfinite(score) or not -1.0 <= score <= 1.0:
        raise ValueError("LanceDB dense score is outside [-1, 1]")
    return score


def _as_bool(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1) or value == "true"


def _validate_row_identity(
    row: dict[str, Any], *, requested_manifest_id: str, index_manifest_id: str
) -> None:
    if row.get("corpus_manifest_id") != requested_manifest_id:
        raise ValueError("LanceDB row corpus_manifest_id mismatch")
    if row.get("index_manifest_id") != index_manifest_id:
        raise ValueError("LanceDB row index_manifest_id mismatch")
    required = {
        "chunk_id", "document_id", "content_text", "available_at", "source_class",
        "chunk_profile_version", "status", "eligibility",
    }
    missing = sorted(name for name in required if name not in row)
    if missing:
        raise ValueError("LanceDB vector row missing fields: " + ",".join(missing))


def retrieve_dense(
    conn: Any,
    query_embedding: Any,
    *,
    ticker: str,
    cutoff: str,
    requested_manifest_id: str,
    top_k: int = _DENSE_TOP_K,
    source_classes: tuple[str, ...] | None = None,
    evidence_types: tuple[str, ...] | None = None,
    index_manifest_id: str | None = None,
    lancedb_table: Any = None,
) -> RetrievalResultSet:
    """Search the identity-bound LanceDB table with server-side prefilters.

    ``conn`` remains in the signature for the retrieval contract, but dense
    retrieval never reads vectors from SQLite or scans a NumPy matrix. The
    table must be the artifact imported under the supplied IndexManifest.
    """
    del conn
    started = time.perf_counter()
    if top_k != _DENSE_TOP_K:
        raise ValueError("dense retrieval top_k must be 20")
    if not index_manifest_id or re.fullmatch(r"[0-9a-f]{64}", index_manifest_id) is None:
        raise ValueError("dense index manifest identity must be a bound SHA-256 value")
    if lancedb_table is None:
        raise ValueError("identity-bound LanceDB table is required for dense retrieval")
    source_classes, evidence_types = _validate_inputs(
        requested_manifest_id, ticker, cutoff, top_k, top_k,
        source_classes, evidence_types,
    )
    query = np.asarray(query_embedding, dtype=np.float32)
    if query.ndim != 1 or query.size != 1024:
        raise ValueError("query_embedding must be a one-dimensional 1024-d vector")
    norm = float(np.linalg.norm(query))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("query_embedding must be non-zero")
    query = query / norm
    filters = RetrievalFilters(
        ticker=ticker,
        requested_manifest_id=requested_manifest_id,
        cutoff=cutoff,
        source_classes=source_classes,
        evidence_types=evidence_types,
    )
    predicate = _lancedb_prefilter(
        ticker=ticker,
        cutoff=cutoff,
        requested_manifest_id=requested_manifest_id,
        index_manifest_id=index_manifest_id,
        source_classes=source_classes,
        evidence_types=evidence_types,
    )
    try:
        query_builder = lancedb_table.search(query, query_type="vector")
        query_builder = query_builder.where(predicate, prefilter=True)
        rows = query_builder.limit(_DENSE_TOP_K).to_list()
    except RetrievalArmUnavailableError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise RetrievalArmUnavailableError(
            "dense", "dense_unavailable", "LanceDB backend unavailable"
        ) from exc
    except ValueError as exc:
        if not _is_lancedb_availability_error(exc):
            raise
        raise RetrievalArmUnavailableError(
            "dense", "dense_unavailable", "LanceDB backend unavailable"
        ) from exc
    except RuntimeError as exc:
        if not _is_lancedb_availability_error(exc):
            raise
        raise RetrievalArmUnavailableError(
            "dense", "dense_unavailable", "LanceDB backend unavailable"
        ) from exc
    timing_ms = (time.perf_counter() - started) * 1000
    results: list[RetrievalResult] = []
    for rank, raw_row in enumerate(rows, start=1):
        row = dict(raw_row)
        _validate_row_identity(
            row, requested_manifest_id=requested_manifest_id,
            index_manifest_id=index_manifest_id,
        )
        results.append(
            RetrievalResult(
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                content_text=str(row["content_text"]),
                available_at=str(row["available_at"]),
                cutoff=cutoff,
                filters_applied=filters,
                source_class=row["source_class"],
                lexical_raw_score=None,
                lexical_rank=None,
                dense_score=_distance_to_score(row),
                dense_rank=rank,
                corpus_manifest_id=requested_manifest_id,
                index_manifest_id=index_manifest_id,
                mode_requested="dense",
                mode_served="dense",
                is_degraded=False,
                fallback_reason=None,
                timing_ms=timing_ms,
                ticker_associations=tuple(row.get("ticker_associations") or (ticker,)),
                dedup_cluster_id=row.get("dedup_cluster_id"),
                cluster_first_available_at=row.get("cluster_first_available_at"),
                representative_document_id=row.get("representative_document_id"),
                is_novel=_as_bool(row.get("is_novel", False)),
            )
        )
    return RetrievalResultSet(
        candidates=tuple(results),
        results=tuple(results),
        candidate_count=len(results),
        mode_requested="dense",
        mode_served="dense",
        is_degraded=False,
        fallback_reason=None,
    )


__all__ = ["RetrievalArmUnavailableError", "retrieve_dense"]
