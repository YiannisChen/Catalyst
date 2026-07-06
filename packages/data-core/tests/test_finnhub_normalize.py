"""Tests for Finnhub normalization + rederive — mock-only, zero network."""
from __future__ import annotations

import json
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from catalyst_data.articles import ensure_articles_table
from catalyst_data.pipeline.finnhub_normalize import rederive_finnhub_news, _parse_finnhub_article
from catalyst_data.storage.sqlite import init_db, compute_asset_id, upsert_raw_asset, get_raw_asset

FIXTURES = Path(__file__).parent / "fixtures"


def _make_db_with_calendar(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_articles_table(conn)
    # Minimal trading calendar
    for dt in ("2025-06-30", "2025-07-01", "2025-07-02"):
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES (?, ?, 100.0)",
            ("AAPL", dt),
        )
    conn.commit()
    return conn


class TestDatetimeConversion:
    def test_unix_to_iso(self):
        """Unix timestamp → correct ISO 8601 with UTC offset."""
        ts = 1751385600
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt.year == 2025
        assert dt.month == 7
        # July 1, 2025 00:00 UTC (allow for timezone conversion)
        iso = dt.isoformat()
        assert "2025-07-01" in iso
        assert "+00:00" in iso

    def test_deprecated_not_used(self):
        """Verify datetime.fromtimestamp used — not utcfromtimestamp."""
        from catalyst_data.pipeline import finnhub_normalize
        import inspect
        src = inspect.getsource(finnhub_normalize._parse_finnhub_article)
        assert "fromtimestamp" in src
        assert "utcfromtimestamp" not in src


class TestFieldMapping:
    def test_publisher_name_from_source(self):
        """source='Yahoo' maps to publisher_name='Yahoo'."""
        article = {
            "id": 1, "headline": "Test", "summary": "Summary",
            "datetime": 1751385600, "source": "Yahoo", "url": "https://example.com"
        }
        with open("/tmp/test_cal.json", "w") as f:
            json.dump([article], f)

        row = _parse_finnhub_article(article, "raw_1", "AAPL", ["2025-07-01", "2025-07-02"])
        assert row is not None
        assert row["publisher_name"] == "Yahoo"
        assert row["provider"] == "finnhub"
        assert row["source_type"] == "finnhub_company_news"

    def test_article_tickers_is_query_symbol(self):
        """article_tickers references the query symbol."""
        article = {
            "id": 2, "headline": "News", "summary": "Body",
            "datetime": 1751385600, "source": "Reuters", "url": "https://x.com"
        }
        row = _parse_finnhub_article(article, "raw_2", "AAPL", ["2025-07-01", "2025-07-02"])
        assert row is not None
        assert row["ticker"] == "AAPL"
        tickers = json.loads(row["tickers_json"])
        assert tickers == ["AAPL"]

    def test_article_id_namespaced(self):
        """article_id uses 'finnhub:' prefix."""
        from catalyst_data.articles import compute_article_id
        aid = compute_article_id("finnhub", "12345")
        assert aid == "finnhub:12345"

    def test_l1_only_match(self):
        """Summary < 800 chars → is_rag_eligible=1 (L1, no L2)."""
        article = {
            "id": 3, "headline": "Short", "summary": "X" * 150,
            "datetime": 1751385600, "source": "Yahoo", "url": ""
        }
        row = _parse_finnhub_article(article, "raw_3", "AAPL", ["2025-07-01"])
        assert row is not None
        assert row["is_rag_eligible"] == 1
        # Description is ~150 chars — well below 800 threshold
        assert len(row["description"]) < 800


class TestBronzeRedervability:
    def test_bronze_roundtrip(self, tmp_path):
        """Store Finnhub raw_asset → decompress → re-normalize → articles match."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db_with_calendar(db_path)

        # Store a Finnhub raw_asset
        with open(FIXTURES / "finnhub_company_news_AAPL.json") as f:
            articles_data = json.load(f)

        raw_bytes = json.dumps(articles_data, ensure_ascii=True).encode("utf-8")
        asset_id = compute_asset_id("AAPL", "2025-07-01", "finnhub_company_news")

        upsert_raw_asset(
            conn,
            asset_id=asset_id,
            ticker="AAPL",
            source_type="finnhub_company_news",
            reference_date="2025-07-01",
            content_raw=raw_bytes,
            http_status=200,
            metadata={"endpoints": ["company-news"], "article_count": 3},
        )
        conn.close()

        # Re-derive
        counts = rederive_finnhub_news(db_path)
        assert counts["articles_upserted"] == 3

        # Verify decompress + re-read
        conn = sqlite3.connect(db_path)
        raw = get_raw_asset(conn, asset_id)
        assert raw is not None
        decompressed = raw["content_raw"]
        assert decompressed is not None
        # Decompressed JSON should match original
        assert b"Apple Reports Record Q2 Earnings" in decompressed

        # Verify articles in DB
        rows = conn.execute(
            "SELECT article_id, title, publisher_name, provider FROM articles WHERE provider='finnhub'"
        ).fetchall()
        assert len(rows) == 3
        titles = [r[1] for r in rows]
        assert "Apple Reports Record Q2 Earnings" in titles

        # Verify article_tickers
        at_rows = conn.execute(
            "SELECT ticker FROM article_tickers WHERE article_id = ?", (rows[0][0],)
        ).fetchall()
        assert at_rows[0][0] == "AAPL"

        conn.close()

    def test_post_ohlcv_ceiling_maps_reference_date(self, tmp_path):
        """Publication after local OHLCV ceiling still maps via calendar oracle."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_articles_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, close) VALUES "
            "('AAPL', '2026-05-01', 100.0)"
        )
        article = {
            "id": 999,
            "headline": "Post ceiling Finnhub",
            "summary": "Body",
            "datetime": int(datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc).timestamp()),
            "source": "Yahoo",
            "url": "https://example.com/post",
        }
        asset_id = compute_asset_id("AAPL", "2026-06-15", "finnhub_company_news")
        upsert_raw_asset(
            conn,
            asset_id=asset_id,
            ticker="AAPL",
            source_type="finnhub_company_news",
            reference_date="2026-06-15",
            content_raw=json.dumps([article]).encode("utf-8"),
            http_status=200,
            metadata={"article_count": 1},
        )
        conn.close()

        rederive_finnhub_news(db_path)

        conn = sqlite3.connect(db_path)
        ref = conn.execute(
            "SELECT reference_date FROM articles WHERE article_id = 'finnhub:999'"
        ).fetchone()[0]
        conn.close()

        assert ref == "2026-06-15"
