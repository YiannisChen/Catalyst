"""3F.2 Integration tests — combined batch ordering, full pipeline."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, patch

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


# ---------------------------------------------------------------------------
# TII1 — Combined polygon+finnhub batch ordering (DISCRIMINATING — B2 proof)
# ---------------------------------------------------------------------------

class TestCombinedBatchDedupAndTier:
    """Prove B2: dedup + classify run BEFORE polygon_rederive → Polygon articles
    from combined batch have NULL source_tier and NULL dedup_group_id."""

    async def test_combined_batch_polygon_articles_not_deduped_or_classified(self, tmp_path):
        """Discriminating: run polygon+finnhub combined → Polygon articles have tier+dedup.

        Before B2 fix: Polygon articles from a combined batch have NULL tier and NULL
        dedup_group_id because dedup and classify ran before polygon rederive.
        After fix: both are correctly assigned.
        """
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        shared_title = "Market rally continues as tech leads"
        shared_date = "2026-06-29T10:00:00Z"

        async def mock_polygon_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={
                    "results": [
                        {
                            "id": "poly_shared_1",
                            "title": shared_title,
                            "description": "Tech rally details",
                            "published_utc": shared_date,
                            "article_url": "https://polygon.example.com/1",
                            "publisher": {"name": "Reuters"},
                            "tickers": [ticker],
                            "keywords": [],
                            "insights": [],
                        }
                    ]
                },
                source_label=endpoint,
            )

        async def mock_finnhub_fetch(ticker, endpoint, date):
            from types import SimpleNamespace
            articles_data = [
                {
                    "id": "finn_shared_1",
                    "headline": shared_title,
                    "summary": "Tech leads market rally",
                    "datetime": 1750000000,
                    "source": "Yahoo Finance",
                    "url": "https://finnhub.io/redirect/1",
                }
            ]
            return SimpleNamespace(status=200, data=articles_data, error=None)

        fetch_fn = {"polygon_news": mock_polygon_fetch}

        import os
        from types import SimpleNamespace
        mock_ns = SimpleNamespace(fetch=mock_finnhub_fetch)

        with patch.dict(os.environ, {"FINNHUB_API_KEY": "test_key_redacted"}), \
             patch("catalyst_data.connectors.finnhub.create_finnhub_fetcher", return_value=mock_ns):
            await run_update_batch(
                db_path,
                tickers=["AAPL"],
                sources=["polygon_news", "finnhub_company_news"],
                from_date="2026-06-29",
                to_date="2026-06-29",
                fetch_fn=fetch_fn,
                limit=2,
                dry_run=False,
            )

        c = sqlite3.connect(db_path)
        poly_tier = c.execute(
            "SELECT source_tier FROM articles WHERE article_id = 'poly:poly_shared_1'"
        ).fetchone()
        poly_dedup = c.execute(
            "SELECT dedup_group_id FROM articles WHERE article_id = 'poly:poly_shared_1'"
        ).fetchone()
        finn_articles = c.execute(
            "SELECT article_id, source_tier, dedup_group_id FROM articles "
            "WHERE provider = 'finnhub'"
        ).fetchall()
        c.close()

        assert poly_tier is not None, "Polygon article should exist"
        assert poly_tier[0] is not None, (
            f"B2 FIX CHECK: Polygon article source_tier={poly_tier[0]} — "
            "should be non-NULL because classify now runs AFTER polygon rederive"
        )
        assert poly_dedup is not None and poly_dedup[0] is not None, (
            f"B2 FIX CHECK: Polygon article dedup_group_id={poly_dedup} — "
            "should be non-NULL because dedup now runs AFTER polygon rederive"
        )
        assert len(finn_articles) > 0, "Finnhub articles should exist"


# ---------------------------------------------------------------------------
# TII2 — Post-batch order verification
# ---------------------------------------------------------------------------

class TestPostBatchOrder:
    async def test_dedup_has_both_providers_articles(self, tmp_path):
        """After B2 fix: dedup runs after both providers are rederived,
        so all articles have non-NULL tier and one-canonical-per-group."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        conn.close()

        shared_title = "Fed signals rate decision"

        async def mock_polygon_fetch(ticker, endpoint, date):
            from catalyst_data.connectors.base import FetchResult
            return FetchResult(
                status=200,
                data={
                    "results": [
                        {
                            "id": "poly_fed_1",
                            "title": shared_title,
                            "description": "Fed analysis",
                            "published_utc": "2026-06-28T10:00:00Z",
                            "article_url": "https://poly.example.com/2",
                            "publisher": {"name": "CNBC"},
                            "tickers": [ticker],
                            "keywords": [], "insights": [],
                        }
                    ]
                },
                source_label=endpoint,
            )

        async def mock_finnhub_fetch(ticker, endpoint, date):
            articles_data = [
                {
                    "id": "finn_fed_1",
                    "headline": shared_title,
                    "summary": "Fed decision analysis",
                    "datetime": 1750000000,
                    "source": "Reuters",
                    "url": "https://finnhub.io/r/2",
                }
            ]
            from types import SimpleNamespace
            return SimpleNamespace(status=200, data=articles_data, error=None)

        fetch_fn = {"polygon_news": mock_polygon_fetch}

        import os
        from types import SimpleNamespace
        mock_ns = SimpleNamespace(fetch=mock_finnhub_fetch)

        with patch.dict(os.environ, {"FINNHUB_API_KEY": "test_key_redacted"}), \
             patch("catalyst_data.connectors.finnhub.create_finnhub_fetcher", return_value=mock_ns):
            await run_update_batch(
                db_path,
                tickers=["AAPL"],
                sources=["polygon_news", "finnhub_company_news"],
                from_date="2026-06-28",
                to_date="2026-06-28",
                fetch_fn=fetch_fn,
                limit=2,
                dry_run=False,
            )

        c = sqlite3.connect(db_path)

        null_tiers = c.execute(
            "SELECT COUNT(*) FROM articles WHERE source_tier IS NULL"
        ).fetchone()[0]
        assert null_tiers == 0, (
            f"All articles should have non-NULL source_tier, got {null_tiers} NULL"
        )

        poly_dedup = c.execute(
            "SELECT dedup_group_id FROM articles WHERE article_id = 'poly:poly_fed_1'"
        ).fetchone()
        assert poly_dedup is not None, "Polygon article dedup_group_id should exist"
        assert poly_dedup[0] is not None, "Polygon article should have dedup_group_id"

        bad_groups = c.execute("""
            SELECT at.dedup_group_id, SUM(a.is_canonical) as c
            FROM articles a
            JOIN article_tickers at ON at.article_id = a.article_id
            WHERE at.dedup_group_id IS NOT NULL
            GROUP BY at.dedup_group_id
            HAVING c != 1
        """).fetchall()
        assert len(bad_groups) == 0, (
            f"Every dedup group must have exactly 1 canonical, got {len(bad_groups)} bad"
        )
        c.close()
