from __future__ import annotations

import sqlite3

import pytest
from catalyst_data.ingestion.ledger import (
    LEDGER_POLICY_VERSION,
    completed_attempt_after,
    ensure_ledger,
    negative_cache_hit,
    record_attempt,
)

SESSION = "2026-09-14"  # Monday; ordinary trading week, no market holiday.


def _conn():
    conn = sqlite3.connect(":memory:")
    ensure_ledger(conn)
    return conn


def _record(conn, status, attempted_at, *, session_date=SESSION):
    record_attempt(
        conn,
        subject="ABC",
        session_date=session_date,
        source="news",
        status=status,
        headlines=0,
        bodies=0,
        attempted_at=attempted_at,
    )


def test_empty_one_trading_day_after_session_is_not_cached():
    conn = _conn()
    _record(conn, "EMPTY", "2026-09-15T12:00:00Z")
    assert not negative_cache_hit(conn, subject="ABC", session_date=SESSION, source="news")


def test_empty_three_trading_days_after_session_is_cached():
    conn = _conn()
    _record(conn, "EMPTY", "2026-09-17T12:00:00Z")
    assert negative_cache_hit(conn, subject="ABC", session_date=SESSION, source="news")


def test_error_never_hits_even_when_old():
    conn = _conn()
    _record(conn, "ERROR", "2026-09-28T12:00:00Z")
    assert not negative_cache_hit(conn, subject="ABC", session_date=SESSION, source="news")


def test_latest_attempt_status_wins():
    conn = _conn()
    _record(conn, "EMPTY", "2026-09-28T12:00:00Z")
    _record(conn, "OK", "2026-09-29T12:00:00Z")
    assert not negative_cache_hit(conn, subject="ABC", session_date=SESSION, source="news")

    other_session = "2026-09-15"
    record_attempt(
        conn, subject="ABC", session_date=other_session, source="news", status="OK",
        headlines=0, bodies=0, attempted_at="2026-09-28T12:00:00Z",
    )
    record_attempt(
        conn, subject="ABC", session_date=other_session, source="news", status="EMPTY",
        headlines=0, bodies=0, attempted_at="2026-09-29T12:00:00Z",
    )
    assert negative_cache_hit(conn, subject="ABC", session_date=other_session, source="news")


def test_other_policy_version_is_ignored():
    conn = _conn()
    conn.execute(
        "INSERT INTO intake_log "
        "(subject, session_date, source, policy_version, attempted_at, headlines, bodies, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("ABC", SESSION, "news", "old_policy", "2026-09-28T12:00:00Z", 0, 0, "EMPTY"),
    )
    conn.commit()
    assert not negative_cache_hit(conn, subject="ABC", session_date=SESSION, source="news")


def test_same_second_attempts_keep_both_rows_and_latest_attempt_order():
    conn = _conn()
    attempted_at = "2026-09-28T12:00:00Z"
    _record(conn, "ERROR", attempted_at)
    _record(conn, "OK", attempted_at)

    assert conn.execute("SELECT COUNT(*) FROM intake_log").fetchone()[0] == 2
    assert not negative_cache_hit(conn, subject="ABC", session_date=SESSION, source="news")


def test_latest_attempt_completion_is_opt_in_and_preserves_other_source_semantics():
    conn = _conn()
    record_attempt(conn, subject="ABC", session_date=SESSION, source="sec", status="OK",
                   headlines=1, bodies=1, attempted_at="2026-09-15T12:00:00Z")
    record_attempt(conn, subject="ABC", session_date=SESSION, source="sec", status="ERROR",
                   headlines=0, bodies=0, attempted_at="2026-09-16T12:00:00Z")
    record_attempt(conn, subject="ABC", session_date=SESSION, source="news", status="OK",
                   headlines=1, bodies=1, attempted_at="2026-09-15T12:00:00Z")
    record_attempt(conn, subject="ABC", session_date=SESSION, source="news", status="ERROR",
                   headlines=0, bodies=0, attempted_at="2026-09-16T12:00:00Z")

    assert completed_attempt_after(conn, "ABC", SESSION, "sec", "2026-09-14T20:00:00Z")
    assert not completed_attempt_after(
        conn, "ABC", SESSION, "sec", "2026-09-14T20:00:00Z", latest_only=True
    )
    assert completed_attempt_after(conn, "ABC", SESSION, "news", "2026-09-14T20:00:00Z")


def test_latest_attempt_completion_reads_legacy_ledger_without_attempt_id():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE intake_log (
        subject TEXT, session_date TEXT, source TEXT, policy_version TEXT,
        attempted_at TEXT, headlines INTEGER, bodies INTEGER, status TEXT,
        PRIMARY KEY(subject, session_date, source, policy_version, attempted_at))""")
    conn.executemany(
        "INSERT INTO intake_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("ABC", SESSION, "sec", LEDGER_POLICY_VERSION, "2026-09-15T12:00:00Z", 0, 0, "OK"),
            ("ABC", SESSION, "sec", LEDGER_POLICY_VERSION, "2026-09-16T12:00:00Z", 0, 0, "ERROR"),
        ],
    )

    assert not completed_attempt_after(
        conn, "ABC", SESSION, "sec", "2026-09-14T20:00:00Z", latest_only=True
    )


def test_ensure_ledger_migrates_existing_composite_primary_key_schema():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE intake_log (
        subject TEXT, session_date TEXT, source TEXT, policy_version TEXT,
        attempted_at TEXT, headlines INTEGER, bodies INTEGER, status TEXT,
        PRIMARY KEY(subject, session_date, source, policy_version, attempted_at))""")
    conn.execute(
        "INSERT INTO intake_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("ABC", SESSION, "news", LEDGER_POLICY_VERSION, "2026-09-28T12:00:00Z", 0, 0, "EMPTY"),
    )
    conn.commit()

    ensure_ledger(conn)
    _record(conn, "OK", "2026-09-28T12:00:00Z")

    rows = conn.execute("SELECT status FROM intake_log ORDER BY attempt_id").fetchall()
    assert rows == [("EMPTY",), ("OK",)]
    assert LEDGER_POLICY_VERSION != "old_policy"


def test_invalid_status_raises():
    conn = _conn()
    with pytest.raises(ValueError):
        _record(conn, "UNKNOWN", "2026-09-15T12:00:00Z")


def test_missing_table_is_a_cache_miss():
    conn = sqlite3.connect(":memory:")
    assert not negative_cache_hit(conn, subject="ABC", session_date=SESSION, source="news")
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'intake_log'"
    ).fetchone() is None
