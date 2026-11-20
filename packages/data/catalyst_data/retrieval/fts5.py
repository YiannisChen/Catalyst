"""Cutoff-safe lexical retrieval with FTS5 and deterministic SQL fallback."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

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
    *,
    build_id: str | None = None,
    window_start_at: str | None = None,
    allow_late_updates: bool = False,
    has_provider_updated_at: bool = True,
) -> tuple[str, list[object]]:
    """Eligibility SQL over the served relation or one exact inactive build.

    ``build_id`` selects the pointer-free per-build relation
    (``corpus_build_chunks c``) and binds ``c.build_id`` instead of the
    manifest-scoped ``c.manifest_id``. ``cutoff`` is the active retrieval
    phase's endpoint, including for expanded evidence windows, and always
    bounds Alpaca article updates. Omitting ``build_id`` keeps the unchanged
    active served-manifest predicate.
    """
    if build_id is None:
        scope_sql = "c.manifest_id = ?"
        scope_params: list[object] = [requested_manifest_id]
    else:
        scope_sql = "c.build_id = ?"
        scope_params = [build_id]
    status_placeholders = ",".join("?" for _ in SEARCHABLE_STATUSES)
    sql = (
        f"{scope_sql} AND c.status IN ({status_placeholders}) "
        "AND c.eligibility = 'eligible' AND c.available_at <= ? "
        "AND EXISTS (SELECT 1 FROM json_each(c.ticker_associations) t "
        "WHERE t.value = ?)"
    )
    params: list[object] = [*scope_params, *SEARCHABLE_STATUSES, cutoff, ticker]
    if has_provider_updated_at:
        sql += (
            " AND (c.document_id NOT LIKE 'alpaca!_news:%' ESCAPE '!' "
            "OR (c.provider_updated_at IS NOT NULL AND c.provider_updated_at <= ?))"
        )
        params.append(cutoff)
    else:
        # Older served schemas cannot prove when an Alpaca article was updated.
        sql += " AND c.document_id NOT LIKE 'alpaca!_news:%' ESCAPE '!'"
    if window_start_at is not None:
        sql += " AND c.available_at >= ?"
        params.append(window_start_at)
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


def _inactive_build_scope(
    conn: sqlite3.Connection,
    requested_manifest_id: str,
    build_id: str,
) -> tuple[int, str]:
    """Validate one inactive candidate build and return (row_count, digest).

    Pointer-free candidate lexical retrieval may only read from an exact
    inactive build that is frozen at ``lexical_ready`` for the requested
    corpus manifest.  Wrong build, wrong manifest, a current/promoted
    manifest, a non-ready build, a missing per-build FTS table, an empty
    generation, and stored-digest inconsistency all fail closed.
    """
    if re.fullmatch(r"[0-9a-f]{64}", build_id) is None:
        raise RetrievalContractError("invalid_build_id")
    row = conn.execute(
        """SELECT manifest_id, status, lexical_ready, lexical_row_count,
                  lexical_digest, lexical_expected_digest
           FROM corpus_publication_builds WHERE build_id=?""",
        (build_id,),
    ).fetchone()
    if row is None:
        raise RetrievalContractError("inactive_build_not_found")
    build_manifest_id = str(row[0] or "")
    status = str(row[1] or "")
    lexical_ready = int(row[2] or 0)
    row_count = int(row[3] or 0)
    lexical_digest = str(row[4] or "")
    expected_digest = str(row[5] or "")
    if build_manifest_id != requested_manifest_id:
        raise RetrievalContractError("inactive_build_manifest_mismatch")
    if status != "lexical_ready" or lexical_ready != 1:
        raise RetrievalContractError("inactive_build_not_ready")
    manifest_row = conn.execute(
        "SELECT is_current FROM corpus_manifest WHERE manifest_id=?",
        (requested_manifest_id,),
    ).fetchone()
    if manifest_row is None:
        raise RetrievalContractError("manifest_not_found")
    if int(manifest_row[0] or 0) != 0:
        raise RetrievalContractError("inactive_build_manifest_current")
    fts_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='corpus_build_chunks_fts'"
    ).fetchone()
    if fts_exists is None:
        raise RetrievalContractError("inactive_build_fts_unavailable")
    if row_count < 1:
        raise RetrievalContractError("inactive_build_fts_empty")
    if not lexical_digest:
        raise RetrievalContractError("inactive_build_fts_digest_missing")
    if expected_digest and expected_digest != lexical_digest:
        raise RetrievalContractError("inactive_build_fts_digest_mismatch")
    return row_count, lexical_digest


def _update_lexical_digest(digest: Any, chunk_id: object, content_text: object) -> None:
    """Canonical per-row lexical digest (must match the FTS builder)."""
    digest.update(
        json.dumps(
            [str(chunk_id), str(content_text)],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(b"\n")


def _inactive_build_source_digest(
    conn: sqlite3.Connection, build_id: str
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cursor = conn.execute(
        """SELECT chunk_id, content_text FROM corpus_build_chunks
           WHERE build_id=? AND eligibility='eligible'
             AND status IN ('active','pending_embedding','embedded','metadata_only')
           ORDER BY chunk_id COLLATE BINARY""",
        (build_id,),
    )
    while True:
        rows = cursor.fetchmany(500)
        if not rows:
            break
        for chunk_id, content_text in rows:
            _update_lexical_digest(digest, chunk_id, content_text)
            count += 1
    return count, digest.hexdigest()


def _inactive_build_persisted_digest(
    conn: sqlite3.Connection, build_id: str
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cursor = conn.execute(
        """SELECT chunk_id, content_text FROM corpus_build_chunks_fts
           WHERE build_id=? ORDER BY chunk_id COLLATE BINARY""",
        (build_id,),
    )
    while True:
        rows = cursor.fetchmany(500)
        if not rows:
            break
        for chunk_id, content_text in rows:
            _update_lexical_digest(digest, chunk_id, content_text)
            count += 1
    return count, digest.hexdigest()


def verify_inactive_lexical_build(
    conn: sqlite3.Connection,
    *,
    requested_manifest_id: str,
    build_id: str,
) -> None:
    """Read-only full verification of one inactive candidate FTS generation.

    Runs the same source/persisted count and canonical-digest parity checks
    the FTS builder enforces at build time, plus the inactive-build scope
    invariants.  Intended to be called once before a candidate evidence pool
    starts; it never writes and never touches active serving state.
    """
    row_count, lexical_digest = _inactive_build_scope(
        conn, requested_manifest_id, build_id
    )
    source_count, source_digest = _inactive_build_source_digest(conn, build_id)
    if source_count != row_count or source_digest != lexical_digest:
        raise RetrievalContractError("inactive_build_fts_count_digest_mismatch")
    persisted_count, persisted_digest = _inactive_build_persisted_digest(
        conn, build_id
    )
    if persisted_count != row_count or persisted_digest != lexical_digest:
        raise RetrievalContractError("inactive_build_fts_count_digest_mismatch")


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
    end_exclusive = (center + timedelta(days=window_days + 1)).isoformat()
    # available_at is stored as UTC 'YYYY-MM-DDTHH:MM:SSZ', so the plain string
    # range selects exactly the same rows as date() and lets SQLite use the
    # available_at index; the date() form is kept as the defining predicate.
    return (
        " AND c.available_at >= ? AND c.available_at < ?"
        " AND date(c.available_at) BETWEEN ? AND ?",
        [start, end_exclusive, start, end],
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
    build_scoped_join: bool = False,
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
        if build_scoped_join:
            join_sql = (
                "ON c.build_id = fts.build_id AND c.chunk_id = fts.chunk_id"
            )
        else:
            join_sql = "ON c.chunk_id = fts.chunk_id"
        base = f"""
            FROM corpus_build_chunks_fts fts
            JOIN {chunks_relation} c {join_sql}
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


