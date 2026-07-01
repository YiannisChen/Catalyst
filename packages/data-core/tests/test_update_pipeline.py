"""Tests for update_pipeline.py — full pipeline with mocked fetch_fn."""

from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.update_pipeline import compute_missing_cells, run_update_batch
from catalyst_data.storage.sqlite import init_db
from catalyst_data.quality import ensure_ingestion_quality_tables
from catalyst_data.articles import ensure_articles_table, upsert_article, upsert_article_ticker


def _make_db(db_path: str, *, with_checkpoints: bool = False) -> sqlite3.Connection:
    """Create a minimal DB with ohlcv + articles."""
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    ensure_articles_table(conn)

    # 3 trading days, 2 tickers
    for dt in ("2026-06-30", "2026-06-29", "2026-06-28"):
        for sym in ("AAPL", "TSLA"):
            conn.execute(
                "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
                (sym, dt),
            )

    if with_checkpoints:
        # Pre-populate: AAPL 2026-06-30 is already successful
        conn.execute(
            "INSERT OR REPLACE INTO source_checkpoints "
            "(run_id, source_type, ticker, date, status) "
            "VALUES ('run_prev', 'polygon_news', 'AAPL', '2026-06-30', 'success')"
        )
        # One failed checkpoint that should be retried
        conn.execute(
            "INSERT OR REPLACE INTO source_checkpoints "
            "(run_id, source_type, ticker, date, status, error_class, retries) "
            "VALUES ('run_prev', 'polygon_news', 'TSLA', '2026-06-29', 'failed', "
            "'TimeoutError', 1)"
        )

    conn.commit()
    return conn


class TestComputeMissingCells:
    def test_all_covered_returns_empty(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_checkpoints=True)

        missing = compute_missing_cells(
            conn, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-30", to_date="2026-06-30",
        )
        assert missing == []
        conn.close()

    def test_failed_cells_included(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_checkpoints=True)

        missing = compute_missing_cells(
            conn, tickers=["TSLA"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
        )
        assert ("TSLA", "2026-06-29", "polygon_news") in missing
        conn.close()

    def test_uncovered_cells_returned(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        missing = compute_missing_cells(
            conn, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-30",
        )
        assert len(missing) == 2
        conn.close()

    def test_ticker_filter_respected(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        missing = compute_missing_cells(
            conn, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-30",
        )
        for cell in missing:
            assert cell[0] == "AAPL"
        conn.close()

    def test_source_filter_respected(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        missing = compute_missing_cells(
            conn, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-30",
        )
        for cell in missing:
            assert cell[2] == "polygon_news"
        conn.close()

    def test_empty_window_returns_empty(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        missing = compute_missing_cells(
            conn, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-30", to_date="2026-06-29",
        )
        assert missing == []
        conn.close()


class TestRunUpdateBatchDryRun:
    def test_dry_run_no_network(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        import asyncio

        async def _run():
            return await run_update_batch(
                db_path,
                tickers=["AAPL"],
                sources=["polygon_news"],
                from_date="2026-06-29",
                to_date="2026-06-30",
                dry_run=True,
            )

        report = asyncio.run(_run())

        assert report["mode"] == "dry-run"

        assert report["cells_success"] == 0
        assert report["cells_failed"] == 0
        assert report["run_id"] is None
        assert report["cells_total"] > 0

    def test_dry_run_zero_db_writes(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        before_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        before_at = conn.execute(
            "SELECT COUNT(*) FROM article_tickers"
        ).fetchone()[0]
        before_cp = conn.execute(
            "SELECT COUNT(*) FROM source_checkpoints"
        ).fetchone()[0]
        before_runs = conn.execute(
            "SELECT COUNT(*) FROM ingestion_runs"
        ).fetchone()[0]
        conn.close()

        import asyncio

        async def _run():
            return await run_update_batch(
                db_path,
                tickers=["AAPL"],
                sources=["polygon_news"],
                from_date="2026-06-29",
                to_date="2026-06-30",
                dry_run=True,
            )

        asyncio.run(_run())

        conn = sqlite3.connect(db_path)
        after_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        after_at = conn.execute(
            "SELECT COUNT(*) FROM article_tickers"
        ).fetchone()[0]
        after_cp = conn.execute(
            "SELECT COUNT(*) FROM source_checkpoints"
        ).fetchone()[0]
        after_runs = conn.execute(
            "SELECT COUNT(*) FROM ingestion_runs"
        ).fetchone()[0]
        conn.close()

        assert after_art == before_art
        assert after_at == before_at
        assert after_cp == before_cp
        assert after_runs == before_runs

    def test_dry_run_requires_no_fetch_fn(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        import asyncio

        with pytest.raises(ValueError, match="fetch_fn"):
            asyncio.run(
                run_update_batch(
                    db_path,
                    tickers=["AAPL"],
                    from_date="2026-06-29",
                    to_date="2026-06-30",
                    dry_run=False,
                )
            )


class TestRunUpdateBatchReal:
    """Mocked real-path tests.  Uses await directly (pytest-asyncio auto mode)."""

    async def test_mocked_fetch(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={"results": []},
                source_label=endpoint,
            )

        report = await run_update_batch(
            db_path,
            tickers=["AAPL"],
            sources=["polygon_news"],
            from_date="2026-06-29",
            to_date="2026-06-30",
            fetch_fn=mock_fetch,
            limit=1,
            dry_run=False,
        )

        assert report["mode"] == "update"
        assert report["run_id"] is not None

    async def test_idempotent_rerun(self, tmp_path):
        """Dry-run re-run: all cells already checkpointed → zero missing."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_checkpoints=True)
        conn.close()

        # All AAPL cells in window already have success checkpoints
        report = await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-30", to_date="2026-06-30",
            dry_run=True,
        )
        assert report["cells_total"] == 0
        assert report["mode"] == "dry-run"

    async def test_checkpoint_skip(self, tmp_path):
        """Cells with a success checkpoint are skipped on re-run."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_checkpoints=True)
        conn.close()

        fetch_calls = []

        async def mock_fetch(ticker, endpoint, date):
            fetch_calls.append((ticker, endpoint, date))
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200, data={"results": []},
                source_label=endpoint,
            )

        report = await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-28", to_date="2026-06-30",
            fetch_fn=mock_fetch, dry_run=False,
        )

        # AAPL 2026-06-30 has success checkpoint → skipped
        fetched_tickers = {f[0] for f in fetch_calls}
        assert all(t == "AAPL" for t in fetched_tickers)
        fetched_dates = {f[1] for f in fetch_calls}
        assert "2026-06-30" not in fetched_dates

    async def test_no_index_state_writes_from_dry_run(self, tmp_path):
        """Verify pipeline does NOT write index_state/manifests."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200, data={"results": []},
                source_label=endpoint,
            )

        conn = sqlite3.connect(db_path)
        before_is = conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0]
        before_im = conn.execute("SELECT COUNT(*) FROM index_manifests").fetchone()[0]
        conn.close()

        await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
            fetch_fn=mock_fetch, limit=1, dry_run=False,
        )

        conn = sqlite3.connect(db_path)
        after_is = conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0]
        after_im = conn.execute("SELECT COUNT(*) FROM index_manifests").fetchone()[0]
        conn.close()

        assert after_is == before_is
        assert after_im == before_im
