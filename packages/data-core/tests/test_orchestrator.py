"""Integration tests for the orchestrator module."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from catalyst_data.connectors.base import FetchResult
from catalyst_data.orchestrator import process_request
from catalyst_data.storage.sqlite import (
    compute_asset_id,
    get_clean_asset,
    get_ohlcv,
    init_db,
)


@pytest.fixture
def db_path(tmp_path):
    """Create a temp SQLite DB with schema initialized."""
    p = tmp_path / "test.db"
    conn = sqlite3.connect(str(p))
    init_db(conn)
    conn.close()
    return str(p)


def _read_conn(db_path: str) -> sqlite3.Connection:
    """Open a read-only connection for assertions."""
    return sqlite3.connect(db_path)


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
    if endpoint == "ohlcv":
        return FetchResult(
            status=200,
            data={
                "ticker": ticker,
                "results": [
                    {
                        "o": 150.12,
                        "h": 150.89,
                        "l": 147.22,
                        "c": 148.34,
                        "v": 98322150,
                    }
                ],
            },
            latency_ms=10.0,
            source_label="polygon:ohlcv",
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
async def test_process_request_stores_bronze_and_silver(db_path):
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        db_path=db_path,
        fetch_fn=mock_fetch,
    )
    assert len(results) >= 1
    assert results[0]["ok"] is True
    assert results[0]["asset_id"] is not None

    conn = _read_conn(db_path)
    raw = conn.execute("SELECT * FROM raw_assets").fetchall()
    assert len(raw) >= 1
    clean = conn.execute("SELECT * FROM clean_assets").fetchall()
    assert len(clean) >= 1
    conn.close()


@pytest.mark.asyncio
async def test_process_request_handles_failed_fetch(db_path):
    async def failing_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        return FetchResult(
            status=500, error="Server error", latency_ms=10.0, source_label="test"
        )

    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        db_path=db_path,
        fetch_fn=failing_fetch,
    )
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert results[0]["error"] is not None


@pytest.mark.asyncio
async def test_process_request_multiple_sources(db_path):
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news", "fmp_fundamentals"],
        db_path=db_path,
        fetch_fn=mock_fetch,
    )
    assert len(results) >= 2

    for r in results:
        assert r["ok"] is True

    conn = _read_conn(db_path)
    raw_count = conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
    clean_count = conn.execute("SELECT COUNT(*) FROM clean_assets").fetchone()[0]
    assert raw_count >= 2
    assert clean_count >= 2
    conn.close()


@pytest.mark.asyncio
async def test_process_request_yfinance_fundamentals_source_maps_correctly(db_path):
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["yfinance_fundamentals"],
        db_path=db_path,
        fetch_fn=mock_fetch,
    )
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["asset_id"] is not None


@pytest.mark.asyncio
async def test_process_request_persists_ohlcv_rows_on_production_path(db_path):
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_ohlcv"],
        db_path=db_path,
        fetch_fn=mock_fetch,
    )

    assert len(results) == 1
    assert results[0]["ok"] is True

    conn = _read_conn(db_path)
    bar = get_ohlcv(conn, "AAPL", "2026-01-15")
    conn.close()

    assert bar is not None
    assert bar["open"] == pytest.approx(150.12)
    assert bar["close"] == pytest.approx(148.34)
    assert bar["source"] == "polygon"


@pytest.mark.asyncio
async def test_process_request_skips_empty_news_without_storing_clean_asset(db_path):
    async def empty_news_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        return FetchResult(
            status=200,
            data={"results": []},
            latency_ms=1.0,
            source_label="polygon:news",
        )

    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        db_path=db_path,
        fetch_fn=empty_news_fetch,
    )

    assert len(results) == 1
    summary = results[0]
    assert summary["ok"] is False
    assert summary["skipped"] is True
    assert summary["skip_reason"] == "no_articles"

    conn = _read_conn(db_path)
    clean_count = conn.execute("SELECT COUNT(*) FROM clean_assets").fetchone()[0]
    conn.close()
    assert clean_count == 0


@pytest.mark.asyncio
async def test_process_request_skips_empty_ohlcv_without_storing_bar(db_path):
    async def empty_ohlcv_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        return FetchResult(
            status=200,
            data={"results": []},
            latency_ms=1.0,
            source_label="polygon:ohlcv",
        )

    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_ohlcv"],
        db_path=db_path,
        fetch_fn=empty_ohlcv_fetch,
    )

    assert len(results) == 1
    summary = results[0]
    assert summary["ok"] is False
    assert summary["skipped"] is True
    assert summary["skip_reason"] == "no_ohlcv_bar"

    conn = _read_conn(db_path)
    bar = get_ohlcv(conn, "AAPL", "2026-01-15")
    conn.close()
    assert bar is None


@pytest.mark.asyncio
async def test_process_request_partial_failure(db_path):
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

    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news", "fmp_fundamentals"],
        db_path=db_path,
        fetch_fn=partial_fetch,
    )
    assert len(results) == 2

    news_result = next(r for r in results if r["source"] == "polygon_news")
    fmp_result = next(r for r in results if r["source"] == "fmp_fundamentals")

    assert news_result["ok"] is False
    assert fmp_result["ok"] is True


@pytest.mark.asyncio
async def test_process_request_reports_failed_endpoints_on_partial_success(db_path):
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

    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["fmp_fundamentals"],
        db_path=db_path,
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


@pytest.mark.asyncio
async def test_process_request_reports_fetch_exceptions_per_source(db_path):
    async def exploding_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        raise RuntimeError(f"boom for {endpoint}")

    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        db_path=db_path,
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


@pytest.mark.asyncio
async def test_process_request_handles_malformed_news_results_dict(db_path):
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

    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        db_path=db_path,
        fetch_fn=malformed_news_fetch,
    )

    assert len(results) == 1
    summary = results[0]
    assert summary["ok"] is True
    conn = _read_conn(db_path)
    clean_rows = conn.execute(
        "SELECT content_md FROM clean_assets WHERE source_type = 'polygon_news'"
    ).fetchall()
    conn.close()
    assert len(clean_rows) >= 1
    content_md = clean_rows[0][0]
    assert "Untitled" in content_md
    assert "Shape drifted to a single object" in content_md


@pytest.mark.asyncio
async def test_process_request_handles_string_publisher_shape(db_path):
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

    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        db_path=db_path,
        fetch_fn=malformed_news_fetch,
    )

    assert len(results) == 1
    summary = results[0]
    assert summary["ok"] is True
    conn = _read_conn(db_path)
    clean_rows = conn.execute(
        "SELECT content_md FROM clean_assets WHERE source_type = 'polygon_news'"
    ).fetchall()
    conn.close()
    assert len(clean_rows) >= 1
    content_md = clean_rows[0][0]
    assert "Untitled" in content_md
    assert "Reuters" in content_md
    assert "Publisher drifted to a string" in content_md


@pytest.mark.asyncio
async def test_process_request_rerun_keeps_single_bronze_and_silver_row(db_path):
    first_results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        db_path=db_path,
        fetch_fn=mock_fetch,
    )
    second_results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        db_path=db_path,
        fetch_fn=mock_fetch,
    )

    asset_id = compute_asset_id("AAPL", "2026-01-15", "polygon_news")
    # B2: clean_asset.asset_id is now poly:{article_id}, not the SHA-256 raw asset_id
    assert first_results[0]["asset_id"] is not None
    assert second_results[0]["asset_id"] is not None
    conn = _read_conn(db_path)
    raw_count = conn.execute(
        "SELECT COUNT(*) FROM raw_assets WHERE asset_id = ?", (asset_id,)
    ).fetchone()[0]
    clean_count = conn.execute(
        "SELECT COUNT(*) FROM clean_assets WHERE source_type = 'polygon_news'"
    ).fetchone()[0]
    conn.close()
    assert raw_count == 1
    assert clean_count >= 1


@pytest.mark.asyncio
async def test_process_request_partial_failure_rerun_does_not_corrupt_prior_success(db_path):
    async def failing_news_fetch(
        ticker: str, endpoint: str, date: str
    ) -> FetchResult:
        return FetchResult(
            status=503,
            error="Polygon 503: upstream unavailable",
            latency_ms=1.0,
            source_label="polygon:news",
        )

    success_results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        db_path=db_path,
        fetch_fn=mock_fetch,
    )
    failure_results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news"],
        db_path=db_path,
        fetch_fn=failing_news_fetch,
    )

    asset_id = compute_asset_id("AAPL", "2026-01-15", "polygon_news")
    conn = _read_conn(db_path)
    raw_count = conn.execute(
        "SELECT COUNT(*) FROM raw_assets WHERE asset_id = ?", (asset_id,)
    ).fetchone()[0]
    clean_count = conn.execute(
        "SELECT COUNT(*) FROM clean_assets WHERE source_type = 'polygon_news'"
    ).fetchone()[0]
    clean_rows = conn.execute(
        "SELECT content_md FROM clean_assets WHERE source_type = 'polygon_news'"
    ).fetchall()
    conn.close()

    assert success_results[0]["ok"] is True
    assert failure_results[0]["ok"] is False
    assert raw_count == 1
    assert clean_count >= 1
    assert len(clean_rows) >= 1
    assert "Test Article" in clean_rows[0][0]


@pytest.mark.asyncio
async def test_process_request_sources_run_concurrently(db_path):
    """Verify sources execute concurrently, not sequentially (BUG-003)."""

    async def slow_fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
        await asyncio.sleep(0.1)  # simulate 100ms network latency
        if endpoint == "news":
            return FetchResult(
                status=200,
                data={"results": [{"title": "A", "published_utc": "2026-01-15T10:00:00Z",
                                   "description": "d", "article_url": "http://x"}]},
                latency_ms=100, source_label=f"polygon:{endpoint}",
            )
        return FetchResult(
            status=200, data=[{"revenue": 1}],
            latency_ms=100, source_label=f"fmp:{endpoint}",
        )

    t0 = time.monotonic()
    results = await process_request(
        ticker="AAPL",
        date="2026-01-15",
        sources=["polygon_news", "fmp_fundamentals"],
        db_path=db_path,
        fetch_fn=slow_fetch,
    )
    elapsed = time.monotonic() - t0

    assert all(r["ok"] for r in results)
    # Sequential would take >=0.5s (5 endpoints × 0.1s).
    # Concurrent sources should complete well under that.
    assert elapsed < 0.45, f"Took {elapsed:.2f}s — sources may still be sequential"
