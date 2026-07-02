"""Tests for publisher tiering with Finnhub publishers."""
from __future__ import annotations

import sqlite3

from catalyst_data.source_tier import tier_for_publisher, classify_articles
from catalyst_data.articles import ensure_articles_table, upsert_article, compute_article_id
from catalyst_data.storage.sqlite import init_db


def _make_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_articles_table(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


class TestPublisherTierMapping:
    def test_seeking_alpha_tier_5(self):
        assert tier_for_publisher("Seeking Alpha") == 5

    def test_reuters_tier_2(self):
        assert tier_for_publisher("Reuters") == 2

    def test_bloomberg_tier_2(self):
        assert tier_for_publisher("Bloomberg") == 2

    def test_wsj_tier_2(self):
        assert tier_for_publisher("The Wall Street Journal") == 2

    def test_business_wire_tier_3(self):
        assert tier_for_publisher("Business Wire") == 3

    def test_pr_newswire_tier_3(self):
        assert tier_for_publisher("PR Newswire") == 3

    def test_yahoo_tier_4(self):
        assert tier_for_publisher("Yahoo") == 4

    def test_yahoo_finance_tier_4(self):
        assert tier_for_publisher("Yahoo Finance") == 4

    def test_cnbc_tier_4(self):
        assert tier_for_publisher("CNBC") == 4

    def test_marketbeat_tier_4(self):
        assert tier_for_publisher("MarketBeat") == 4

    def test_tipranks_tier_5(self):
        assert tier_for_publisher("TipRanks") == 5

    def test_24_7_wall_st_tier_5(self):
        assert tier_for_publisher("24/7 Wall St.") == 5

    def test_unknown_publisher_tier_4(self):
        assert tier_for_publisher("SomeUnknownBlog") == 4

    def test_none_publisher_tier_4(self):
        assert tier_for_publisher(None) == 4

    def test_whitespace_publisher_tier_4(self):
        assert tier_for_publisher("   ") == 4


class TestClassifyArticles:
    def test_classify_articles_sets_finnhub_tiers(self, tmp_path):
        """Full flow: insert Finnhub article, classify, assert tier."""
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        aid = compute_article_id("finnhub", "999")
        upsert_article(conn, article={
            "article_id": aid,
            "raw_asset_id": "raw_test",
            "provider": "finnhub",
            "source_type": "finnhub_company_news",
            "ticker": "AAPL",
            "reference_date": "2025-07-01",
            "published_utc": "2025-07-01T00:00:00+00:00",
            "title": "Test Article",
            "description": "Test",
            "article_url": "https://example.com",
            "image_url": None,
            "author": None,
            "publisher_name": "Yahoo",
            "publisher_homepage_url": None,
            "publisher_logo_url": None,
            "publisher_favicon_url": None,
            "keywords_json": "[]",
            "insights_json": "[]",
            "tickers_json": '["AAPL"]',
            "source_tier": None,
            "dedup_group_id": None,
            "is_canonical": 1,
            "is_rag_eligible": 1,
            "quality_score": 1.0,
        })

        classified = classify_articles(conn)
        assert classified >= 1

        row = conn.execute(
            "SELECT source_tier FROM articles WHERE article_id = ?", (aid,)
        ).fetchone()
        assert row[0] == 4  # Yahoo → T4

        conn.close()
