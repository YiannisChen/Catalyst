"""Tests for freshness.py — news + index staleness (read-only)."""

from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.freshness import (
    latest_local_ohlcv_date,
    news_freshness,
    index_freshness,
    freshness_report,
)
from catalyst_data.storage.sqlite import init_db
from catalyst_data.articles import ensure_articles_table, upsert_article, upsert_article_ticker


def _make_db(db_path: str, *, with_articles: bool = True) -> sqlite3.Connection:
    """Create a minimal DB with ohlcv and optional articles."""
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_articles_table(conn)

    # OHLCV — 3 trading days
    for dt in ("2026-05-01", "2026-04-30", "2026-04-29"):
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
            ("AAPL", dt),
        )
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 200.0)",
            ("TSLA", dt),
        )

    if with_articles:
        # Raw assets
        for i, (ticker, ref_date) in enumerate([
            ("AAPL", "2026-05-01"),  # FRESH
            ("TSLA", "2026-04-30"),  # STALE (1 day behind)
        ]):
            conn.execute(
                "INSERT OR REPLACE INTO raw_assets "
                "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
                "VALUES (?, ?, 'polygon_news', ?, datetime('now'), ?)",
                (f"raw-f{i}", ticker, ref_date, b"{}"),
            )

        articles_data = [
            ("poly:f1", "raw-f0", "AAPL", "2026-05-01", "Fresh article", "Body."),
            ("poly:f2", "raw-f1", "TSLA", "2026-04-30", "Stale article", "Body."),
        ]
        for art_id, raw_id, ticker, ref_date, title, desc in articles_data:
            upsert_article(conn, article={
                "article_id": art_id,
                "raw_asset_id": raw_id,
                "provider": "polygon",
                "source_type": "polygon_news",
                "ticker": ticker,
                "reference_date": ref_date,
                "published_utc": f"{ref_date}T12:00:00Z",
                "title": title,
                "description": desc,
                "publisher_name": "Test Publisher",
                "source_tier": 5,
            })
            upsert_article_ticker(conn, article_id=art_id, ticker=ticker,
                                  raw_asset_id=raw_id, reference_date=ref_date)

    conn.commit()
    return conn


class TestLatestOHLCVDate:
    def test_returns_max_date(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        assert latest_local_ohlcv_date(conn) == "2026-05-01"
        conn.close()

    def test_raises_on_empty(self, tmp_path):
        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.commit()
        with pytest.raises(RuntimeError, match="ohlcv.*empty"):
            latest_local_ohlcv_date(conn)
        conn.close()


class TestNewsFreshness:
    def test_fresh_and_stale(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        result = news_freshness(conn)
        conn.close()

        pn = result["polygon_news"]
        assert pn["AAPL"]["status"] == "FRESH"
        assert pn["AAPL"]["latest_date"] == "2026-05-01"
        assert pn["TSLA"]["status"] == "STALE"
        assert pn["TSLA"]["days_behind"] == 1

    def test_no_data_ticker(self, tmp_path):
        """Tickers in ohlcv but not in articles get NO_DATA."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path, with_articles=False)
        result = news_freshness(conn)
        conn.close()

        # polygon_news may not exist as a key if no articles at all
        pn = result.get("polygon_news", {})
        assert pn.get("AAPL", {}).get("status") == "NO_DATA"
        assert pn.get("TSLA", {}).get("status") == "NO_DATA"

    def test_accepts_watermark_kwarg(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        result = news_freshness(conn, watermark="2026-04-30")
        conn.close()

        pn = result["polygon_news"]
        # AAPL ref=2026-05-01 > watermark=2026-04-30 → AHEAD
        assert pn["AAPL"]["status"] == "AHEAD"
        # TSLA ref=2026-04-30 == watermark → FRESH
        assert pn["TSLA"]["status"] == "FRESH"


class TestIndexFreshness:
    def test_no_index_when_no_live_manifest(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)
        result = index_freshness(conn)
        conn.close()

        assert result["status"] == "NO_INDEX"
        assert result["stale_count"] > 0

    def test_ignores_dry_run_manifest(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # Insert a dry_run manifest — should be ignored
        conn.execute("""
            INSERT INTO index_manifests
            (build_id, created_at, model, model_hash, lancedb_path,
             l1_count, l2_count, article_count, indexed_through_date,
             corpus_hash, status)
            VALUES (?, datetime('now'), 'bge-m3', 'abc123', '/tmp/test',
                    11772, 0, 11772, '2026-05-01', 'x', 'dry_run')
        """, ("build-dry-1",))
        conn.commit()

        result = index_freshness(conn)
        conn.close()

        assert result["status"] == "NO_INDEX"

    def test_stale_when_articles_not_in_index_state(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # Insert a live manifest
        conn.execute("""
            INSERT INTO index_manifests
            (build_id, created_at, model, model_hash, lancedb_path,
             l1_count, l2_count, article_count, indexed_through_date,
             corpus_hash, status)
            VALUES (?, datetime('now'), 'bge-m3', 'abc123', '/tmp/test',
                    2, 0, 2, '2026-05-01', 'x', 'live')
        """, ("build-live-1",))
        conn.commit()

        result = index_freshness(conn)
        conn.close()

        assert result["status"] == "STALE"
        assert result["stale_count"] > 0


class TestFreshnessReport:
    def test_is_read_only(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # Capture row counts before
        before_articles = conn.execute(
            "SELECT COUNT(*) FROM articles"
        ).fetchone()[0]
        before_at = conn.execute(
            "SELECT COUNT(*) FROM article_tickers"
        ).fetchone()[0]

        report = freshness_report(conn)

        # Verify counts unchanged
        after_articles = conn.execute(
            "SELECT COUNT(*) FROM articles"
        ).fetchone()[0]
        after_at = conn.execute(
            "SELECT COUNT(*) FROM article_tickers"
        ).fetchone()[0]
        conn.close()

        assert after_articles == before_articles
        assert after_at == before_at
        assert "local_ohlcv_date" in report
        assert "news" in report
        assert "index" in report
        assert "generated_at" in report
