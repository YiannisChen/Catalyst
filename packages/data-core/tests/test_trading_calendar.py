"""Tests for shared trading calendar provider."""
from __future__ import annotations

import sqlite3

from catalyst_data.storage.sqlite import init_db


def test_calendar_trading_days_excludes_weekends_and_holidays():
    from catalyst_data.trading_calendar import calendar_trading_days

    assert calendar_trading_days("2026-07-02", "2026-07-06") == [
        "2026-07-02",
        "2026-07-06",
    ]


def test_calendar_trading_days_from_after_to_is_empty():
    from catalyst_data.trading_calendar import calendar_trading_days

    assert calendar_trading_days("2026-07-06", "2026-07-02") == []


def test_trading_days_for_window_unions_ohlcv_with_calendar(tmp_path):
    from catalyst_data.trading_calendar import trading_days_for_window

    db_path = tmp_path / "calendar.db"
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES "
        "('AAPL', '2026-06-29', 100.0)"
    )
    conn.commit()

    days = trading_days_for_window(conn, "2026-06-29", "2026-07-07")
    conn.close()

    assert "2026-06-29" in days
    assert "2026-07-06" in days
    assert "2026-07-07" in days
    assert "2026-07-03" not in days
