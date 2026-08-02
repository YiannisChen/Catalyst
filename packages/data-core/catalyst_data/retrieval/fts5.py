"""Cutoff-safe lexical retrieval with FTS5 and deterministic SQL fallback."""

from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone

from .degraded import rank_sql_fallback
from .result import (
    EVIDENCE_TYPES,
    SEARCHABLE_STATUSES,
    SOURCE_CLASSES,
    RetrievalContractError,
    RetrievalFilters,
    RetrievalResult,
    RetrievalResultSet,
)
from .trace import RetrievalTrace


def _normalize_query(query: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", query).casefold()
    return tuple(dict.fromkeys(re.findall(r"[^\W_]+", normalized, re.UNICODE)))


def _validate_inputs(
    requested_manifest_id: str,
    ticker: str,
    cutoff: str,
    top_k: int,
    candidate_depth: int,
    source_classes: tuple[str, ...] | None,
    evidence_types: tuple[str, ...] | None,
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    if re.fullmatch(r"[0-9a-f]{64}", requested_manifest_id) is None:
        raise RetrievalContractError("invalid_manifest_id")
    if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker) is None:
        raise RetrievalContractError("invalid_ticker")
    try:
        parsed_cutoff = datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        raise RetrievalContractError("invalid_cutoff") from None
    if parsed_cutoff.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") != cutoff:
        raise RetrievalContractError("invalid_cutoff")
    if not (1 <= top_k <= candidate_depth <= 100):
        raise RetrievalContractError("invalid_depth")

    canonical_sources = _validate_filter(source_classes, SOURCE_CLASSES)
    canonical_evidence = _validate_filter(evidence_types, EVIDENCE_TYPES)
    return canonical_sources, canonical_evidence


def _validate_filter(
    values: tuple[str, ...] | None,
    allowed: frozenset[str],
) -> tuple[str, ...] | None:
    if values is None:
        return None
    if not values or any(value not in allowed for value in values):
        raise RetrievalContractError("invalid_filter")
    return tuple(sorted(set(values)))


def _eligibility_predicate(
    requested_manifest_id: str,
    cutoff: str,
    ticker: str,
    source_classes: tuple[str, ...] | None,
    evidence_types: tuple[str, ...] | None,
) -> tuple[str, list[object]]:
    status_placeholders = ",".join("?" for _ in SEARCHABLE_STATUSES)
    sql = (
        f"c.manifest_id = ? AND c.status IN ({status_placeholders}) "
        "AND c.eligibility = 'eligible' AND c.available_at <= ? "
        "AND EXISTS (SELECT 1 FROM json_each(c.ticker_associations) t "
        "WHERE t.value = ?)"
    )
    params: list[object] = [
        requested_manifest_id, *SEARCHABLE_STATUSES, cutoff, ticker,
    ]
    if source_classes is not None:
        sql += f" AND c.source_class IN ({','.join('?' for _ in source_classes)})"
        params.extend(source_classes)
    if evidence_types is not None:
        sql += (
            f" AND c.chunk_profile_version IN "
            f"({','.join('?' for _ in evidence_types)})"
        )
        params.extend(evidence_types)
    return sql, params


def _served_mode(
    conn: sqlite3.Connection, manifest_id: str
) -> tuple[str, str | None, str | None]:
    fts_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_chunks_fts'"
    ).fetchone()
    if fts_exists is None:
        return "sql_like", "fts5_missing", None
    state_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(lexical_index_state)")
    }
    generation_projection = (
        "lexical_generation_id" if "lexical_generation_id" in state_columns else "NULL"
    )
    state = conn.execute(
        f"SELECT corpus_manifest_id, mode_served, fallback_reason, "
        f"{generation_projection} FROM lexical_index_state WHERE singleton_id = 1"
    ).fetchone()
    if state is None or state[0] != manifest_id:
        return "sql_like", "fts5_stale", None
    if state[1] != "fts5":
        return "sql_like", state[2] or "fts5_unavailable", None
    return "fts5", None, state[3]


