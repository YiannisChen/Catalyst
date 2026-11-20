"""The prepared-scope query, against a real slice of the production daily job store (10-26..10-29 movers/verify rows)."""
import shutil
import sqlite3
from pathlib import Path

import pytest
from catalyst_data.ingestion.ops import readiness

REAL = Path(__file__).parent.parent / "fixtures" / "real_update_jobs_slice.sqlite3"


def test_real_slice_lists_the_four_sessions_and_their_movers():
    assert readiness.prepared_sessions(REAL) == ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
    tickers = readiness.prepared_tickers(REAL, "2026-10-29")
    assert tickers[:3] == ["NOW", "GOOGL", "CAT"]  # largest absolute move first
    assert readiness.is_prepared(REAL, "now", "2026-10-29")
    assert not readiness.is_prepared(REAL, "AAPL", "2026-10-29")  # a real ticker, not a mover that day
    assert not readiness.is_prepared(REAL, "NOW", "2026-10-30")  # not yet verified: outside the first version


def test_a_session_without_a_passed_verify_is_not_offered(tmp_path):
    copy = tmp_path / "jobs.sqlite3"
    shutil.copy(REAL, copy)
    with sqlite3.connect(copy) as conn:
        conn.execute("UPDATE job_items SET status='FAILED' WHERE stage='verify' AND session_date='2026-10-28'")
    assert "2026-10-28" not in readiness.prepared_sessions(copy)
    assert readiness.prepared_tickers(copy, "2026-10-28") == []


def test_the_store_is_opened_read_only(tmp_path):
    copy = tmp_path / "jobs.sqlite3"
    shutil.copy(REAL, copy)
    before = copy.read_bytes()
    readiness.prepared_pairs(copy)
    assert copy.read_bytes() == before
    with pytest.raises(FileNotFoundError):
        readiness.prepared_pairs(tmp_path / "missing.sqlite3")
