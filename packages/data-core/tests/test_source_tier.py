"""Tests for source_tier.py — publisher→tier classifier."""

from __future__ import annotations

import sqlite3
import logging
from pathlib import Path

from catalyst_data.source_tier import (
    tier_for_publisher,
    classify_articles,
    tier_distribution,
    tier_label,
)
from catalyst_data.storage.sqlite import init_db
from catalyst_data.articles import ensure_articles_table


def _make_article_db(db_path: str) -> sqlite3.Connection:
    """Create a DB with articles table and 8 test publishers (all source_tier NULL)."""
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_articles_table(conn)

    # Insert raw_assets first for FK
    for i in range(8):
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES (?, 'AAPL', 'polygon_news', '2025-01-01', datetime('now'), ?)",
            (f"raw-{i}", b"{}"),
        )
    conn.commit()

    articles_data = [
        ("poly:mf1", "raw-0", "AAPL", "The Motley Fool"),
        ("poly:bz1", "raw-1", "TSLA", "Benzinga"),
        ("poly:gn1", "raw-2", "MSFT", "GlobeNewswire Inc."),
        ("poly:ic1", "raw-3", "AMZN", "Investing.com"),
        ("poly:mw1", "raw-4", "GOOGL", "MarketWatch"),
        ("poly:z1", "raw-5", "META", "Zacks Investment Research"),
        ("poly:up1", "raw-6", "NVDA", "Unknown Publisher LLC"),
        ("poly:nil1", "raw-7", "AMD", None),
    ]
    for article_id, raw_id, ticker, publisher in articles_data:
        conn.execute(
            """INSERT OR REPLACE INTO articles
               (article_id, raw_asset_id, provider, source_type, ticker,
                reference_date, published_utc, title, publisher_name)
               VALUES (?, ?, 'polygon', 'polygon_news', ?, '2025-01-01',
                       '2025-01-01T12:00:00Z', 'Test Article', ?)""",
            (article_id, raw_id, ticker, publisher),
        )
    conn.commit()
    return conn


class TestTierForPublisher:
    def test_known_publishers_exact(self):
        assert tier_for_publisher("MarketWatch") == 2
        assert tier_for_publisher("GlobeNewswire") == 3
        assert tier_for_publisher("GlobeNewswire Inc.") == 3
        assert tier_for_publisher("Benzinga") == 4
        assert tier_for_publisher("Investing.com") == 4
        assert tier_for_publisher("The Motley Fool") == 5
        assert tier_for_publisher("Motley Fool") == 5
        assert tier_for_publisher("Zacks") == 5
        assert tier_for_publisher("Zacks Investment Research") == 5

    def test_unknown_publisher_t4(self):
        assert tier_for_publisher("Random Blog Inc.") == 4

    def test_none_publisher_t4(self):
        assert tier_for_publisher(None) == 4

    def test_empty_string_t4(self):
        assert tier_for_publisher("") == 4


class TestClassifyArticles:
    def test_all_null_become_non_null(self, tmp_path: Path):
        conn = _make_article_db(str(tmp_path / "test.db"))

        null_count = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE source_tier IS NULL"
        ).fetchone()[0]
        assert null_count == 8

        count = classify_articles(conn)
        assert count == 8

        null_count2 = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE source_tier IS NULL"
        ).fetchone()[0]
        assert null_count2 == 0
        conn.close()

    def test_idempotent(self, tmp_path: Path):
        conn = _make_article_db(str(tmp_path / "test.db"))

        first = classify_articles(conn)
        assert first == 8

        second = classify_articles(conn)
        assert second == 0
        conn.close()

    def test_tier_assignments_correct(self, tmp_path: Path):
        conn = _make_article_db(str(tmp_path / "test.db"))
        classify_articles(conn)

        rows = conn.execute(
            "SELECT article_id, source_tier FROM articles ORDER BY article_id"
        ).fetchall()

        expected = {
            "poly:bz1": 4,
            "poly:gn1": 3,
            "poly:ic1": 4,
            "poly:mf1": 5,
            "poly:mw1": 2,
            "poly:nil1": 4,
            "poly:up1": 4,
            "poly:z1": 5,
        }
        for article_id, tier in rows:
            assert tier == expected[article_id], (
                f"{article_id}: expected {expected[article_id]}, got {tier}"
            )
        conn.close()

    def test_unknown_logged_once(self, tmp_path: Path, caplog):
        conn = _make_article_db(str(tmp_path / "test.db"))

        with caplog.at_level(logging.WARNING):
            classify_articles(conn)

        unknown_warnings = [
            r for r in caplog.records if "Unknown publisher" in r.message
        ]
        assert len(unknown_warnings) >= 2, (
            f"Expected ≥2 warnings, got {len(unknown_warnings)}"
        )

        # Second run — no new warnings
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            classify_articles(conn)

        second_warnings = [
            r for r in caplog.records if "Unknown publisher" in r.message
        ]
        assert len(second_warnings) == 0, (
            f"Idempotent: expected 0 warnings, got {len(second_warnings)}"
        )
        conn.close()


class TestTierDistribution:
    def test_distribution_matches(self, tmp_path: Path):
        conn = _make_article_db(str(tmp_path / "test.db"))
        classify_articles(conn)

        dist = tier_distribution(conn)
        assert dist[2] == 1  # MarketWatch
        assert dist[3] == 1  # GlobeNewswire
        assert dist[4] == 4  # Benzinga + Investing.com + Unknown + None
        assert dist[5] == 2  # Motley Fool + Zacks
        assert sum(dist.values()) == 8
        conn.close()


class TestTierLabel:
    def test_labels(self):
        assert "T1" in tier_label(1)
        assert "T2" in tier_label(2)
        assert "T3" in tier_label(3)
        assert "T4" in tier_label(4)
        assert "T5" in tier_label(5)
        assert "T6" in tier_label(6)
        assert "Unknown" in tier_label(99)
