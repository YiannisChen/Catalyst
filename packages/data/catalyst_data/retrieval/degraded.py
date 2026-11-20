"""Deterministic SQL fallback for lexical retrieval (AMEND-5.1 policy).

The fallback mirrors the FTS5 arm: content terms are optional, not mandatory.
It first prefers rows containing every content term within expanding temporal
windows; when none exist it ranks rows containing at least one content term.
Low-information (ticker/date-only) queries use pure temporal ranking — never
an unbounded full-history generic event-term OR.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .query_policy import TEMPORAL_WINDOW_DAYS


def _window_bounds(target_date: str | None, window_days: int | None) -> tuple[str, str] | None:
    if target_date is None or window_days is None:
        return None
    center = date.fromisoformat(target_date)
    start = (center - timedelta(days=window_days)).isoformat()
    end = (center + timedelta(days=window_days)).isoformat()
    return start, end


def _lag_key(available_at: str, target_date: str | None) -> tuple[float, str]:
    if target_date is None:
        return (0.0, available_at)
    try:
        avail = date.fromisoformat(available_at[:10])
        target = date.fromisoformat(target_date)
        return (float(abs((avail - target).days)), available_at)
    except ValueError:
        return (1e9, available_at)


def rank_sql_fallback(
    conn: sqlite3.Connection,
    *,
    eligibility_sql: str,
    eligibility_params: list[object],
    terms: tuple[str, ...],
    candidate_depth: int,
    chunks_relation: str = "corpus_chunks",
    target_date: str | None = None,
    policy: str = "content",
) -> tuple[list[tuple], int, str, int | None]:
    """Return (rows, matched_count, match_mode, window_days).

    Avoids unbounded Python materialization of the full eligible set when a
    temporal window is active: COUNT + ordered LIMIT push work into SQLite.
    Content matching still requires scanning the windowed set (no FTS), but
    windows keep that set bounded for attribution queries.
    """
    windows: list[int | None] = list(TEMPORAL_WINDOW_DAYS) + [None]

    if policy == "temporal_window" or not terms:
        for window in windows:
            bounds = _window_bounds(target_date, window)
            window_sql = ""
            window_params: list[object] = []
            if bounds is not None:
                window_sql = " AND date(c.available_at) BETWEEN ? AND ?"
                window_params = [bounds[0], bounds[1]]
            count_sql = (
                f"SELECT COUNT(*) FROM {chunks_relation} c "
                f"WHERE {eligibility_sql}{window_sql}"
            )
            matched = int(
                conn.execute(
                    count_sql, [*eligibility_params, *window_params]
                ).fetchone()[0]
            )
            if matched == 0:
                continue
            if target_date is None:
                order_sql = "c.available_at DESC, c.chunk_id ASC"
                order_params: list[object] = []
            else:
                order_sql = (
                    "ABS(julianday(date(c.available_at)) - julianday(?)) ASC, "
                    "c.chunk_id ASC"
                )
                order_params = [target_date]
            rows = conn.execute(
                f"""SELECT c.chunk_id, c.document_id, c.available_at, c.source_class,
                           c.content_text
                    FROM {chunks_relation} c
                    WHERE {eligibility_sql}{window_sql}
                    ORDER BY {order_sql}
                    LIMIT ?""",
                [*eligibility_params, *window_params, *order_params, candidate_depth],
            ).fetchall()
            return list(rows), matched, "temporal", window
        return [], 0, "none", None

    for window in windows:
        bounds = _window_bounds(target_date, window)
        window_sql = ""
        window_params: list[object] = []
        if bounds is not None:
            window_sql = " AND date(c.available_at) BETWEEN ? AND ?"
            window_params = [bounds[0], bounds[1]]
        # Windowed fetch — still limited to the temporal slice, not full history.
        rows = conn.execute(
            f"""SELECT c.chunk_id, c.document_id, c.available_at, c.source_class,
                       c.content_text
                FROM {chunks_relation} c
                WHERE {eligibility_sql}{window_sql}
                ORDER BY c.chunk_id ASC""",
            [*eligibility_params, *window_params],
        ).fetchall()
        all_match: list[tuple[tuple, tuple]] = []
        any_match: list[tuple[tuple, tuple]] = []
        for row in rows:
            content = row[4].casefold()
            lag = _lag_key(row[2], target_date)
            freq = sum(content.count(term) for term in terms)
            # Sort key: temporal lag ASC, term frequency DESC, chunk_id ASC.
            sort_key = (lag[0], -freq, row[0])
            if all(term in content for term in terms):
                all_match.append((sort_key, row))
            elif any(term in content for term in terms):
                any_match.append((sort_key, row))
        if all_match:
            all_match.sort(key=lambda item: item[0])
            scored = [row for _, row in all_match]
            return scored[:candidate_depth], len(scored), "AND", window
        if any_match:
            any_match.sort(key=lambda item: item[0])
            scored = [row for _, row in any_match]
            return scored[:candidate_depth], len(scored), "OR", window
    return [], 0, "none", None