def retrieve_lexical(
    conn: sqlite3.Connection,
    query: str,
    *,
    ticker: str,
    cutoff: str,
    requested_manifest_id: str,
    top_k: int = 8,
    candidate_depth: int = 20,
    source_classes: tuple[str, ...] | None = None,
    evidence_types: tuple[str, ...] | None = None,
    include_trace: bool = False,
) -> RetrievalResultSet:
    started = time.perf_counter()
    source_classes, evidence_types = _validate_inputs(
        requested_manifest_id, ticker, cutoff, top_k, candidate_depth,
        source_classes, evidence_types,
    )
    filters = RetrievalFilters(
        ticker=ticker,
        requested_manifest_id=requested_manifest_id,
        cutoff=cutoff,
        source_classes=source_classes,
        evidence_types=evidence_types,
    )
    if conn.execute(
        "SELECT 1 FROM corpus_manifest WHERE manifest_id = ?", (requested_manifest_id,)
    ).fetchone() is None:
        raise RetrievalContractError("manifest_not_found")
    from catalyst_data.corpus.streaming_publication import served_chunks_relation

    chunks_relation = served_chunks_relation(conn)

    eligibility_sql, eligibility_params = _eligibility_predicate(
        requested_manifest_id, cutoff, ticker, source_classes, evidence_types,
    )
    manifest_count = conn.execute(
        f"SELECT COUNT(*) FROM {chunks_relation} WHERE manifest_id = ?",
        (requested_manifest_id,),
    ).fetchone()[0]
    eligible_count = conn.execute(
        f"SELECT COUNT(*) FROM {chunks_relation} c WHERE {eligibility_sql}",
        eligibility_params,
    ).fetchone()[0]
    mode_served, degradation_reason, lexical_generation_id = _served_mode(
        conn, requested_manifest_id
    )
    filtered_at = time.perf_counter()
    terms = _normalize_query(query)

    raw_candidates: list[tuple[str, str, str, str, float | None]] = []
    matched_count = 0
    fallback_reason = degradation_reason
    if not terms:
        fallback_reason = "empty_query"
    elif mode_served == "fts5":
        match_query = " AND ".join(f'"{term}"' for term in terms)
        if lexical_generation_id is None:
            rows = conn.execute(
                f"""SELECT c.chunk_id, c.document_id, c.available_at, c.source_class,
                           bm25(corpus_chunks_fts) AS score
                    FROM corpus_chunks_fts fts
                    JOIN {chunks_relation} c
                      ON c.manifest_id = fts.manifest_id AND c.chunk_id = fts.chunk_id
                    WHERE corpus_chunks_fts MATCH ? AND {eligibility_sql}
                    ORDER BY score ASC, c.chunk_id ASC""",
                [match_query, *eligibility_params],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT c.chunk_id, c.document_id, c.available_at, c.source_class,
                           bm25(corpus_build_chunks_fts) AS score
                    FROM corpus_build_chunks_fts fts
                    JOIN {chunks_relation} c ON c.chunk_id = fts.chunk_id
                    WHERE corpus_build_chunks_fts MATCH ? AND fts.build_id = ?
                      AND {eligibility_sql}
                    ORDER BY score ASC, c.chunk_id ASC""",
                [match_query, lexical_generation_id, *eligibility_params],
            ).fetchall()
        matched_count = len(rows)
        raw_candidates = [tuple(row) for row in rows[:candidate_depth]]
    else:
        rows, matched_count = rank_sql_fallback(
            conn,
            eligibility_sql=eligibility_sql,
            eligibility_params=eligibility_params,
            terms=terms,
            candidate_depth=candidate_depth,
            chunks_relation=chunks_relation,
        )
        raw_candidates = [
            (row[0], row[1], row[2], row[3], None) for row in rows
        ]

    scored_at = time.perf_counter()
    total_ms = (scored_at - started) * 1000
    candidates = tuple(
        RetrievalResult(
            chunk_id=row[0],
            document_id=row[1],
            available_at=row[2],
            cutoff=cutoff,
            filters_applied=filters,
            source_class=row[3],
            lexical_raw_score=row[4],
            lexical_rank=rank,
            corpus_manifest_id=requested_manifest_id,
            index_manifest_id=None,
            mode_requested="lexical",
            mode_served=mode_served,
            is_degraded=mode_served != "fts5",
            fallback_reason=degradation_reason,
            timing_ms=total_ms,
        )
        for rank, row in enumerate(raw_candidates, start=1)
    )
    results = candidates[:top_k]
    trace = None
    if include_trace:
        trace = RetrievalTrace(
            manifest_row_count=manifest_count,
            eligible_row_count=eligible_count,
            matched_row_count=matched_count,
            candidate_count=len(candidates),
            final_count=len(results),
            filter_ms=(filtered_at - started) * 1000,
            score_ms=(scored_at - filtered_at) * 1000,
            total_ms=total_ms,
            mode_requested="lexical",
            mode_served=mode_served,
            fallback_reason=fallback_reason,
        )
    return RetrievalResultSet(
        candidates=candidates,
        results=results,
        candidate_count=len(candidates),
        mode_requested="lexical",
        mode_served=mode_served,
        is_degraded=mode_served != "fts5",
        fallback_reason=fallback_reason,
        trace=trace,
    )


__all__ = ["RetrievalContractError", "retrieve_lexical"]
