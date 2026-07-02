"""Tests for FRED macro freshness — cadence-aware, mock-only."""
from __future__ import annotations

import sqlite3

from catalyst_data.freshness import macro_freshness
from catalyst_data.storage.sqlite import init_db, ensure_macro_tables, upsert_macro_observation


def _make_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_macro_tables(conn)
    # Trading calendar for watermark
    for dt in ("2026-06-30", "2026-07-01", "2026-07-02"):
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
            ("AAPL", dt),
        )
    conn.commit()
    # Disable FK so we can test without creating raw_assets entries
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


class TestMacroFreshness:
    def test_daily_series_stale(self, tmp_path):
        """DFF latest obs 3 days behind watermark (watermark=2026-07-02) → STALE."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        upsert_macro_observation(
            conn, series_id="DFF", observation_date="2026-06-29",
            value=4.83, released_at="2026-06-29", raw_asset_id="ra1",
        )

        report = macro_freshness(conn)
        dff = report["DFF"]
        assert dff["status"] == "STALE"
        assert dff["days_behind"] > 2
        conn.close()

    def test_daily_series_fresh(self, tmp_path):
        """DFF latest obs on watermark → FRESH."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        upsert_macro_observation(
            conn, series_id="DFF", observation_date="2026-07-01",
            value=4.83, released_at="2026-07-01", raw_asset_id="ra1",
        )

        report = macro_freshness(conn, watermark="2026-07-01")
        dff = report["DFF"]
        assert dff["status"] == "FRESH"
        conn.close()

    def test_monthly_series_fresh_within_threshold(self, tmp_path):
        """CPIAUCSL 20 days behind → FRESH (35-day threshold)."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        upsert_macro_observation(
            conn, series_id="CPIAUCSL", observation_date="2026-06-15",
            value=315.0, released_at="2026-06-15", raw_asset_id="ra1",
        )

        report = macro_freshness(conn, watermark="2026-07-01")
        cpi = report["CPIAUCSL"]
        assert cpi["status"] == "FRESH"
        assert cpi["days_behind"] < 35
        conn.close()

    def test_quarterly_series_fresh_within_threshold(self, tmp_path):
        """GDP 90 days behind → FRESH (100-day threshold)."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        upsert_macro_observation(
            conn, series_id="GDP", observation_date="2026-03-31",
            value=29000.0, released_at="2026-04-28", raw_asset_id="ra1",
        )

        report = macro_freshness(conn, watermark="2026-07-01")
        gdp = report["GDP"]
        assert gdp["status"] == "FRESH"
        conn.close()

    def test_no_data_series(self, tmp_path):
        """Series with no observations → NO_DATA."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        report = macro_freshness(conn)
        # UNRATE should be NO_DATA since no observations were added
        unr = report["UNRATE"]
        assert unr["status"] == "NO_DATA"
        conn.close()

    def test_all_12_series_reported(self, tmp_path):
        """Every curated series appears in the freshness report."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        report = macro_freshness(conn)
        assert len(report) == 12
        assert "DFF" in report
        assert "T10Y2Y" in report
        assert "TEDRATE" not in report
        conn.close()
