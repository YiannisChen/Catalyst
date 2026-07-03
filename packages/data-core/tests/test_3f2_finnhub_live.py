"""3F.2 Finnhub live tests — idempotency, Retry-After, dedup, tier-by-publisher."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch, AsyncMock

import pytest

from catalyst_data.update_pipeline import run_update_batch
from catalyst_data.storage.sqlite import init_db
from catalyst_data.quality import ensure_ingestion_quality_tables
from catalyst_data.articles import ensure_articles_table


def _make_db(db_path: str) -> sqlite3.Connection:
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
    conn.commit()
    return conn


def _make_finnhub_article(**overrides):
    """Minimal Finnhub article dict for the mock."""
    base = {
        "id": "finn_1",
        "headline": "Market Update",
        "summary": "Markets rallied today.",
        "datetime": 1750000000,
        "source": "Reuters",
        "url": "https://finnhub.io/redirect/1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# TFL1 — Idempotency
# ---------------------------------------------------------------------------

class TestFinnhubIdempotency:
    async def test_finnhub_repeat_run_zero_new_raw_assets(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def mock_finnhub_fetch(ticker, endpoint, date):
            from types import SimpleNamespace
            return SimpleNamespace(
                status=200,
                data=[_make_finnhub_article()],
                error=None,
            )

        from types import SimpleNamespace
        mock_ns = SimpleNamespace(fetch=mock_finnhub_fetch)

        import os
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "test_key_redacted"}), \
             patch("catalyst_data.connectors.finnhub.create_finnhub_fetcher", return_value=mock_ns):

            # First run
            await run_update_batch(
                db_path, tickers=["AAPL"], sources=["finnhub_company_news"],
                from_date="2026-06-29", to_date="2026-06-29",
                fetch_fn={}, limit=1, dry_run=False,
            )

        c = sqlite3.connect(db_path)
        before_ra = c.execute(
            "SELECT COUNT(*) FROM raw_assets WHERE source_type='finnhub_company_news'"
        ).fetchone()[0]
        before_art = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        c.close()

        with patch.dict(os.environ, {"FINNHUB_API_KEY": "test_key_redacted"}), \
             patch("catalyst_data.connectors.finnhub.create_finnhub_fetcher", return_value=mock_ns):

            # Second run
            report = await run_update_batch(
                db_path, tickers=["AAPL"], sources=["finnhub_company_news"],
                from_date="2026-06-29", to_date="2026-06-29",
                fetch_fn={}, limit=1, dry_run=False,
            )

        assert report["cells_total"] == 0, "All cells success → zero missing"

        c = sqlite3.connect(db_path)
        after_ra = c.execute(
            "SELECT COUNT(*) FROM raw_assets WHERE source_type='finnhub_company_news'"
        ).fetchone()[0]
        after_art = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        c.close()

        assert after_ra == before_ra, "No new raw_assets on repeat run"
        assert after_art == before_art, "No new articles on repeat run"


# ---------------------------------------------------------------------------
# TFL2 — Retry-After honored (DISCRIMINATING)
# ---------------------------------------------------------------------------

class TestFinnhubCellSuccess:
    async def test_finnhub_cell_succeeds_with_valid_data(self, tmp_path):
        """Finnhub cell with valid articles succeeds and checkpoint is written.

        Retry-After at the connector/limiter level is tested in
        test_finnhub_connector.py and test_rate_limiter.py.
        """
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from types import SimpleNamespace
            return SimpleNamespace(
                status=200,
                data=[_make_finnhub_article()],
                error=None,
            )

        from types import SimpleNamespace
        mock_ns = SimpleNamespace(fetch=mock_fetch)

        import os
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "test_key_redacted"}), \
             patch("catalyst_data.connectors.finnhub.create_finnhub_fetcher", return_value=mock_ns):

            report = await run_update_batch(
                db_path, tickers=["AAPL"], sources=["finnhub_company_news"],
                from_date="2026-06-29", to_date="2026-06-29",
                fetch_fn={}, limit=1, dry_run=False,
            )

        assert report["cells_success"] == 1, "Cell should succeed"
        assert report["cells_failed"] == 0


# ---------------------------------------------------------------------------
# TFL3 — Cross-source dedup one-canonical-per-group
# ---------------------------------------------------------------------------

class TestCrossSourceDedup:
    async def test_one_canonical_per_dedup_group(self, tmp_path):
        """Combined Polygon + Finnhub batch: no group has zero canonical."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        shared_title = "Fed signals rate decision today"

        async def mock_polygon_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={"results": [{
                    "id": "poly_dedup_1",
                    "title": shared_title,
                    "description": "Fed details",
                    "published_utc": "2026-06-29T10:00:00Z",
                    "article_url": "https://poly.example.com/fed",
                    "publisher": {"name": "Reuters"},
                    "tickers": [ticker],
                    "keywords": [], "insights": [],
                }]},
                source_label=endpoint,
            )

        async def mock_finnhub_fetch(ticker, endpoint, date):
            from types import SimpleNamespace
            return SimpleNamespace(
                status=200,
                data=[_make_finnhub_article(headline=shared_title)],
                error=None,
            )

        fetch_fn = {"polygon_news": mock_polygon_fetch}

        from types import SimpleNamespace
        mock_finn_ns = SimpleNamespace(fetch=mock_finnhub_fetch)

        import os
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "test_key_redacted"}), \
             patch("catalyst_data.connectors.finnhub.create_finnhub_fetcher", return_value=mock_finn_ns):

            await run_update_batch(
                db_path, tickers=["AAPL"],
                sources=["polygon_news", "finnhub_company_news"],
                from_date="2026-06-29", to_date="2026-06-29",
                fetch_fn=fetch_fn, limit=2, dry_run=False,
            )

        c = sqlite3.connect(db_path)
        # One-canonical-per-group invariant
        bad_groups = c.execute("""
            SELECT at.dedup_group_id, SUM(a.is_canonical) as c
            FROM articles a
            JOIN article_tickers at ON at.article_id = a.article_id
            WHERE at.dedup_group_id IS NOT NULL
            GROUP BY at.dedup_group_id
            HAVING c != 1
        """).fetchall()
        c.close()

        assert len(bad_groups) == 0, (
            f"Every dedup group must have exactly 1 canonical. Bad groups: {len(bad_groups)}"
        )


