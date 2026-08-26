"""M3-3: SEC fail-closed eligibility tests (execution-lock §D; Batch A A3/A4/A5).

Accepted time is authoritative; otherwise the row fails closed and never uses
filing date as an after-close shortcut. Valid date-only ``filed_at`` with no
accepted time fails closed as ``fail_closed_no_time_of_day`` /
``unknown_time_of_day``; missing/unusable ``filed_at`` fails closed as
``fail_closed_no_accepted_time`` / ``unknown``. Under the operator-approved
latest-plausible-instant rule the conservative instant is used instead.
Persistence is result-authoritative and rejects contradictory arguments
without writing.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.sec.eligible_at import (
    EligibleAtResult,
    derive_eligible_at,
    latest_plausible_instant,
    persist_filing_temporal_repair,
)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _filing_row(**overrides):
    row = {
        "filing_id": "filing-8k-0000320193-26-000001",
        "cik": "0000320193",
        "filed_at": "2026-01-05",
        "form_type": "8-K",
    }
    row.update(overrides)
    return row


def _filing_sqlite_row(**overrides) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ["filing_id", "cik", "filed_at", "form_type"]
    values = [
        overrides.get("filing_id", "filing-8k-0000320193-26-000001"),
        overrides.get("cik", "0000320193"),
        overrides.get("filed_at", "2026-01-05"),
        overrides.get("form_type", "8-K"),
    ]
    conn.execute(
        f"CREATE TABLE t ({', '.join(cols)})"
    )
    conn.execute(
        f"INSERT INTO t VALUES ({', '.join('?' for _ in cols)})", values
    )
    return conn.execute("SELECT * FROM t").fetchone()


def _tuesday_window() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-06T14:30:00Z"),
        session_close_at=_utc("2026-01-06T21:00:00Z"),
        information_window_start_at=_utc("2026-01-05T21:00:00Z"),  # Monday close
        cutoff_at=_utc("2026-01-06T21:00:00Z"),  # Tuesday close
    )


def _monday_window() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-05",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-05T14:30:00Z"),
        session_close_at=_utc("2026-01-05T21:00:00Z"),
        information_window_start_at=_utc("2026-01-02T21:00:00Z"),  # Friday close
        cutoff_at=_utc("2026-01-05T21:00:00Z"),
    )


def test_monday_1605_earnings_eligible_for_tuesday_window():
    result = derive_eligible_at(
        _filing_row(), accepted_time=_utc("2026-01-05T21:05:00Z")
    )
    assert result.accepted_time_recovered is True
    assert result.fail_closed is False
    assert result.eligible_at_reason == "accepted_time_recovered"
    assert result.temporal_precision == "accepted_time"
    assert _tuesday_window().contains(result.eligible_at)


def test_after_close_friday_eligible_for_monday_window():
    # Friday 2026-01-02 16:05 ET = 21:05Z -> Monday window starts Friday close.
    result = derive_eligible_at(
        _filing_row(filed_at="2026-01-02"), accepted_time=_utc("2026-01-02T21:05:00Z")
    )
    assert result.eligible_at == _utc("2026-01-02T21:05:00Z")
    assert _monday_window().contains(result.eligible_at)


def test_premarket_accepted_eligible_for_same_session_window():
    result = derive_eligible_at(
        _filing_row(), accepted_time=_utc("2026-01-05T14:05:00Z")
    )
    assert _monday_window().contains(result.eligible_at)


def test_after_cutoff_accepted_excluded_from_session():
    # Tuesday 16:05 ET = 21:05Z is after Tuesday's 21:00Z cutoff.
    result = derive_eligible_at(
        _filing_row(filed_at="2026-01-06"), accepted_time=_utc("2026-01-06T21:05:00Z")
    )
    assert _tuesday_window().contains(result.eligible_at) is False
    # Eligible for the next (Wednesday) window instead.
    wednesday = TemporalIdentity(
        session_date="2026-01-07",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-07T14:30:00Z"),
        session_close_at=_utc("2026-01-07T21:00:00Z"),
        information_window_start_at=_utc("2026-01-06T21:00:00Z"),
        cutoff_at=_utc("2026-01-07T21:00:00Z"),
    )
    assert wednesday.contains(result.eligible_at)


def test_fail_closed_date_only_never_uses_filed_at_as_after_close_shortcut():
    """Valid date-only filed_at + no accepted_time + no approval -> §D case 3."""
    result = derive_eligible_at(_filing_row(), accepted_time=None)
    assert result.eligible_at is None
    assert result.fail_closed is True
    assert result.accepted_time_recovered is False
    assert result.eligible_at_reason == "fail_closed_no_time_of_day"
    assert result.temporal_precision == "unknown_time_of_day"


def test_fail_closed_date_only_from_sqlite_row():
    result = derive_eligible_at(_filing_sqlite_row(), accepted_time=None)
    assert result.eligible_at is None
    assert result.fail_closed is True
    assert result.eligible_at_reason == "fail_closed_no_time_of_day"
    assert result.temporal_precision == "unknown_time_of_day"


def test_fail_closed_naive_accepted_time():
    """Naive accepted_time is never treated as UTC (case 2)."""
    result = derive_eligible_at(
        _filing_row(), accepted_time=datetime(2026, 1, 5, 21, 5, 0)
    )
    assert result.eligible_at is None
    assert result.eligible_at_reason == "fail_closed_no_accepted_time"
    assert result.temporal_precision == "unknown"
    assert result.accepted_time_recovered is False
    assert result.fail_closed is True


@pytest.mark.parametrize(
    "filed_at",
    ["malformed", "", "2026-01-05T16:00:00Z", "2026-13-40", "2026-1-5"],
)
def test_malformed_filed_at_fails_closed_without_approval(filed_at):
    result = derive_eligible_at(
        _filing_row(filed_at=filed_at), accepted_time=None
    )
    assert result.eligible_at is None
    assert result.fail_closed is True
    assert result.accepted_time_recovered is False
    assert result.eligible_at_reason == "fail_closed_no_accepted_time"
    assert result.temporal_precision == "unknown"


@pytest.mark.parametrize(
    "filed_at",
    ["malformed", "", "2026-01-05T16:00:00Z", "2026-13-40", "2026-1-5"],
)
def test_malformed_filed_at_fails_closed_with_approval(filed_at):
    """Approval never rescues a malformed/timestamp-shaped filed_at (case 5)."""
    result = derive_eligible_at(
        _filing_row(filed_at=filed_at), accepted_time=None,
        approve_latest_plausible=True,
    )
    assert result.eligible_at is None
    assert result.fail_closed is True
    assert result.eligible_at_reason == "fail_closed_no_accepted_time"
    assert result.temporal_precision == "unknown"


def test_malformed_filed_at_no_raise_for_non_mapping_row():
    result = derive_eligible_at(None, accepted_time=None)
    assert result.eligible_at is None
    assert result.fail_closed is True
    assert result.eligible_at_reason == "fail_closed_no_accepted_time"


def test_weekend_filing_fails_closed_without_approval():
    # Saturday filing: accepted_time absent -> fail closed, filed_at not used.
    result = derive_eligible_at(
        _filing_row(filed_at="2026-01-03"), accepted_time=None
    )
    assert result.eligible_at is None
    assert result.fail_closed is True
    assert result.eligible_at_reason == "fail_closed_no_time_of_day"


def test_holiday_filing_fails_closed_without_approval():
    result = derive_eligible_at(
        _filing_row(filed_at="2026-01-01"), accepted_time=None
    )
    assert result.eligible_at is None
    assert result.fail_closed is True
    assert result.eligible_at_reason == "fail_closed_no_time_of_day"


def test_latest_plausible_instant_est():
    # 2026-01-05 is EST (UTC-5): next-day 00:00 ET = 05:00Z.
    assert latest_plausible_instant("2026-01-05") == _utc("2026-01-06T05:00:00Z")


def test_latest_plausible_instant_edt():
    # 2026-05-01 is EDT (UTC-4): next-day 00:00 ET = 04:00Z.
    assert latest_plausible_instant("2026-05-01") == _utc("2026-05-02T04:00:00Z")


def test_latest_plausible_instant_after_monday_close_before_tuesday_cutoff():
    # Monday date-only rule lands after Monday 16:00 ET close and before Tuesday close.
    eligible = latest_plausible_instant("2026-01-05")
    assert eligible == _utc("2026-01-06T05:00:00Z")
    assert _tuesday_window().contains(eligible)


def test_approved_latest_plausible_rule_sets_precision_and_reason():
    result = derive_eligible_at(
        _filing_row(), accepted_time=None, approve_latest_plausible=True
    )
    assert result.eligible_at == _utc("2026-01-06T05:00:00Z")
    assert result.eligible_at_reason == "latest_plausible_approved"
    assert result.temporal_precision == "date_only_latest_plausible"
    assert result.fail_closed is False


def test_approved_latest_plausible_from_sqlite_row():
    result = derive_eligible_at(
        _filing_sqlite_row(), accepted_time=None, approve_latest_plausible=True
    )
    assert result.eligible_at == _utc("2026-01-06T05:00:00Z")
    assert result.eligible_at_reason == "latest_plausible_approved"


def _db_with_filing() -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    from catalyst_data.storage.sqlite import upsert_filing

    upsert_filing(
        conn,
        filing_id="filing-8k-0000320193-26-000001",
        cik="0000320193",
        ticker="AAPL",
        form_type="8-K",
        filed_at="2026-01-05",
        accession_number="0000320193-26-000001",
        primary_document="a.htm",
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/a.htm",
    )
    return conn


def _read_filing(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM filings WHERE filing_id=?",
        ("filing-8k-0000320193-26-000001",),
    ).fetchone()


def test_persist_filing_temporal_repair_writes_repair_columns():
    conn = _db_with_filing()
    result = derive_eligible_at(
        _filing_row(), accepted_time=_utc("2026-01-05T21:05:00Z")
    )
    persist_filing_temporal_repair(
        conn,
        filing_id="filing-8k-0000320193-26-000001",
        result=result,
        accepted_time=_utc("2026-01-05T21:05:00Z"),
    )
    row = _read_filing(conn)
    assert row["accepted_time_utc"] == "2026-01-05T21:05:00Z"
    assert row["eligible_at"] == "2026-01-05T21:05:00Z"
    assert row["eligible_at_reason"] == "accepted_time_recovered"
    assert row["temporal_precision"] == "accepted_time"
    assert row["accepted_time_recovered"] == 1
    assert row["eligibility_fail_closed"] == 0
    # M3-5B can read the persisted repair values.
    readback = conn.execute(
        "SELECT eligible_at, temporal_precision, accepted_time_recovered FROM filings"
    ).fetchone()
    assert readback["eligible_at"] == "2026-01-05T21:05:00Z"
    conn.close()


def test_persist_recovered_pair_identical_utc_z():
    """Recovered pair persists the same UTC Z to accepted_time_utc and eligible_at."""
    conn = _db_with_filing()
    accepted = _utc("2026-01-05T21:05:00Z")
    result = derive_eligible_at(_filing_row(), accepted_time=accepted)
    persist_filing_temporal_repair(
        conn,
        filing_id="filing-8k-0000320193-26-000001",
        result=result,
        accepted_time=accepted,
    )
    row = _read_filing(conn)
    assert row["accepted_time_utc"] == row["eligible_at"] == "2026-01-05T21:05:00Z"
    conn.close()


def test_persist_filing_temporal_repair_fail_closed_row():
    conn = _db_with_filing()
    result = derive_eligible_at(_filing_row(), accepted_time=None)
    persist_filing_temporal_repair(
        conn,
        filing_id="filing-8k-0000320193-26-000001",
        result=result,
        accepted_time=None,
    )
    row = _read_filing(conn)
    assert row["eligible_at"] is None
    assert row["accepted_time_utc"] is None
    assert row["eligible_at_reason"] == "fail_closed_no_time_of_day"
    assert row["temporal_precision"] == "unknown_time_of_day"
    assert row["accepted_time_recovered"] == 0
    assert row["eligibility_fail_closed"] == 1
    conn.close()


def test_persist_approved_latest_plausible_writes_eligible_at_only():
    conn = _db_with_filing()
    result = derive_eligible_at(
        _filing_row(), accepted_time=None, approve_latest_plausible=True
    )
    persist_filing_temporal_repair(
        conn,
        filing_id="filing-8k-0000320193-26-000001",
        result=result,
        accepted_time=None,
    )
    row = _read_filing(conn)
    assert row["accepted_time_utc"] is None
    assert row["eligible_at"] == "2026-01-06T05:00:00Z"
    assert row["eligible_at_reason"] == "latest_plausible_approved"
    assert row["temporal_precision"] == "date_only_latest_plausible"
    assert row["accepted_time_recovered"] == 0
    assert row["eligibility_fail_closed"] == 0
    conn.close()


def test_persist_unknown_filing_id_fails_closed():
    conn = _db_with_filing()
    result = derive_eligible_at(_filing_row(), accepted_time=_utc("2026-01-05T21:05:00Z"))
    with pytest.raises(ValueError, match="filing"):
        persist_filing_temporal_repair(
            conn,
            filing_id="filing-missing-00000000",
            result=result,
            accepted_time=_utc("2026-01-05T21:05:00Z"),
        )
    conn.close()


def test_persist_fail_closed_with_non_none_accepted_time_raises_and_does_not_write():
    conn = _db_with_filing()
    result = derive_eligible_at(_filing_row(), accepted_time=None)
    with pytest.raises(ValueError, match="accepted_time"):
        persist_filing_temporal_repair(
            conn,
            filing_id="filing-8k-0000320193-26-000001",
            result=result,
            accepted_time=_utc("2026-01-05T21:05:00Z"),
        )
    row = _read_filing(conn)
    assert row["eligible_at_reason"] is None  # nothing written
    assert row["accepted_time_utc"] is None
    conn.close()


def test_persist_recovered_with_none_accepted_time_raises_and_does_not_write():
    conn = _db_with_filing()
    result = derive_eligible_at(
        _filing_row(), accepted_time=_utc("2026-01-05T21:05:00Z")
    )
    with pytest.raises(ValueError, match="accepted_time"):
        persist_filing_temporal_repair(
            conn,
            filing_id="filing-8k-0000320193-26-000001",
            result=result,
            accepted_time=None,
        )
    row = _read_filing(conn)
    assert row["eligible_at_reason"] is None  # nothing written
    conn.close()


def test_persist_recovered_with_mismatching_accepted_time_raises_and_does_not_write():
    conn = _db_with_filing()
    result = derive_eligible_at(
        _filing_row(), accepted_time=_utc("2026-01-05T21:05:00Z")
    )
    with pytest.raises(ValueError, match="accepted_time"):
        persist_filing_temporal_repair(
            conn,
            filing_id="filing-8k-0000320193-26-000001",
            result=result,
            accepted_time=_utc("2026-01-05T22:00:00Z"),
        )
    row = _read_filing(conn)
    assert row["eligible_at_reason"] is None  # nothing written
    conn.close()


def test_persist_fail_closed_with_eligible_at_raises_and_does_not_write():
    conn = _db_with_filing()
    result = EligibleAtResult(
        eligible_at=_utc("2026-01-06T05:00:00Z"),
        eligible_at_reason="fail_closed_no_time_of_day",
        temporal_precision="unknown_time_of_day",
        accepted_time_recovered=False,
        fail_closed=True,
    )
    with pytest.raises(ValueError, match="eligible_at"):
        persist_filing_temporal_repair(
            conn,
            filing_id="filing-8k-0000320193-26-000001",
            result=result,
            accepted_time=None,
        )
    row = _read_filing(conn)
    assert row["eligible_at"] is None  # nothing written
    conn.close()
