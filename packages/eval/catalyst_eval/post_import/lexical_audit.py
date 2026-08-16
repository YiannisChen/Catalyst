"""Frozen-corpus read-only lexical effect audit (AMEND-5.1).

Runs without embedding models: for every approved T4 case it reports the
normalized lexical terms, the executed match policy, temporal alignment
metrics, FTS5 matched/returned counts, top chunk IDs/dates/source classes,
and cutoff/ticker violations against the served-corpus predicate.

``run_lexical_audit`` uses a production-faithful fast path that materializes
the served predicate into a connection-scoped TEMP table (never persisted)
and then executes the same temporal-window MATCH + BM25 + lag ranking shape
used by ``catalyst_data.retrieval.fts5.retrieve_lexical``.  Passing
``use_production_retriever=True`` calls the real production retriever instead;
the unit tests prove both paths agree on chunk IDs, counts, match_mode, and
policy.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from catalyst_data.retrieval.query_policy import (
    TEMPORAL_WINDOW_DAYS,
    compile_match,
    plan_lexical_query,
    resolve_temporal_center,
)
from catalyst_data.retrieval.result import SEARCHABLE_STATUSES

AUDIT_SCHEMA_VERSION = "amend5_1_lexical_effect_audit_v1"

# SUFFICIENT cases require tightly aligned top-20 evidence (calendar-day lag
# on available_at vs session/target date). 2 calendar days covers one weekend
# / holiday adjacency around a trading session without coercing INSUFFICIENT
# cases (those only require non-empty + zero violations).
SUFFICIENT_MAX_NEAREST_LAG_DAYS = 2.0


@dataclass(frozen=True)
class LexicalTopChunk:
    chunk_id: str
    available_at: str
    source_class: str
    rank: int


@dataclass(frozen=True)
class LexicalCaseAudit:
    case_id: str
    ticker: str
    cutoff: str
    query: str
    raw_terms: tuple[str, ...]
    content_terms: tuple[str, ...]
    policy: str
    match_mode: str  # "AND" | "OR" | "temporal" | "none"
    matched_count: int
    returned_count: int
    top_chunks: tuple[LexicalTopChunk, ...] = field(default_factory=tuple)
    cutoff_violations: tuple[str, ...] = field(default_factory=tuple)
    ticker_violations: tuple[str, ...] = field(default_factory=tuple)
    nearest_lag_days: float | None = None
    within_2_day_count: int = 0
    within_7_day_count: int = 0
    source_class_distribution: dict[str, int] = field(default_factory=dict)
    temporal_window_days: int | None = None
    temporal_center_date: str | None = None
    query_date: str | None = None
    query_date_conflict: bool = False
    query_date_decision: str | None = None
    latency_ms: float = 0.0
    should_refuse: bool | None = None
    effect_valid: bool = False
    effect_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.effect_valid


def _eligibility_clause() -> str:
    statuses = ",".join("?" for _ in SEARCHABLE_STATUSES)
    return (
        f"c.manifest_id = ? AND c.status IN ({statuses}) "
        "AND c.eligibility = 'eligible' AND c.available_at <= ? "
        "AND EXISTS (SELECT 1 FROM json_each(c.ticker_associations) t WHERE t.value = ?)"
    )


def _eligibility_params(manifest_id: str, cutoff: str, ticker: str) -> list[object]:
    return [manifest_id, *SEARCHABLE_STATUSES, cutoff, ticker]


def _chunks_relation(conn: sqlite3.Connection) -> str:
    from catalyst_data.corpus.streaming_publication import served_chunks_relation

    return served_chunks_relation(conn)


def _materialize_served(conn: sqlite3.Connection, *, manifest_id: str, cutoff: str, ticker: str) -> None:
    conn.execute("DROP TABLE IF EXISTS tmp_audit_served")
    conn.execute(
        f"""
        CREATE TEMP TABLE tmp_audit_served AS
        SELECT c.chunk_id, c.available_at, c.source_class
        FROM {_chunks_relation(conn)} c
        WHERE {_eligibility_clause()}
        """,
        _eligibility_params(manifest_id, cutoff, ticker),
    )


def _served_chunk(conn: sqlite3.Connection, *, chunk_id: str, manifest_id: str, cutoff: str, ticker: str):
    return conn.execute(
        f"""
        SELECT 1 FROM {_chunks_relation(conn)} c
        WHERE c.chunk_id = ? AND {_eligibility_clause()}
        """,
        (chunk_id, *_eligibility_params(manifest_id, cutoff, ticker)),
    ).fetchone()


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").date()
        except ValueError:
            return None


def _target_date(
    query: str,
    cutoff: str,
    session_date: str | None,
    plan_target: str | None = None,  # diagnostic only; never overrides center
) -> tuple[str | None, str | None, bool, str]:
    """Return (center_date, query_date, conflict, decision).

    AMEND-5.2: structured session/cutoff is always the temporal center.
    ``plan_target`` is ignored for the center (kept for call-site compatibility).
    """
    del plan_target  # free-text parse never overrides structured center
    resolved = resolve_temporal_center(
        query=query, cutoff=cutoff, session_date=session_date,
    )
    return (
        resolved.center_date,
        resolved.query_date,
        resolved.conflict,
        resolved.decision,
    )


def _lag_days(available_at: str, target: str | None) -> float | None:
    if target is None:
        return None
    avail = _parse_day(available_at)
    tgt = _parse_day(target)
    if avail is None or tgt is None:
        return None
    return float(abs((avail - tgt).days))


def _window_bounds(target_date: str | None, window_days: int | None) -> tuple[str, str] | None:
    if target_date is None or window_days is None:
        return None
    center = date.fromisoformat(target_date)
    return (
        (center - timedelta(days=window_days)).isoformat(),
        (center + timedelta(days=window_days)).isoformat(),
    )


def _temporal_metrics(
    top: tuple[LexicalTopChunk, ...],
    target_date: str | None,
) -> tuple[float | None, int, int, dict[str, int]]:
    lags: list[float] = []
    within_2 = 0
    within_7 = 0
    dist: Counter[str] = Counter()
    for chunk in top:
        dist[chunk.source_class] += 1
        lag = _lag_days(chunk.available_at, target_date)
        if lag is None:
            continue
        lags.append(lag)
        if lag <= 2.0:
            within_2 += 1
        if lag <= 7.0:
            within_7 += 1
    nearest = min(lags) if lags else None
    return nearest, within_2, within_7, dict(sorted(dist.items()))


def _effect_validity(
    *,
    returned_count: int,
    cutoff_violations: tuple[str, ...],
    ticker_violations: tuple[str, ...],
    nearest_lag_days: float | None,
    within_2_day_count: int,
    should_refuse: bool | None,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if returned_count <= 0:
        reasons.append("empty_results")
    if cutoff_violations:
        reasons.append("cutoff_violations")
    if ticker_violations:
        reasons.append("ticker_violations")
    # SUFFICIENT (should_refuse is False): require tightly aligned evidence.
    # INSUFFICIENT / unknown: do not force temporal support.
    if should_refuse is False:
        if nearest_lag_days is None or nearest_lag_days > SUFFICIENT_MAX_NEAREST_LAG_DAYS:
            reasons.append("insufficient_temporal_alignment")
        if within_2_day_count <= 0:
            reasons.append("no_within_2_day_evidence")
    return (not reasons), tuple(reasons)


def _fast_query_windowed(
    conn: sqlite3.Connection,
    *,
    plan_terms: tuple[str, ...],
    policy: str,
    target_date: str | None,
    build_id: str | None,
    candidate_depth: int,
) -> tuple[list[tuple], int, str, int | None]:
    """Mirror production temporal expansion with COUNT + LIMIT (no fetchall)."""
    windows: list[int | None] = list(TEMPORAL_WINDOW_DAYS) + [None]
    fts_table = "corpus_build_chunks_fts" if build_id is not None else "corpus_chunks_fts"

    def _from(window: int | None, match_sql: str | None) -> tuple[str, list[object]]:
        bounds = _window_bounds(target_date, window)
        clauses = ["c.chunk_id IS NOT NULL"]
        params: list[object] = []
        if match_sql is not None:
            if build_id is not None:
                clauses = [
                    f"{fts_table} MATCH ?",
                    "fts.build_id = ?",
                ]
                params = [match_sql, build_id]
            else:
                clauses = [f"{fts_table} MATCH ?"]
                params = [match_sql]
        if bounds is not None:
            clauses.append("date(c.available_at) BETWEEN ? AND ?")
            params.extend([bounds[0], bounds[1]])
        where = " AND ".join(clauses)
        if match_sql is not None:
            join = (
                f"FROM {fts_table} fts "
                f"JOIN tmp_audit_served c ON c.chunk_id = fts.chunk_id "
                f"WHERE {where}"
            )
            score = f"bm25({fts_table}) AS score"
        else:
            join = f"FROM tmp_audit_served c WHERE {where}"
            score = "NULL AS score"
        return join, params, score  # type: ignore[return-value]

    if policy == "temporal_window" or not plan_terms:
        for window in windows:
            join, params, score = _from(window, None)  # type: ignore[misc]
            # rebuild properly
            bounds = _window_bounds(target_date, window)
            clauses = ["1=1"]
            params = []
            if bounds is not None:
                clauses.append("date(c.available_at) BETWEEN ? AND ?")
                params.extend([bounds[0], bounds[1]])
            where = " AND ".join(clauses)
            count_sql = f"SELECT COUNT(*) FROM tmp_audit_served c WHERE {where}"
            matched = int(conn.execute(count_sql, params).fetchone()[0])
            if matched == 0:
                continue
            if target_date is None:
                order = "c.available_at DESC, c.chunk_id ASC"
                order_params: list[object] = []
            else:
                order = (
                    "ABS(julianday(date(c.available_at)) - julianday(?)) ASC, "
                    "c.chunk_id ASC"
                )
                order_params = [target_date]
            rows = conn.execute(
                f"SELECT c.chunk_id, c.available_at, c.source_class "
                f"FROM tmp_audit_served c WHERE {where} "
                f"ORDER BY {order} LIMIT ?",
                [*params, *order_params, candidate_depth],
            ).fetchall()
            return list(rows), matched, "temporal", window
        return [], 0, "none", None

    for window in windows:
        bounds = _window_bounds(target_date, window)
        window_clause = ""
        window_params: list[object] = []
        if bounds is not None:
            window_clause = " AND date(c.available_at) BETWEEN ? AND ?"
            window_params = [bounds[0], bounds[1]]
        if build_id is not None:
            from_clause = (
                f"FROM {fts_table} fts "
                f"JOIN tmp_audit_served c ON c.chunk_id = fts.chunk_id "
                f"WHERE {fts_table} MATCH ? AND fts.build_id = ?{window_clause}"
            )
            prefix = lambda match: [match, build_id, *window_params]
            score_expr = f"bm25({fts_table})"
        else:
            from_clause = (
                f"FROM {fts_table} fts "
                f"JOIN tmp_audit_served c ON c.chunk_id = fts.chunk_id "
                f"WHERE {fts_table} MATCH ?{window_clause}"
            )
            prefix = lambda match: [match, *window_params]
            score_expr = f"bm25({fts_table})"
        if target_date is None:
            order = f"{score_expr} ASC, c.chunk_id ASC"
            order_params = []
        else:
            order = (
                f"ABS(julianday(date(c.available_at)) - julianday(?)) ASC, "
                f"{score_expr} ASC, c.chunk_id ASC"
            )
            order_params = [target_date]
        count_sql = f"SELECT COUNT(*) {from_clause}"
        top_sql = (
            f"SELECT c.chunk_id, c.available_at, c.source_class, {score_expr} AS score "
            f"{from_clause} ORDER BY {order} LIMIT ?"
        )
        and_query = compile_match(plan_terms, mode="and")
        matched = int(conn.execute(count_sql, prefix(and_query)).fetchone()[0])
        if matched > 0:
            rows = conn.execute(
                top_sql, [*prefix(and_query), *order_params, candidate_depth]
            ).fetchall()
            return list(rows), matched, "AND", window
        if len(plan_terms) > 1:
            or_query = compile_match(plan_terms, mode="or")
            matched = int(conn.execute(count_sql, prefix(or_query)).fetchone()[0])
            if matched > 0:
                rows = conn.execute(
                    top_sql, [*prefix(or_query), *order_params, candidate_depth]
                ).fetchall()
                return list(rows), matched, "OR", window
    return [], 0, "none", None


def _fast_lexical_audit(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    ticker: str,
    cutoff: str,
    query: str,
    manifest_id: str,
    build_id: str | None,
    candidate_depth: int = 20,
    session_date: str | None = None,
    should_refuse: bool | None = None,
) -> LexicalCaseAudit:
    started = time.perf_counter()
    plan = plan_lexical_query(query, ticker)
    target, q_date, q_conflict, q_decision = _target_date(
        query, cutoff, session_date, plan.target_date,
    )
    _materialize_served(conn, manifest_id=manifest_id, cutoff=cutoff, ticker=ticker)
    rows, matched, mode, window = _fast_query_windowed(
        conn,
        plan_terms=plan.terms,
        policy=plan.policy,
        target_date=target,
        build_id=build_id,
        candidate_depth=candidate_depth,
    )
    top = tuple(
        LexicalTopChunk(chunk_id=row[0], available_at=row[1], source_class=row[2], rank=idx)
        for idx, row in enumerate(rows, start=1)
    )
    cutoff_violations = tuple(row.chunk_id for row in top if row.available_at > cutoff)
    ticker_violations = tuple(
        row.chunk_id for row in top
        if _served_chunk(
            conn, chunk_id=row.chunk_id, manifest_id=manifest_id, cutoff=cutoff, ticker=ticker
        ) is None
    )
    nearest, w2, w7, dist = _temporal_metrics(top, target)
    valid, reasons = _effect_validity(
        returned_count=len(top),
        cutoff_violations=cutoff_violations,
        ticker_violations=ticker_violations,
        nearest_lag_days=nearest,
        within_2_day_count=w2,
        should_refuse=should_refuse,
    )
    return LexicalCaseAudit(
        case_id=case_id, ticker=ticker, cutoff=cutoff, query=query,
        raw_terms=plan.raw_terms, content_terms=plan.content_terms,
        policy=plan.policy, match_mode=mode, matched_count=int(matched),
        returned_count=len(top), top_chunks=top,
        cutoff_violations=cutoff_violations, ticker_violations=ticker_violations,
        nearest_lag_days=nearest, within_2_day_count=w2, within_7_day_count=w7,
        source_class_distribution=dist, temporal_window_days=window,
        temporal_center_date=target, query_date=q_date,
        query_date_conflict=q_conflict, query_date_decision=q_decision,
        latency_ms=(time.perf_counter() - started) * 1000,
        should_refuse=should_refuse, effect_valid=valid, effect_reasons=reasons,
    )


def _production_retriever_audit(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    ticker: str,
    cutoff: str,
    query: str,
    manifest_id: str,
    candidate_depth: int = 20,
    session_date: str | None = None,
    should_refuse: bool | None = None,
) -> LexicalCaseAudit:
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    started = time.perf_counter()
    plan = plan_lexical_query(query, ticker)
    target, q_date, q_conflict, q_decision = _target_date(
        query, cutoff, session_date, plan.target_date,
    )
    result = retrieve_lexical(
        conn, query=query, ticker=ticker, cutoff=cutoff,
        requested_manifest_id=manifest_id, top_k=candidate_depth,
        candidate_depth=candidate_depth, include_trace=True,
        session_date=session_date,
    )
    top = tuple(
        LexicalTopChunk(
            chunk_id=item.chunk_id, available_at=item.available_at,
            source_class=item.source_class, rank=idx,
        )
        for idx, item in enumerate(result.results, start=1)
    )
    cutoff_violations = tuple(
        item.chunk_id for item in result.results if item.available_at > cutoff
    )
    ticker_violations = tuple(
        item.chunk_id for item in result.results
        if _served_chunk(
            conn, chunk_id=item.chunk_id, manifest_id=manifest_id,
            cutoff=cutoff, ticker=ticker,
        ) is None
    )
    # Prefer production-trace temporal center when present (parity surface).
    if result.trace is not None and result.trace.temporal_center_date:
        target = result.trace.temporal_center_date
        q_date = result.trace.query_date
        q_conflict = bool(result.trace.query_date_conflict)
        q_decision = result.trace.query_date_decision
    nearest, w2, w7, dist = _temporal_metrics(top, target)
    # Prefer executed match_mode/policy from production trace (never re-infer).
    if result.trace is not None and result.trace.match_mode is not None:
        mode = result.trace.match_mode
    else:
        mode = "none"
    policy = (
        result.trace.policy if result.trace is not None and result.trace.policy
        else plan.policy
    )
    window = result.trace.temporal_window_days if result.trace is not None else None
    matched = int(result.trace.matched_row_count) if result.trace else len(top)
    valid, reasons = _effect_validity(
        returned_count=len(top),
        cutoff_violations=cutoff_violations,
        ticker_violations=ticker_violations,
        nearest_lag_days=nearest,
        within_2_day_count=w2,
        should_refuse=should_refuse,
    )
    return LexicalCaseAudit(
        case_id=case_id, ticker=ticker, cutoff=cutoff, query=query,
        raw_terms=plan.raw_terms, content_terms=plan.content_terms,
        policy=policy, match_mode=mode, matched_count=matched,
        returned_count=len(top), top_chunks=top,
        cutoff_violations=cutoff_violations, ticker_violations=ticker_violations,
        nearest_lag_days=nearest, within_2_day_count=w2, within_7_day_count=w7,
        source_class_distribution=dist, temporal_window_days=window,
        temporal_center_date=target, query_date=q_date,
        query_date_conflict=q_conflict, query_date_decision=q_decision,
        latency_ms=(time.perf_counter() - started) * 1000,
        should_refuse=should_refuse, effect_valid=valid, effect_reasons=reasons,
    )


def _served_build_id(conn: sqlite3.Connection, manifest_id: str) -> str | None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lexical_index_state)")}
    if "lexical_generation_id" in columns:
        state = conn.execute(
            "SELECT lexical_generation_id FROM lexical_index_state WHERE singleton_id = 1"
        ).fetchone()
        generation = state[0] if state else None
        if generation:
            return generation
    build = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_build_chunks_fts'"
    ).fetchone()
    if build is not None:
        row = conn.execute(
            "SELECT build_id FROM corpus_build_chunks_fts LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    return None


def _case_should_refuse(case) -> bool | None:
    golden = getattr(case, "golden", None)
    if isinstance(golden, dict) and "should_refuse" in golden:
        return bool(golden["should_refuse"])
    return None


def _case_session_date(case) -> str | None:
    value = getattr(case, "session_date", None)
    return str(value) if value else None


def run_lexical_audit(
    *,
    db_path: Path,
    case_pack_path: Path,
    manifest_id: str,
    output_dir: Path,
    use_production_retriever: bool = False,
) -> dict:
    """Audit every case in the T4 case pack against the frozen corpus (read-only).

    Writes ``report.json`` under ``output_dir`` and returns the report dict.
    No model loading, no vector retrieval, no writes to the frozen DB.
    Exit-relevant summary flags: ``all_effect_valid`` drives process success.
    """
    from catalyst_eval.post_import.case_pack import load_case_pack

    cases = load_case_pack(case_pack_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    total_started = time.perf_counter()
    try:
        build_id = None if use_production_retriever else _served_build_id(conn, manifest_id)
        per_case: list[LexicalCaseAudit] = []
        for case in cases:
            kwargs = dict(
                case_id=case.case_id, ticker=case.ticker,
                cutoff=case.cutoff, query=case.query,
                manifest_id=manifest_id,
                session_date=_case_session_date(case),
                should_refuse=_case_should_refuse(case),
            )
            if use_production_retriever:
                audit = _production_retriever_audit(conn, **kwargs)
            else:
                audit = _fast_lexical_audit(conn, build_id=build_id, **kwargs)
            per_case.append(audit)
    finally:
        conn.close()

    total_ms = (time.perf_counter() - total_started) * 1000
    all_effect_valid = all(audit.effect_valid for audit in per_case)
    report = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "db_path": str(Path(db_path).resolve()),
        "case_pack_path": str(Path(case_pack_path).resolve()),
        "manifest_id": manifest_id,
        "use_production_retriever": use_production_retriever,
        "case_count": len(per_case),
        "all_non_empty": all(audit.returned_count > 0 for audit in per_case),
        "all_effect_valid": all_effect_valid,
        "cutoff_violations_total": sum(len(audit.cutoff_violations) for audit in per_case),
        "ticker_violations_total": sum(len(audit.ticker_violations) for audit in per_case),
        "total_latency_ms": total_ms,
        "sufficient_max_nearest_lag_days": SUFFICIENT_MAX_NEAREST_LAG_DAYS,
        "per_case": [asdict(audit) for audit in per_case],
    }
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return report


__all__ = [
    "AUDIT_SCHEMA_VERSION", "SUFFICIENT_MAX_NEAREST_LAG_DAYS",
    "LexicalCaseAudit", "LexicalTopChunk", "run_lexical_audit",
]
