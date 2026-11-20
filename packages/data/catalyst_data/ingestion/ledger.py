"""Append-only intake attempt ledger and negative-cache policy."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from catalyst_data.trading_calendar import calendar_trading_days

LEDGER_POLICY_VERSION = "m9_intake_v1"
EMPTY_RETRY_SESSIONS = 2
STATUSES = ("OK", "EMPTY", "ERROR", "DISABLED")

_CREATE_TABLE = """CREATE TABLE intake_log (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT,
    session_date TEXT,
    source TEXT,
    policy_version TEXT,
    attempted_at TEXT,
    headlines INTEGER,
    bodies INTEGER,
    status TEXT
)"""

_ATTEMPT_INDEX = """CREATE INDEX IF NOT EXISTS intake_log_attempt_order
    ON intake_log(subject, session_date, source, policy_version, attempted_at DESC, attempt_id DESC)"""


def ensure_ledger(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='intake_log'"
    ).fetchone()
    if exists is None:
        conn.execute(_CREATE_TABLE)
        conn.execute(_ATTEMPT_INDEX)
        return

    columns = {row[1] for row in conn.execute("PRAGMA table_info(intake_log)")}
    if "attempt_id" not in columns:
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("SAVEPOINT migrate_intake_log_attempt_identity")
        try:
            conn.execute("ALTER TABLE intake_log RENAME TO intake_log_legacy")
            conn.execute(_CREATE_TABLE)
            conn.execute(
                """INSERT INTO intake_log
                   (subject, session_date, source, policy_version, attempted_at,
                    headlines, bodies, status)
                   SELECT subject, session_date, source, policy_version, attempted_at,
                          headlines, bodies, status
                   FROM intake_log_legacy"""
            )
            conn.execute("DROP TABLE intake_log_legacy")
            conn.execute(_ATTEMPT_INDEX)
            conn.execute("RELEASE SAVEPOINT migrate_intake_log_attempt_identity")
            if owns_transaction:
                conn.commit()
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_intake_log_attempt_identity")
            conn.execute("RELEASE SAVEPOINT migrate_intake_log_attempt_identity")
            if owns_transaction:
                conn.rollback()
            raise
    else:
        conn.execute(_ATTEMPT_INDEX)


def record_attempt(
    conn: sqlite3.Connection,
    *,
    subject: str,
    session_date: str,
    source: str,
    status: str,
    headlines: int,
    bodies: int,
    attempted_at: str | None = None,
    policy_version: str = LEDGER_POLICY_VERSION,
    commit: bool = True,
) -> None:
    if status not in STATUSES:
        raise ValueError(f"invalid intake status: {status}")
    if attempted_at is None:
        attempted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO intake_log
        (subject, session_date, source, policy_version, attempted_at, headlines, bodies, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (subject, session_date, source, policy_version, attempted_at, headlines, bodies, status),
    )
    if commit:
        conn.commit()


def completed_attempt_after(
    conn: sqlite3.Connection,
    subject: str,
    session_date: str,
    source: str,
    cutoff: str,
    *,
    latest_only: bool = False,
) -> bool:
    """Whether a completed attempt happened after cutoff.

    Most sources retain the historical any-success rule. SEC callers can opt into
    the latest attempt because a newly discovered index-time filing may reopen a
    session that previously had a successful intake.
    """
    try:
        if latest_only:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(intake_log)")}
            tie_breaker = "attempt_id" if "attempt_id" in columns else "rowid"
            attempt = conn.execute(
                f"""SELECT attempted_at, status FROM intake_log
                WHERE subject = ? AND session_date = ? AND source = ? AND policy_version = ?
                ORDER BY attempted_at DESC, {tie_breaker} DESC LIMIT 1""",
                (subject, session_date, source, LEDGER_POLICY_VERSION),
            ).fetchone()
            return bool(attempt and attempt[1] in ("OK", "EMPTY") and attempt[0] > cutoff)
        return conn.execute(
            """SELECT 1 FROM intake_log
            WHERE subject = ? AND session_date = ? AND source = ? AND policy_version = ?
              AND status IN ('OK', 'EMPTY') AND attempted_at > ?
            LIMIT 1""",
            (subject, session_date, source, LEDGER_POLICY_VERSION, cutoff),
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def negative_cache_hit(
    conn: sqlite3.Connection,
    *,
    subject: str,
    session_date: str,
    source: str,
    now: str | None = None,
) -> bool:
    del now  # Age is defined by the persisted attempt date, not the current wall clock.
    try:
        attempt = conn.execute(
            """SELECT attempted_at, status FROM intake_log
            WHERE subject = ? AND session_date = ? AND source = ? AND policy_version = ?
            ORDER BY attempted_at DESC, attempt_id DESC LIMIT 1""",
            (subject, session_date, source, LEDGER_POLICY_VERSION),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if attempt is None or attempt[1] != "EMPTY":
        return False
    attempt_date = attempt[0][:10]
    try:
        trading_days = calendar_trading_days(session_date, attempt_date)
    except (ValueError, TypeError):
        return False
    # The calendar helper includes both endpoints; the session itself is excluded.
    return len([day for day in trading_days if day > session_date]) >= EMPTY_RETRY_SESSIONS
