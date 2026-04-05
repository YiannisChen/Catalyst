"""Integration tests for the orchestrator module."""

from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.connectors.base import FetchResult
from catalyst_data.orchestrator import process_request
from catalyst_data.storage.sqlite import compute_asset_id, get_clean_asset, init_db


async def mock_fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
    if endpoint == "news":
        return FetchResult(
            status=200,
            data={
                "results": [
                    {
                        "title": "Test Article",
                        "published_utc": "2026-01-15T10:00:00Z",
                        "article_url": "https://example.com",
                        "description": "Test content",
                        "publisher": {"name": "Reuters"},
                        "tickers": ["AAPL"],
                    }
                ]
            },
            latency_ms=10.0,
            source_label="polygon:news",
        )
    if endpoint in ("income_statement", "balance_sheet", "cash_flow"):
        return FetchResult(
            status=200,
            data=[{"revenue": 100}],
            latency_ms=10.0,
            source_label=f"fmp:{endpoint}",
        )
    return FetchResult(
        status=200,
        data={"value": "5.50"},
        latency_ms=10.0,
        source_label=f"other:{endpoint}",
    )


@pytest.mark.asyncio
async def test_process_request_stores_bronze_and_silver():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        conn=conn,
        fetch_fn=mock_fetch,
    )
    assert len(results) >= 1
    assert results[0]["ok"] is True
    assert results[0]["asset_id"] is not None

    # Check Bronze
    raw = conn.execute("SELECT * FROM raw_assets").fetchall()
    assert len(raw) >= 1

    # Check Silver
    clean = conn.execute("SELECT * FROM clean_assets").fetchall()
    assert len(clean) >= 1

    conn.close()


@pytest.mark.asyncio
async def test_process_request_handles_failed_fetch():
    async def failing_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        return FetchResult(
            status=500, error="Server error", latency_ms=10.0, source_label="test"
        )

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        conn=conn,
        fetch_fn=failing_fetch,
    )
    # Should return results with error info, not crash
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert results[0]["error"] is not None

    conn.close()


@pytest.mark.asyncio
async def test_process_request_multiple_sources():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news", "fmp_fundamentals"],
        conn=conn,
        fetch_fn=mock_fetch,
    )
    assert len(results) >= 2

    # Both should succeed
    for r in results:
        assert r["ok"] is True

    # Should have Bronze and Silver for both
    raw_count = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
    clean_count = conn.execute("SELECT COUNT(*) FROM clean_assets").fetchone()[0]
    assert raw_count >= 2
    assert clean_count >= 2

    conn.close()


@pytest.mark.asyncio
async def test_process_request_yfinance_fundamentals_source_maps_correctly():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["yfinance_fundamentals"],
        conn=conn,
        fetch_fn=mock_fetch,
    )
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["asset_id"] is not None
    conn.close()


@pytest.mark.asyncio
async def test_process_request_partial_failure():
    """One source fails, the other succeeds -- both results are returned."""

    call_count = 0

    async def partial_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        nonlocal call_count
        call_count += 1
        if endpoint == "news":
            return FetchResult(
                status=500, error="Boom", latency_ms=1.0, source_label="test"
            )
        return FetchResult(
            status=200,
            data=[{"revenue": 42}],
            latency_ms=1.0,
            source_label=f"fmp:{endpoint}",
        )

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news", "fmp_fundamentals"],
        conn=conn,
        fetch_fn=partial_fetch,
    )
    assert len(results) == 2

    news_result = next(r for r in results if r["source"] == "polygon_news")
    fmp_result = next(r for r in results if r["source"] == "fmp_fundamentals")

    assert news_result["ok"] is False
    assert fmp_result["ok"] is True

    conn.close()


@pytest.mark.asyncio
async def test_process_request_reports_failed_endpoints_on_partial_success():
    async def partial_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        if endpoint == "cash_flow":
            return FetchResult(
                status=503,
                error="FMP 503: upstream unavailable",
                latency_ms=1.0,
                source_label="fmp:cash_flow",
            )
        return FetchResult(
            status=200,
            data=[{"value": endpoint}],
            latency_ms=1.0,
            source_label=f"fmp:{endpoint}",
        )

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["fmp_fundamentals"],
        conn=conn,
        fetch_fn=partial_fetch,
    )

    assert len(results) == 1
    summary = results[0]
    assert summary["ok"] is True
    assert summary["failed_endpoints"] == {
        "cash_flow": {
            "status": 503,
            "error": "FMP 503: upstream unavailable",
            "source_label": "fmp:cash_flow",
        }
    }
    assert summary["endpoint_statuses"]["income_statement"] == 200
    assert summary["endpoint_statuses"]["cash_flow"] == 503
    conn.close()


