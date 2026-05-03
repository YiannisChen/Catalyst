from __future__ import annotations

from catalyst_data.connectors.base import FetchResult
from scripts.backfill import PolygonIncidentMonitor, sources_for_date


def test_sources_for_date_skips_market_sources_on_weekend():
    sources = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals", "fred_macro"]
    assert sources_for_date(sources, "2026-05-02") == []


def test_sources_for_date_keeps_all_sources_on_weekday():
    sources = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals", "fred_macro"]
    assert sources_for_date(sources, "2026-05-01") == sources


def test_polygon_monitor_triggers_after_poor_rolling_window():
    rotated = {"called": False}

    def build_polygon_fetcher(api_key: str) -> None:
        rotated["called"] = api_key == "backup-key"

    monitor = PolygonIncidentMonitor(
        build_polygon_fetcher=build_polygon_fetcher,
        backup_api_key="backup-key",
    )
    for _ in range(11):
        monitor.record(FetchResult(status=500, error="boom"))
    for _ in range(9):
        monitor.record(FetchResult(status=200))

    assert monitor.triggered is True
    assert monitor.rotated_to_backup is True
    assert rotated["called"] is True


def test_polygon_monitor_triggers_on_long_429_backoff_without_backup():
    monitor = PolygonIncidentMonitor(
        build_polygon_fetcher=lambda api_key: None,
        backup_api_key=None,
    )
    monitor.record(FetchResult(status=429, retry_after_seconds=301))

    assert monitor.triggered is True
    assert monitor.rotated_to_backup is False
