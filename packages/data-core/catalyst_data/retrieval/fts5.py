"""Cutoff-safe lexical retrieval with FTS5 and deterministic SQL fallback."""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone

from .degraded import rank_sql_fallback
from .query_policy import (
    TEMPORAL_WINDOW_DAYS,
    TemporalCenterResolution,
    compile_match,
    normalize_query_terms,
    plan_lexical_query,
    resolve_temporal_center,
)
from .result import (
    EVIDENCE_TYPES,
    SEARCHABLE_STATUSES,
    SOURCE_CLASSES,
    RetrievalArmUnavailableError,
    RetrievalContractError,
    RetrievalFilters,
    RetrievalResult,
    RetrievalResultSet,
)
from .trace import RetrievalTrace


def _normalize_query(query: str) -> tuple[str, ...]:
    """Raw word tokens (NFC/casefold, order preserved, duplicates removed)."""
    return normalize_query_terms(query)


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


def _is_sqlite_availability_error(exc: BaseException) -> bool:
    """Confirmed SQLite availability failures only (never query/programmer errors)."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    if "fts5: syntax error" in message:
        return False
    return any(
        marker in message
        for marker in (
            "unable to open database file",
            "database is locked",
            "database is busy",
            "disk i/o error",
            "no such table",
            "readonly database",
        )
    )


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


def _resolve_center(
    *,
    query: str,
    cutoff: str,
    session_date: str | None = None,
    trade_date: str | None = None,
) -> TemporalCenterResolution:
    """Structured session center only — free-text query date never overrides."""
    return resolve_temporal_center(
        query=query,
        cutoff=cutoff,
        session_date=session_date,
        trade_date=trade_date,
    )


def _window_predicate(target_date: str | None, window_days: int | None) -> tuple[str, list[object]]:
    """SQL fragment restricting available_at to ±window_days of target_date.

    ``window_days is None`` means unbounded (still subject to cutoff eligibility).
    Bounds are computed in Python so the predicate is deterministic and index-
    friendly; comparison uses date(available_at) for calendar-day semantics.
    """
    if target_date is None or window_days is None:
        return "", []
    from datetime import date, timedelta

    center = date.fromisoformat(target_date)
    start = (center - timedelta(days=window_days)).isoformat()
    end = (center + timedelta(days=window_days)).isoformat()
    return (
        " AND date(c.available_at) BETWEEN ? AND ?",
        [start, end],
    )


def _temporal_order_sql(target_date: str | None) -> str:
    if target_date is None:
        return "c.available_at DESC, c.chunk_id ASC"
    # Explicit temporal lag first, then BM25 (lower better), then chunk_id.
    return (
        "ABS(julianday(date(c.available_at)) - julianday(?)) ASC, "
        "score ASC, c.chunk_id ASC"
    )


def _fts_from_clause(
    *,
    chunks_relation: str,
    lexical_generation_id: str | None,
    eligibility_sql: str,
    window_sql: str,
    order_sql: str,
) -> tuple[str, str, str]:
    """Return (select_sql, count_sql, fts_table) for MATCH queries."""
    if lexical_generation_id is None:
        fts_table = "corpus_chunks_fts"
        base = f"""
            FROM corpus_chunks_fts fts
            JOIN {chunks_relation} c
              ON c.manifest_id = fts.manifest_id AND c.chunk_id = fts.chunk_id
            WHERE corpus_chunks_fts MATCH ? AND {eligibility_sql}{window_sql}
        """
        score_expr = "bm25(corpus_chunks_fts) AS score"
    else:
        fts_table = "corpus_build_chunks_fts"
        base = f"""
            FROM corpus_build_chunks_fts fts
            JOIN {chunks_relation} c ON c.chunk_id = fts.chunk_id
            WHERE corpus_build_chunks_fts MATCH ? AND fts.build_id = ?
              AND {eligibility_sql}{window_sql}
        """
        score_expr = "bm25(corpus_build_chunks_fts) AS score"
    select_sql = (
        f"SELECT c.chunk_id, c.document_id, c.available_at, c.source_class, "
        f"{score_expr}, c.content_text {base} ORDER BY {order_sql} LIMIT ?"
    )
    count_sql = f"SELECT COUNT(*) {base}"
    return select_sql, count_sql, fts_table


def _run_fts_match(
    conn: sqlite3.Connection,
    *,
    match_expr: str,
    select_sql: str,
    count_sql: str,
    params_prefix: list[object],
    eligibility_params: list[object],
    window_params: list[object],
    order_params: list[object],
    candidate_depth: int,
) -> tuple[list[tuple], int]:
    """Execute COUNT + LIMIT select without materializing the full match set."""
    base_params = [match_expr, *params_prefix, *eligibility_params, *window_params]
    matched = int(conn.execute(count_sql, base_params).fetchone()[0])
    if matched == 0:
        return [], 0
    rows = conn.execute(
        select_sql,
        [*base_params, *order_params, candidate_depth],
    ).fetchall()
    return list(rows), matched


def _run_temporal_only(
    conn: sqlite3.Connection,
    *,
    chunks_relation: str,
    eligibility_sql: str,
    eligibility_params: list[object],
    target_date: str | None,
    window_days: int | None,
    candidate_depth: int,
) -> tuple[list[tuple], int]:
    """Eligible chunks ordered by temporal lag (no unrestricted event-term OR)."""
    window_sql, window_params = _window_predicate(target_date, window_days)
    count_sql = (
        f"SELECT COUNT(*) FROM {chunks_relation} c "
        f"WHERE {eligibility_sql}{window_sql}"
    )
    matched = int(
        conn.execute(count_sql, [*eligibility_params, *window_params]).fetchone()[0]
    )
    if matched == 0:
        return [], 0
    if target_date is None:
        order_sql = "c.available_at DESC, c.chunk_id ASC"
        order_params: list[object] = []
    else:
        order_sql = (
            "ABS(julianday(date(c.available_at)) - julianday(?)) ASC, "
            "c.chunk_id ASC"
        )
        order_params = [target_date]
    select_sql = (
        f"SELECT c.chunk_id, c.document_id, c.available_at, c.source_class, "
        f"NULL AS score, c.content_text "
        f"FROM {chunks_relation} c "
        f"WHERE {eligibility_sql}{window_sql} "
        f"ORDER BY {order_sql} LIMIT ?"
    )
    rows = conn.execute(
        select_sql,
        [*eligibility_params, *window_params, *order_params, candidate_depth],
    ).fetchall()
    return list(rows), matched


def _fts_retrieve_with_policy(
    conn: sqlite3.Connection,
    *,
    query: str,
    ticker: str,
    cutoff: str,
    chunks_relation: str,
    eligibility_sql: str,
    eligibility_params: list[object],
    lexical_generation_id: str | None,
    candidate_depth: int,
    session_date: str | None = None,
    trade_date: str | None = None,
) -> tuple[list[tuple], int, str, str, int | None, TemporalCenterResolution]:
    """Return (rows, matched_count, match_mode, policy, window_days, center).

    AMEND-5.1 temporal candidate policy:
    - content terms use AND-first then OR within expanding calendar windows;
    - low-information (ticker/date only) uses pure temporal ranking within
      expanding windows — never an unbounded generic event-term OR;
    - ranking is explicit temporal lag + BM25 + chunk_id (deterministic).

    AMEND-5.2: temporal center is always structured session/cutoff; free-text
    query dates are recorded for conflict diagnostics only.
    """
    plan = plan_lexical_query(query, ticker)
    center = _resolve_center(
        query=query, cutoff=cutoff, session_date=session_date, trade_date=trade_date,
    )
    target_date = center.center_date
    windows: list[int | None] = list(TEMPORAL_WINDOW_DAYS) + [None]

    if plan.policy == "temporal_window" or not plan.terms:
        for window in windows:
            rows, matched = _run_temporal_only(
                conn,
                chunks_relation=chunks_relation,
                eligibility_sql=eligibility_sql,
                eligibility_params=eligibility_params,
                target_date=target_date,
                window_days=window,
                candidate_depth=candidate_depth,
            )
            if matched > 0:
                return rows, matched, "temporal", plan.policy, window, center
        return [], 0, "none", plan.policy, None, center

    params_prefix: list[object] = (
        [] if lexical_generation_id is None else [lexical_generation_id]
    )
    for window in windows:
        window_sql, window_params = _window_predicate(target_date, window)
        order_sql = _temporal_order_sql(target_date)
        order_params: list[object] = [] if target_date is None else [target_date]
        # Replace "score" alias for non-fts temporal-only already handled.
        select_sql, count_sql, _ = _fts_from_clause(
            chunks_relation=chunks_relation,
            lexical_generation_id=lexical_generation_id,
            eligibility_sql=eligibility_sql,
            window_sql=window_sql,
            order_sql=order_sql,
        )
        and_query = compile_match(plan.terms, mode="and")
        rows, matched = _run_fts_match(
            conn,
            match_expr=and_query,
            select_sql=select_sql,
            count_sql=count_sql,
            params_prefix=params_prefix,
            eligibility_params=eligibility_params,
            window_params=window_params,
            order_params=order_params,
            candidate_depth=candidate_depth,
        )
        if matched > 0:
            return rows, matched, "AND", plan.policy, window, center
        if len(plan.terms) > 1:
            or_query = compile_match(plan.terms, mode="or")
            rows, matched = _run_fts_match(
                conn,
                match_expr=or_query,
                select_sql=select_sql,
                count_sql=count_sql,
                params_prefix=params_prefix,
                eligibility_params=eligibility_params,
                window_params=window_params,
                order_params=order_params,
                candidate_depth=candidate_depth,
            )
            if matched > 0:
                return rows, matched, "OR", plan.policy, window, center
    return [], 0, "none", plan.policy, None, center


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
    session_date: str | None = None,
    trade_date: str | None = None,
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
    match_mode: str = "none"
    policy_name: str = "content"
    temporal_window_days: int | None = None
    temporal_center = _resolve_center(
        query=query, cutoff=cutoff, session_date=session_date, trade_date=trade_date,
    )
    try:
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
        raw_terms = _normalize_query(query)

        raw_candidates: list[tuple[str, str, str, str, float | None, str]] = []
        matched_count = 0
        fallback_reason = degradation_reason
        if not raw_terms:
            fallback_reason = "empty_query"
            match_mode = "none"
            policy_name = "content"
        elif mode_served == "fts5":
            (
                rows, matched_count, match_mode, policy_name,
                temporal_window_days, temporal_center,
            ) = (
                _fts_retrieve_with_policy(
                    conn,
                    query=query,
                    ticker=ticker,
                    cutoff=cutoff,
                    chunks_relation=chunks_relation,
                    eligibility_sql=eligibility_sql,
                    eligibility_params=eligibility_params,
                    lexical_generation_id=lexical_generation_id,
                    candidate_depth=candidate_depth,
                    session_date=session_date,
                    trade_date=trade_date,
                )
            )
            if match_mode == "none" and not plan_lexical_query(query, ticker).terms:
                # Distinguish empty structured-only query that still has terms empty
                # after stripping (should not happen for temporal_window).
                pass
            raw_candidates = [tuple(row) for row in rows]
        else:
            plan = plan_lexical_query(query, ticker)
            policy_name = plan.policy
            temporal_center = _resolve_center(
                query=query, cutoff=cutoff,
                session_date=session_date, trade_date=trade_date,
            )
            rows, matched_count, match_mode, temporal_window_days = rank_sql_fallback(
                conn,
                eligibility_sql=eligibility_sql,
                eligibility_params=eligibility_params,
                terms=plan.terms,
                candidate_depth=candidate_depth,
                chunks_relation=chunks_relation,
                target_date=temporal_center.center_date,
                policy=plan.policy,
            )
            raw_candidates = [
                (row[0], row[1], row[2], row[3], None, row[4]) for row in rows
            ]
    except sqlite3.OperationalError as exc:
        if not _is_sqlite_availability_error(exc):
            raise
        raise RetrievalArmUnavailableError(
            "lexical", "fts5_unavailable", "SQLite backend unavailable"
        ) from exc

    scored_at = time.perf_counter()
    total_ms = (scored_at - started) * 1000
    candidates = tuple(
        RetrievalResult(
            chunk_id=row[0],
            document_id=row[1],
            content_text=row[5],
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
            temporal_center_date=temporal_center.center_date,
            query_date=temporal_center.query_date,
            query_date_conflict=temporal_center.conflict,
            query_date_decision=temporal_center.decision,
        )
        for rank, row in enumerate(raw_candidates, start=1)
    )
    results = candidates[:top_k]
    # Trace counts must be monotonically non-increasing. Cap matched at
    # eligible when the selected window is the eligible set itself.
    safe_matched = min(matched_count, eligible_count)
    safe_candidates = min(len(candidates), safe_matched)
    safe_final = min(len(results), safe_candidates)
    # Re-slice if capping changed ordering counts (should not drop rows when
    # matched >= returned).
    candidates = candidates[:safe_candidates]
    results = candidates[:safe_final]
    trace = None
    if include_trace:
        trace = RetrievalTrace(
            manifest_row_count=manifest_count,
            eligible_row_count=eligible_count,
            matched_row_count=safe_matched,
            candidate_count=len(candidates),
            final_count=len(results),
            filter_ms=(filtered_at - started) * 1000,
            score_ms=(scored_at - filtered_at) * 1000,
            total_ms=total_ms,
            mode_requested="lexical",
            mode_served=mode_served,
            fallback_reason=fallback_reason,
            match_mode=match_mode if match_mode in {"AND", "OR", "temporal", "none"} else "none",
            policy=policy_name,
            temporal_window_days=temporal_window_days,
            temporal_center_date=temporal_center.center_date,
            query_date=temporal_center.query_date,
            query_date_conflict=temporal_center.conflict,
            query_date_decision=temporal_center.decision,
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
        temporal_center_date=temporal_center.center_date,
        query_date=temporal_center.query_date,
        query_date_conflict=temporal_center.conflict,
        query_date_decision=temporal_center.decision,
    )


__all__ = [
    "RetrievalArmUnavailableError", "RetrievalContractError", "retrieve_lexical",
]
