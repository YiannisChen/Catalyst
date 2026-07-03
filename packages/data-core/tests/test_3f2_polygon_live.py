"""3F.2 Polygon live tests — fetch_fn routing, idempotency, checkpoint resume."""

from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.update_pipeline import run_update_batch
from catalyst_data.storage.sqlite import init_db
from catalyst_data.quality import ensure_ingestion_quality_tables
from catalyst_data.articles import ensure_articles_table


def _make_db(db_path: str, *, with_checkpoints: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    ensure_articles_table(conn)

    for dt in ("2026-06-30", "2026-06-29", "2026-06-28"):
        for sym in ("AAPL", "TSLA"):
            conn.execute(
                "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
                (sym, dt),
            )

    if with_checkpoints:
        conn.execute(
            "INSERT OR REPLACE INTO source_checkpoints "
            "(run_id, source_type, ticker, date, status) "
            "VALUES ('run_prev', 'polygon_news', 'AAPL', '2026-06-30', 'success')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO source_checkpoints "
            "(run_id, source_type, ticker, date, status, error_class, retries) "
            "VALUES ('run_prev', 'polygon_news', 'TSLA', '2026-06-29', 'failed', "
            "'TimeoutError', 1)"
        )

    conn.commit()
    return conn


def _make_article(**overrides):
    """Build a minimal Polygon article dict that passes orchestrator validation."""
    base = {
        "id": "art_1",
        "title": "Test Article",
        "published_utc": "2026-06-29T12:00:00Z",
        "article_url": "https://example.com/1",
        "publisher": {"name": "Reuters"},
        "tickers": ["AAPL"],
        "keywords": [],
        "insights": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# TPL1 — Polygon fetch_fn dict routing (DISCRIMINATING — B3 fix)
# ---------------------------------------------------------------------------

class TestPolygonFetchFnUnwrap:
    """B3 fix: dict fetch_fn {"polygon_news": callable} is correctly unwrapped."""

    async def test_dict_fetch_fn_cell_succeeds_after_fix(self, tmp_path):
        """B3 FIX VERIFIED: dict fetch_fn → cell SUCCEEDS (not fails).

        Before fix: dict passed to orchestrator → TypeError → cell failed.
        After fix: dict is unwrapped → real fetch_fn called → cell succeeds.
        """
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        call_count = 0

        async def mock_polygon_fetch(ticker, endpoint, date):
            nonlocal call_count
            call_count += 1
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={"results": [_make_article()]},
                source_label=endpoint,
            )

        fetch_fn = {"polygon_news": mock_polygon_fetch}

        report = await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
            fetch_fn=fetch_fn, limit=1, dry_run=False,
        )

        # B3 fixed: cell succeeds, fetch_fn was called
        assert report["cells_success"] == 1, (
            f"B3 FIX: dict unwrapped → cell succeeds. Got {report['cells_success']}"
        )
        assert report["cells_failed"] == 0
        assert call_count > 0, "Fetch function should have been called"


# ---------------------------------------------------------------------------
# TPL2 — Polygon fetch writes raw_assets + articles
# ---------------------------------------------------------------------------

class TestPolygonLiveRawAssetWritten:
    async def test_polygon_live_raw_asset_and_articles_written(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        article_title = "Earnings beat estimates"

        async def mock_polygon_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={"results": [_make_article(title=article_title)]},
                source_label=endpoint,
            )

        fetch_fn = {"polygon_news": mock_polygon_fetch}

        await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
            fetch_fn=fetch_fn, limit=1, dry_run=False,
        )

        c = sqlite3.connect(db_path)
        ra_count = c.execute(
            "SELECT COUNT(*) FROM raw_assets WHERE source_type='polygon_news'"
        ).fetchone()[0]
        assert ra_count > 0, "raw_asset should be written"

        art_count = c.execute(
            "SELECT COUNT(*) FROM articles WHERE title = ?", (article_title,)
        ).fetchone()[0]
        assert art_count > 0, "Article should be written after rederive"

        at_count = c.execute(
            "SELECT COUNT(*) FROM article_tickers WHERE ticker = 'AAPL'"
        ).fetchone()[0]
        assert at_count > 0, "article_tickers should link to AAPL"
        c.close()


# ---------------------------------------------------------------------------
# TPL3 — Idempotency
# ---------------------------------------------------------------------------

