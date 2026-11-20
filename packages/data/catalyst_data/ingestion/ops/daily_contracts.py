"""Shared policy and ledger queries for the daily ingestion stages."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_data import trading_calendar
from catalyst_data.storage.connect import connect_readonly

SEC_INTAKE_POLICY_VERSION = "sec_paged_v1"


def completed_sec_intake(bodies_db: Path) -> set[tuple[str, str, str]]:
    """SEC sessions whose latest paginated-policy attempt after close succeeded."""
    if not Path(bodies_db).exists():
        return set()
    with connect_readonly(bodies_db, immutable=False, timeout_ms=10_000) as conn:
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(intake_log)")}
            tie_breaker = "attempt_id" if "attempt_id" in columns else "rowid"
            rows = conn.execute(
                f"""SELECT subject, session_date, source, attempted_at, status FROM (
                       SELECT subject, session_date, source, attempted_at, status,
                              ROW_NUMBER() OVER (
                                  PARTITION BY subject, session_date, source
                                  ORDER BY attempted_at DESC, {tie_breaker} DESC
                              ) AS attempt_rank
                       FROM intake_log
                       WHERE policy_version=? AND source='sec'
                   ) WHERE attempt_rank=1 AND status IN ('OK','EMPTY')""",
                (SEC_INTAKE_POLICY_VERSION,),
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
    completed = set()
    for subject, session_date, source, attempted_at, _status in rows:
        try:
            if attempted_at > trading_calendar.session_close_utc(session_date):
                completed.add((subject, session_date, source))
        except ValueError:
            continue
    return completed
