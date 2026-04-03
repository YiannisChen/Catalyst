import pytest
from catalyst_data.pipeline.align import map_to_trade_date, compute_forward_returns


def test_premarket_news_maps_to_same_day():
    trading_days = ["2026-01-14", "2026-01-15", "2026-01-16"]
    result = map_to_trade_date("2026-01-15T08:00:00-05:00", trading_days)
    assert result == "2026-01-15"


def test_during_hours_maps_to_same_day():
    trading_days = ["2026-01-14", "2026-01-15", "2026-01-16"]
    result = map_to_trade_date("2026-01-15T12:00:00-05:00", trading_days)
    assert result == "2026-01-15"


def test_afterhours_news_maps_to_next_day():
    trading_days = ["2026-01-14", "2026-01-15", "2026-01-16"]
    result = map_to_trade_date("2026-01-15T18:00:00-05:00", trading_days)
    assert result == "2026-01-16"


def test_weekend_news_maps_to_monday():
    # Fri=16, Sat=17, Sun=18, Mon=19 — but only trading days in list
    trading_days = ["2026-01-16", "2026-01-19", "2026-01-20"]
    result = map_to_trade_date("2026-01-17T12:00:00-05:00", trading_days)  # Saturday
    assert result == "2026-01-19"  # next trading day (Monday)


def test_no_matching_trade_date_returns_none():
    trading_days = ["2026-01-10", "2026-01-11"]
    result = map_to_trade_date("2026-01-20T12:00:00-05:00", trading_days)
    assert result is None


def test_utc_timezone_converted_to_et():
    # 2026-01-15T21:00:00Z = 2026-01-15T16:00:00 ET (after market close)
    trading_days = ["2026-01-15", "2026-01-16"]
    result = map_to_trade_date("2026-01-15T21:00:00Z", trading_days)
    assert result == "2026-01-16"  # after-hours -> next day


def test_forward_returns():
    closes = {
        "2026-01-14": 150.0,
        "2026-01-15": 148.0,
        "2026-01-16": 152.0,
        "2026-01-17": 151.0,
        "2026-01-20": 155.0,
        "2026-01-21": 153.0,
    }
    dates_sorted = sorted(closes.keys())
    returns = compute_forward_returns("2026-01-15", closes, dates_sorted)
    assert returns["ret_t0"] == pytest.approx((148.0 - 150.0) / 150.0, abs=1e-6)
    assert returns["ret_t1"] == pytest.approx((152.0 - 148.0) / 148.0, abs=1e-6)


def test_forward_returns_missing_future_dates():
    closes = {"2026-01-14": 150.0, "2026-01-15": 148.0}
    dates_sorted = sorted(closes.keys())
    returns = compute_forward_returns("2026-01-15", closes, dates_sorted)
    assert returns["ret_t0"] == pytest.approx((148.0 - 150.0) / 150.0, abs=1e-6)
    assert returns["ret_t1"] is None  # no data for T+1