class TestPolygonIdempotency:
    async def test_repeat_run_zero_new_raw_assets(self, tmp_path):
        """Second live run: all cells already success → cells_total=0, no new raw_assets."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={"results": [_make_article()]},
                source_label=endpoint,
            )

        fetch_fn = {"polygon_news": mock_fetch}

        # First run
        await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
            fetch_fn=fetch_fn, limit=1, dry_run=False,
        )

        c = sqlite3.connect(db_path)
        before_ra = c.execute(
            "SELECT COUNT(*) FROM raw_assets WHERE source_type='polygon_news'"
        ).fetchone()[0]
        before_art = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        c.close()

        # Second run: all should be success → 0 missing cells
        report = await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
            fetch_fn=fetch_fn, limit=1, dry_run=False,
        )

        assert report["cells_total"] == 0, "All cells success → zero missing"

        c = sqlite3.connect(db_path)
        after_ra = c.execute(
            "SELECT COUNT(*) FROM raw_assets WHERE source_type='polygon_news'"
        ).fetchone()[0]
        after_art = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        c.close()

        assert after_ra == before_ra, "No new raw_assets on repeat run"
        assert after_art == before_art, "No new articles on repeat run"


# ---------------------------------------------------------------------------
# TPL4 — Checkpoint reflects success (NOT multi-ticker from article field)
# ---------------------------------------------------------------------------

class TestCheckpointWritten:
    async def test_success_checkpoint_written(self, tmp_path):
        """Cell marked success → checkpoint status='success'."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={"results": [_make_article()]},
                source_label=endpoint,
            )

        fetch_fn = {"polygon_news": mock_fetch}

        await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
            fetch_fn=fetch_fn, limit=1, dry_run=False,
        )

        c = sqlite3.connect(db_path)
        cp = c.execute(
            "SELECT status FROM source_checkpoints WHERE ticker='AAPL' AND date='2026-06-29'"
        ).fetchone()
        c.close()
        assert cp is not None, "Checkpoint should be written"
        assert cp[0] == "success", f"Expected success, got {cp[0]}"


# ---------------------------------------------------------------------------
# TPL5 — Failed cell retry
# ---------------------------------------------------------------------------

class TestFailedCellRetry:
    async def test_failed_checkpoint_cell_is_retried(self, tmp_path):
        """Cell with failed checkpoint appears in missing cells → retried."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_checkpoints=True)
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={"results": [_make_article()]},
                source_label=endpoint,
            )

        fetch_fn = {"polygon_news": mock_fetch}

        # TSLA 2026-06-29 has status='failed' → should be retried
        report = await run_update_batch(
            db_path, tickers=["TSLA"], sources=["polygon_news"],
            from_date="2026-06-29", to_date="2026-06-29",
            fetch_fn=fetch_fn, limit=1, dry_run=False,
        )

        assert report["cells_total"] > 0, "Failed cell should appear as missing"
        assert report["cells_success"] > 0, "Should succeed on retry"

        c = sqlite3.connect(db_path)
        cps = c.execute(
            "SELECT status FROM source_checkpoints WHERE ticker='TSLA' AND date='2026-06-29'"
        ).fetchall()
        c.close()
        statuses = [r[0] for r in cps]
        assert "success" in statuses, (
            f"At least one checkpoint should be success, got {statuses}"
        )


# ---------------------------------------------------------------------------
# TPL6 — Checkpoint resume
# ---------------------------------------------------------------------------

class TestCheckpointResume:
    async def test_checkpoint_resume_zero_new_cells_on_rerun(self, tmp_path):
        """All cells success → second run has 0 missing cells."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        call_count = 0

        async def mock_fetch(ticker, endpoint, date):
            nonlocal call_count
            call_count += 1
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={"results": [_make_article()]},
                source_label=endpoint,
            )

        fetch_fn = {"polygon_news": mock_fetch}

        # First run: 3 cells
        await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-28", to_date="2026-06-30",
            fetch_fn=fetch_fn, dry_run=False,
        )

        first_count = call_count
        assert first_count >= 3, f"Should fetch 3 cells, got {first_count}"

        # Second run: all success → zero missing
        report = await run_update_batch(
            db_path, tickers=["AAPL"], sources=["polygon_news"],
            from_date="2026-06-28", to_date="2026-06-30",
            fetch_fn=fetch_fn, dry_run=False,
        )

        assert report["cells_total"] == 0, "Second run: all cells already success"
        # call_count may increase due to post-batch rederive re-fetching, but missing cells=0
        assert call_count == first_count, (
            f"No new fetch calls on resume, got {call_count} vs {first_count}"
        )