#: Sidecar tables of the served-build projection (WP-B B7a,
#: ``catalyst_data.retrieval.served_projection``).
_PROJECTION_CHUNKS = "served_projection_chunks"
_PROJECTION_FTS_MAP = "served_projection_fts_map"
#: rowids per ``rowid IN (...)`` probe, well under SQLite's variable limit.
_WINDOW_ROWID_BATCH = 500


class _ProjectionRoute:
    """Per-call routing of the served lexical path onto the projection sidecar.

    Holds the caller's live sidecar connection (TEMP tables are per
    connection) and a per-call cache ``match_expr -> TEMP hits table`` so the
    phase-1 MATCH runs once per distinct expression across window steps.
    """

    def __init__(
        self, served: sqlite3.Connection, handle: Any, sidecar: sqlite3.Connection
    ) -> None:
        self.served = served
        self.handle = handle
        self.conn = sidecar
        self._hits: dict[str, str] = {}
        self._window_hits: dict[tuple, str] = {}

    def hits_table(self, match_expr: str) -> str:
        name = self._hits.get(match_expr)
        if name is not None:
            return name
        name = f"b7a_hits_{len(self._hits)}"
        # Phase 1: rowid + bm25 only. No UNINDEXED column is read or filtered,
        # so no FTS content row is touched; the build filter is applied
        # through the projection's fts_map (B7a §2.2).
        rows = self.served.execute(
            "SELECT rowid, bm25(corpus_build_chunks_fts) FROM corpus_build_chunks_fts "
            "WHERE corpus_build_chunks_fts MATCH ?",
            (match_expr,),
        ).fetchall()
        self.conn.execute(
            f"CREATE TEMP TABLE {name} (fts_rowid INTEGER PRIMARY KEY, bm25_score REAL)"
        )
        self.conn.executemany(f"INSERT INTO temp.{name} VALUES (?, ?)", rows)
        self._hits[match_expr] = name
        return name

    def window_hits_table(
        self,
        match_expr: str,
        *,
        lexical_generation_id: str,
        eligibility_sql: str,
        eligibility_params: list[object],
        window_sql: str,
        window_params: list[object],
    ) -> str:
        """MATCH rows for one dated window only, same rowids and bm25 values.

        A broad OR expression matches hundreds of thousands of rows across the
        whole corpus, while one ticker's eligible chunks in a few-day window
        are tens to hundreds. The window's FTS rowids come from the projection
        first, and MATCH is evaluated only on them. bm25 uses whole-index
        statistics, so each score equals the unrestricted one; the phase-3 join
        applies the same predicates, so its result is unchanged.
        """
        key = (match_expr, lexical_generation_id, eligibility_sql, window_sql,
               tuple(eligibility_params), tuple(window_params))
        name = self._window_hits.get(key)
        if name is not None:
            return name
        name = f"b7a_whits_{len(self._window_hits)}"
        rowids = [
            int(row[0])
            for row in self.conn.execute(
                f"SELECT DISTINCT m.fts_rowid FROM {_PROJECTION_CHUNKS} c "
                f"JOIN {_PROJECTION_FTS_MAP} m ON m.chunk_id = c.chunk_id "
                f"WHERE m.build_id = ? AND {eligibility_sql}{window_sql}",
                [lexical_generation_id, *eligibility_params, *window_params],
            )
        ]
        rows: list[tuple] = []
        for start in range(0, len(rowids), _WINDOW_ROWID_BATCH):
            batch = rowids[start:start + _WINDOW_ROWID_BATCH]
            rows.extend(
                self.served.execute(
                    "SELECT rowid, bm25(corpus_build_chunks_fts) FROM corpus_build_chunks_fts "
                    "WHERE corpus_build_chunks_fts MATCH ? "
                    f"AND rowid IN ({','.join('?' for _ in batch)})",
                    (match_expr, *batch),
                ).fetchall()
            )
        self.conn.execute(
            f"CREATE TEMP TABLE {name} (fts_rowid INTEGER PRIMARY KEY, bm25_score REAL)"
        )
        self.conn.executemany(f"INSERT INTO temp.{name} VALUES (?, ?)", rows)
        self._window_hits[key] = name
        return name

    def close(self) -> None:
        for name in [*self._hits.values(), *self._window_hits.values()]:
            self.conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
        self._hits.clear()
        self._window_hits.clear()


