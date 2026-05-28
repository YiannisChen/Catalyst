from __future__ import annotations

import json

from catalyst_data.connectors.base import FetchResult
from scripts.backfill import (
    PolygonIncidentMonitor,
    build_run_notes,
    explicit_calendar_days,
    sources_for_date,
)


def test_sources_for_date_skips_market_sources_on_weekend():
    sources = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals", "fred_macro"]
    assert sources_for_date(sources, "2026-05-02") == []


def test_sources_for_date_keeps_all_sources_on_weekday():
    sources = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals", "fred_macro"]
    assert sources_for_date(sources, "2026-05-01") == sources


def test_explicit_calendar_days_is_inclusive():
    assert explicit_calendar_days("2024-12-30", "2025-01-02") == [
        "2024-12-30",
        "2024-12-31",
        "2025-01-01",
        "2025-01-02",
    ]


def test_build_run_notes_records_provider_and_rate_assumptions(monkeypatch):
    monkeypatch.setattr("scripts.backfill.api_key_id", lambda provider: f"{provider}-key-id")

    payload = json.loads(
        build_run_notes(
            tickers=["NVDA"],
            sources=["polygon_news", "fred_macro"],
            start_date="2024-12-30",
            end_date="2025-05-01",
        )
    )

    assert payload["date_window"] == {"start": "2024-12-30", "end": "2025-05-01"}
    assert payload["sources"] == ["polygon_news", "fred_macro"]
    assert payload["api_key_ids"]["polygon"] == "polygon-key-id"
    assert "polygon" in payload["rate_policies"]
    assert "fred" in payload["rate_policies"]


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