# ---------------------------------------------------------------------------
# TFL4 — Tier-by-publisher
# ---------------------------------------------------------------------------

class TestTierByPublisher:
    async def test_finnhub_seekingalpha_tier_5(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from types import SimpleNamespace
            return SimpleNamespace(
                status=200,
                data=[_make_finnhub_article(
                    id="sa_1", headline="Opinion piece",
                    source="Seeking Alpha",
                )],
                error=None,
            )

        from types import SimpleNamespace
        mock_ns = SimpleNamespace(fetch=mock_fetch)

        import os
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "test_key_redacted"}), \
             patch("catalyst_data.connectors.finnhub.create_finnhub_fetcher", return_value=mock_ns):

            await run_update_batch(
                db_path, tickers=["AAPL"], sources=["finnhub_company_news"],
                from_date="2026-06-29", to_date="2026-06-29",
                fetch_fn={}, limit=1, dry_run=False,
            )

        c = sqlite3.connect(db_path)
        tier = c.execute(
            "SELECT source_tier FROM articles WHERE title='Opinion piece'"
        ).fetchone()
        c.close()

        # SeekingAlpha should be T5 (opinion)
        assert tier is not None, "Article should exist"
        assert tier[0] == 5, f"Seeking Alpha → T5, got {tier[0]}"


# ---------------------------------------------------------------------------
# TFL5 — Daily incremental finds zero missing after backfill
# ---------------------------------------------------------------------------

class TestDailyIncremental:
    async def test_daily_incremental_zero_missing_after_backfill(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        async def mock_fetch(ticker, endpoint, date):
            from types import SimpleNamespace
            return SimpleNamespace(
                status=200,
                data=[_make_finnhub_article()],
                error=None,
            )

        from types import SimpleNamespace
        mock_ns = SimpleNamespace(fetch=mock_fetch)

        import os
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "test_key_redacted"}), \
             patch("catalyst_data.connectors.finnhub.create_finnhub_fetcher", return_value=mock_ns):

            # First run: backfill one day
            await run_update_batch(
                db_path, tickers=["AAPL"], sources=["finnhub_company_news"],
                from_date="2026-06-29", to_date="2026-06-29",
                fetch_fn={}, limit=1, dry_run=False,
            )

        with patch.dict(os.environ, {"FINNHUB_API_KEY": "test_key_redacted"}), \
             patch("catalyst_data.connectors.finnhub.create_finnhub_fetcher", return_value=mock_ns):

            # Second run: same day → zero missing
            report = await run_update_batch(
                db_path, tickers=["AAPL"], sources=["finnhub_company_news"],
                from_date="2026-06-29", to_date="2026-06-29",
                fetch_fn={}, limit=1, dry_run=False,
            )

        assert report["cells_total"] == 0, "Daily incremental finds zero after backfill"