def _require_projection_is_served(
    conn: sqlite3.Connection,
    *,
    handle: Any,
    requested_manifest_id: str,
    chunks_relation: str,
) -> None:
    """Fail closed unless the projection is exactly what this call serves.

    The served build is resolved on the call's own connection (the one
    published, lexical_ready generation of the current manifest), FTS must be
    the served mode, and build, generation and manifest must equal the
    projection's. Any mismatch or resolver failure is ``served_projection_stale``;
    the call never falls back to the unprojected or SQL-fallback path.
    """
    from .lexical_retriever import resolve_served_build

    stale = RetrievalContractError(
        "served_projection_stale",
        "the served projection is not the currently served build",
    )
    try:
        resolution = resolve_served_build(conn, expected_manifest_id=requested_manifest_id)
    except RetrievalContractError as exc:
        raise stale from exc
    mode_served, _, served_generation = _served_mode(conn, requested_manifest_id)
    if (
        chunks_relation != "corpus_served_chunks"
        or mode_served != "fts5"
        or served_generation is None
        or str(served_generation) != str(handle.build_id)
        or resolution.build_id != str(handle.build_id)
        or resolution.corpus_manifest_id != str(handle.manifest_id)
    ):
        raise stale


#: Counts over the projection sidecar, which never changes once published:
#: (sidecar path, sql, params) -> count. Each count is a full scan of the
#: projection, and one run repeats the same few dozens of times.
_PROJECTION_COUNTS: dict[tuple, int] = {}
_PROJECTION_COUNTS_MAX = 4096


