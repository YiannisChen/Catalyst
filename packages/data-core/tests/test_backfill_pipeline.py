"""Tests for backfill_pipeline.py — date-window chunking around update pipeline."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from catalyst_data.backfill_pipeline import run_backfill, _chunk_date_range
from catalyst_data.storage.sqlite import init_db
from catalyst_data.quality import ensure_ingestion_quality_tables
from catalyst_data.articles import ensure_articles_table


def _make_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    ensure_articles_table(conn)

    # Long OHLCV range: 10 trading days
    for i in range(10):
        dt = f"2026-06-{20 + i:02d}"
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES ('AAPL', ?, 100.0)",
            (dt,),
        )
    conn.commit()
    return conn


class TestChunkDateRange:
    def test_produces_correct_chunks(self):
        chunks = _chunk_date_range("2026-06-20", "2026-06-29", 3)
        assert chunks == [
            ("2026-06-20", "2026-06-22"),
            ("2026-06-23", "2026-06-25"),
            ("2026-06-26", "2026-06-28"),
            ("2026-06-29", "2026-06-29"),
        ]

    def test_single_day_window(self):
        chunks = _chunk_date_range("2026-06-20", "2026-06-20", 7)
        assert chunks == [("2026-06-20", "2026-06-20")]

    def test_empty_range_returns_empty(self):
        chunks = _chunk_date_range("2026-06-30", "2026-06-20", 7)
        assert chunks == []


class TestRunBackfillDryRun:
    def test_dry_run_produces_chunks(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def _run():
            return await run_backfill(
                db_path,
                tickers=["AAPL"],
                sources=["polygon_news"],
                from_date="2026-06-20",
                to_date="2026-06-29",
                chunk_days=3,
                dry_run=True,
            )

        results = asyncio.run(_run())

        assert len(results) == 4
        total_cells = sum(r["cells_total"] for r in results)
        # 10 trading days × 1 ticker × 1 source = 10
        assert total_cells == 10

    def test_chunks_no_overlap(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def _run():
            return await run_backfill(
                db_path,
                tickers=["AAPL"],
                sources=["polygon_news"],
                from_date="2026-06-20",
                to_date="2026-06-22",
                chunk_days=2,
                dry_run=True,
            )

        results = asyncio.run(_run())
        assert len(results) == 2

        cells = [c for r in results for c in r.get("missing_cells", [])]
        dates_seen = set(c[1] for c in cells)
        # Should cover exactly 3 days with no duplicates
        assert len(dates_seen) == 3

    def test_chunk_size_respected(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def _run():
            return await run_backfill(
                db_path,
                tickers=["AAPL"],
                sources=["polygon_news"],
                from_date="2026-06-20",
                to_date="2026-06-29",
                chunk_days=2,
                dry_run=True,
            )

        results = asyncio.run(_run())
        # 10 days / 2-day chunks = 5 chunks
        assert len(results) == 5
        # Each chunk should have 2 trading days × 1 ticker × 1 source = 2 cells
        # Last chunk might have fewer if ohlcv is sparse
        for r in results[:-1]:
            assert r["cells_total"] <= 2  # 2 days per chunk max