@pytest.mark.asyncio
async def test_process_request_reports_fetch_exceptions_per_source():
    async def exploding_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        raise RuntimeError(f"boom for {endpoint}")

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        conn=conn,
        fetch_fn=exploding_fetch,
    )

    assert len(results) == 1
    summary = results[0]
    assert summary["ok"] is False
    assert summary["error"] == "ingest: No endpoint returned data"
    assert summary["failed_endpoints"] == {
        "news": {
            "status": 0,
            "error": "boom for news",
            "source_label": "news",
        }
    }
    assert summary["endpoint_statuses"] == {"news": 0}
    conn.close()


@pytest.mark.asyncio
async def test_process_request_handles_malformed_news_results_dict():
    async def malformed_news_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        return FetchResult(
            status=200,
            data={
                "results": {
                    "published_utc": "2026-01-15T10:00:00Z",
                    "description": "Shape drifted to a single object",
                    "article_url": "https://example.com/drift",
                }
            },
            latency_ms=1.0,
            source_label="polygon:news",
        )

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        conn=conn,
        fetch_fn=malformed_news_fetch,
    )

    assert len(results) == 1
    summary = results[0]
    assert summary["ok"] is True
    asset_id = compute_asset_id("AAPL", "2026-01-15", "polygon_news")
    clean_asset = get_clean_asset(conn, asset_id)
    assert clean_asset is not None
    assert "Untitled" in clean_asset["content_md"]
    assert "Shape drifted to a single object" in clean_asset["content_md"]
    conn.close()


@pytest.mark.asyncio
async def test_process_request_handles_string_publisher_shape():
    async def malformed_news_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        return FetchResult(
            status=200,
            data={
                "results": [
                    {
                        "published_utc": "2026-01-15T10:00:00Z",
                        "publisher": "Reuters",
                        "description": "Publisher drifted to a string",
                    }
                ]
            },
            latency_ms=1.0,
            source_label="polygon:news",
        )

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        conn=conn,
        fetch_fn=malformed_news_fetch,
    )

    assert len(results) == 1
    summary = results[0]
    assert summary["ok"] is True
    asset_id = compute_asset_id("AAPL", "2026-01-15", "polygon_news")
    clean_asset = get_clean_asset(conn, asset_id)
    assert clean_asset is not None
    assert "Untitled" in clean_asset["content_md"]
    assert "Reuters" in clean_asset["content_md"]
    assert "Publisher drifted to a string" in clean_asset["content_md"]
    conn.close()


@pytest.mark.asyncio
async def test_process_request_rerun_keeps_single_bronze_and_silver_row():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    first_results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        conn=conn,
        fetch_fn=mock_fetch,
    )
    second_results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        conn=conn,
        fetch_fn=mock_fetch,
    )

    asset_id = compute_asset_id("AAPL", "2026-01-15", "polygon_news")
    assert first_results[0]["asset_id"] == asset_id
    assert second_results[0]["asset_id"] == asset_id
    raw_count = conn.execute(
        "SELECT COUNT(*) FROM raw_assets WHERE asset_id = ?", (asset_id,)
    ).fetchone()[0]
    clean_count = conn.execute(
        "SELECT COUNT(*) FROM clean_assets WHERE asset_id = ?", (asset_id,)
    ).fetchone()[0]
    assert raw_count == 1
    assert clean_count == 1
    conn.close()


@pytest.mark.asyncio
async def test_process_request_partial_failure_rerun_does_not_corrupt_prior_success():
    async def failing_news_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        return FetchResult(
            status=503,
            error="Polygon 503: upstream unavailable",
            latency_ms=1.0,
            source_label="polygon:news",
        )

    conn = sqlite3.connect(":memory:")
    init_db(conn)

    success_results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        conn=conn,
        fetch_fn=mock_fetch,
    )
    failure_results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        conn=conn,
        fetch_fn=failing_news_fetch,
    )

    asset_id = compute_asset_id("AAPL", "2026-01-15", "polygon_news")
    raw_count = conn.execute(
        "SELECT COUNT(*) FROM raw_assets WHERE asset_id = ?", (asset_id,)
    ).fetchone()[0]
    clean_count = conn.execute(
        "SELECT COUNT(*) FROM clean_assets WHERE asset_id = ?", (asset_id,)
    ).fetchone()[0]
    clean_asset = get_clean_asset(conn, asset_id)

    assert success_results[0]["ok"] is True
    assert failure_results[0]["ok"] is False
    assert raw_count == 1
    assert clean_count == 1
    assert clean_asset is not None
    assert "Test Article" in clean_asset["content_md"]
    conn.close()