def _projection_count(
    route: "_ProjectionRoute | None", conn: sqlite3.Connection, sql: str, params: list[object]
) -> int:
    if route is None:
        return int(conn.execute(sql, params).fetchone()[0])
    key = (str(getattr(route.handle, "path", id(route.handle))), sql, tuple(params))
    cached = _PROJECTION_COUNTS.get(key)
    if cached is not None:
        return cached
    value = int(conn.execute(sql, params).fetchone()[0])
    if len(_PROJECTION_COUNTS) >= _PROJECTION_COUNTS_MAX:
        _PROJECTION_COUNTS.clear()
    _PROJECTION_COUNTS[key] = value
    return value


def _run_projected_match(
    route: _ProjectionRoute,
    *,
    match_expr: str,
    lexical_generation_id: str,
    eligibility_sql: str,
    eligibility_params: list[object],
    window_sql: str,
    window_params: list[object],
    order_sql: str,
    order_params: list[object],
    candidate_depth: int,
) -> tuple[list[tuple], int]:
    """Phase 3 of the MATCH bridge: the served FTS join, on the projection.

    Equivalent to ``_fts_from_clause`` with ``lexical_generation_id`` set:
    ``fts JOIN view c ON c.chunk_id = fts.chunk_id WHERE MATCH ? AND
    fts.build_id = ?`` — the hits are the MATCH rows, ``fts_map`` carries
    ``fts.build_id``/``fts.chunk_id`` by FTS rowid.
    """
    hits = (
        route.window_hits_table(
            match_expr,
            lexical_generation_id=lexical_generation_id,
            eligibility_sql=eligibility_sql,
            eligibility_params=eligibility_params,
            window_sql=window_sql,
            window_params=window_params,
        )
        if window_sql
        else route.hits_table(match_expr)
    )
    base = (
        f" FROM temp.{hits} h "
        f"JOIN {_PROJECTION_FTS_MAP} m ON m.fts_rowid = h.fts_rowid "
        f"JOIN {_PROJECTION_CHUNKS} c ON c.chunk_id = m.chunk_id "
        f"WHERE m.build_id = ? AND {eligibility_sql}{window_sql}"
    )
    base_params = [lexical_generation_id, *eligibility_params, *window_params]
    matched = int(route.conn.execute(f"SELECT COUNT(*){base}", base_params).fetchone()[0])
    if matched == 0:
        return [], 0
    rows = route.conn.execute(
        "SELECT c.chunk_id, c.document_id, c.available_at, c.source_class, "
        f"h.bm25_score AS score, c.content_text{base} ORDER BY {order_sql} LIMIT ?",
        [*base_params, *order_params, candidate_depth],
    ).fetchall()
    return list(rows), matched


def _hydrate_projected_content(
    conn: sqlite3.Connection, *, build_id: str, rows: list[tuple]
) -> list[tuple]:
    """Re-read ``content_text`` from the served build by PK, keeping row order."""
    ids = [row[0] for row in rows]
    content: dict[str, str] = {}
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        for chunk_id, text in conn.execute(
            "SELECT chunk_id, content_text FROM corpus_build_chunks "
            f"WHERE build_id = ? AND chunk_id IN ({','.join('?' for _ in batch)})",
            [build_id, *batch],
        ):
            content[chunk_id] = text
    missing = [chunk_id for chunk_id in ids if chunk_id not in content]
    if missing:
        raise RetrievalContractError(
            "served_projection_content_missing",
            f"{len(missing)} projected chunks have no served content row",
        )
    return [(*row[:5], content[row[0]]) for row in rows]


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
    build_scoped_join: bool = False,
    route: _ProjectionRoute | None = None,
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
                conn if route is None else route.conn,
                chunks_relation=(
                    chunks_relation if route is None else _PROJECTION_CHUNKS
                ),
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

    def _match(
        conn: sqlite3.Connection,
        *,
        match_expr: str,
        select_sql: str,
        count_sql: str,
        params_prefix: list[object],
        eligibility_params: list[object],
        window_sql: str,
        window_params: list[object],
        order_sql: str,
        order_params: list[object],
        candidate_depth: int,
    ) -> tuple[list[tuple], int]:
        if route is None:
            return _run_fts_match(
                conn,
                match_expr=match_expr,
                select_sql=select_sql,
                count_sql=count_sql,
                params_prefix=params_prefix,
                eligibility_params=eligibility_params,
                window_params=window_params,
                order_params=order_params,
                candidate_depth=candidate_depth,
            )
        return _run_projected_match(
            route,
            match_expr=match_expr,
            lexical_generation_id=str(lexical_generation_id),
            eligibility_sql=eligibility_sql,
            eligibility_params=eligibility_params,
            window_sql=window_sql,
            window_params=window_params,
            order_sql=order_sql,
            order_params=order_params,
            candidate_depth=candidate_depth,
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
            build_scoped_join=build_scoped_join,
        )
        and_query = compile_match(plan.terms, mode="and")
        rows, matched = _match(
            conn,
            match_expr=and_query,
            select_sql=select_sql,
            count_sql=count_sql,
            params_prefix=params_prefix,
            eligibility_params=eligibility_params,
            window_sql=window_sql,
            window_params=window_params,
            order_sql=order_sql,
            order_params=order_params,
            candidate_depth=candidate_depth,
        )
        if matched > 0:
            return rows, matched, "AND", plan.policy, window, center
        if len(plan.terms) > 1:
            or_query = compile_match(plan.terms, mode="or")
            rows, matched = _match(
                conn,
                match_expr=or_query,
                select_sql=select_sql,
                count_sql=count_sql,
                params_prefix=params_prefix,
                eligibility_params=eligibility_params,
                window_sql=window_sql,
                window_params=window_params,
                order_sql=order_sql,
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
    inactive_build_id: str | None = None,
    served_projection: tuple[Any, sqlite3.Connection] | None = None,
    window_start_at: str | None = None,
    allow_late_updates: bool = False,
) -> RetrievalResultSet:
    """Retrieve lexical results for the requested corpus manifest.

    When ``inactive_build_id`` is supplied the query is bound to that exact
    inactive candidate build's per-build FTS rows (pointer-free; it never
    touches active serving state or ``lexical_index_state``).  When omitted,
    the unchanged active served-manifest path is used.

    ``served_projection`` is ``(ServedProjectionHandle, live sidecar
    connection)`` (WP-B B7a). When given, the eligibility counts, the
    temporal-only path and the MATCH join run on the verified projection
    sidecar and ``content_text`` is re-read by primary key; results are
    identical to the unprojected path. It never combines with
    ``inactive_build_id``, and a projection that is not the served build
    fails closed (``served_projection_stale``).

    ``allow_late_updates`` remains accepted for compatibility but does not
    widen eligibility by itself. ``cutoff`` is always the active window's
    endpoint; expanded callers must pass their expanded endpoint there.
    """
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
    if served_projection is not None and inactive_build_id is not None:
        raise RetrievalContractError(
            "served_projection_with_inactive_build",
            "the served projection is only for the active served build",
        )
    route: _ProjectionRoute | None = None
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

        build_scope = inactive_build_id is not None
        if build_scope:
            _inactive_build_scope(conn, requested_manifest_id, inactive_build_id)
            chunks_relation = "corpus_build_chunks"
        else:
            chunks_relation = served_chunks_relation(conn)
        if served_projection is not None:
            handle, sidecar = served_projection
            _require_projection_is_served(
                conn,
                handle=handle,
                requested_manifest_id=requested_manifest_id,
                chunks_relation=chunks_relation,
            )
            route = _ProjectionRoute(conn, handle, sidecar)
        count_conn = conn if route is None else route.conn
        count_relation = chunks_relation if route is None else _PROJECTION_CHUNKS
        relation_columns = {
            row[1] for row in count_conn.execute(f"PRAGMA table_info({count_relation})")
        }

        eligibility_sql, eligibility_params = _eligibility_predicate(
            requested_manifest_id, cutoff, ticker, source_classes, evidence_types,
            build_id=inactive_build_id if build_scope else None,
            window_start_at=window_start_at,
            allow_late_updates=allow_late_updates,
            has_provider_updated_at="provider_updated_at" in relation_columns,
        )
        if build_scope:
            manifest_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM corpus_build_chunks WHERE build_id=?",
                    (inactive_build_id,),
                ).fetchone()[0]
            )
        else:
            manifest_count = _projection_count(
                route, count_conn,
                f"SELECT COUNT(*) FROM {count_relation} WHERE manifest_id = ?",
                [requested_manifest_id],
            )
        eligible_count = _projection_count(
            route, count_conn,
            f"SELECT COUNT(*) FROM {count_relation} c WHERE {eligibility_sql}",
            list(eligibility_params),
        )
        if build_scope:
            mode_served = "fts5"
            degradation_reason = None
            lexical_generation_id = inactive_build_id
        else:
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
                    build_scoped_join=build_scope,
                    route=route,
                )
            )
            if match_mode == "none" and not plan_lexical_query(query, ticker).terms:
                # Distinguish empty structured-only query that still has terms empty
                # after stripping (should not happen for temporal_window).
                pass
            raw_candidates = [tuple(row) for row in rows]
            if route is not None and raw_candidates:
                raw_candidates = _hydrate_projected_content(
                    conn, build_id=str(route.handle.build_id), rows=raw_candidates
                )
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
    finally:
        if route is not None:
            route.close()

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
    "verify_inactive_lexical_build",
]
