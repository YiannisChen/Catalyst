"""Tests for ticker_scope.py — SQLite post-filter (Mac-safe, no LanceDB)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_data.storage.ticker_scope import filter_by_ticker, filter_by_tickers
from catalyst_data.storage.sqlite import init_db
from catalyst_data.articles import ensure_articles_table, upsert_article, upsert_article_ticker


def _make_db(db_path: str) -> sqlite3.Connection:
    """Create a DB with articles + article_tickers for synthetic testing."""
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_articles_table(conn)

    for i in range(5):
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
            "VALUES (?, 'AAPL', 'polygon_news', '2025-01-01', datetime('now'), ?)",
            (f"raw-{i}", b"{}"),
        )
    conn.commit()

    articles = [
        ("poly:a1", "raw-0", "AAPL", "Title 1"),
        ("poly:a2", "raw-1", "AAPL", "Title 2"),
        ("poly:a3", "raw-2", "TSLA", "Title 3"),
        ("poly:a4", "raw-3", "MSFT", "Title 4"),
        ("poly:a5", "raw-4", "JPM", "Title 5"),
    ]
    for article_id, raw_id, ticker, title in articles:
        upsert_article(conn, article={
            "article_id": article_id,
            "raw_asset_id": raw_id,
            "provider": "polygon",
            "source_type": "polygon_news",
            "ticker": ticker,
            "reference_date": "2025-01-01",
            "published_utc": "2025-01-01T12:00:00Z",
            "title": title,
            "publisher_name": "TestPub",
            "source_tier": 4,
        })

    # article_tickers: a2 under AAPL+TSLA, a5 under JPM+MSFT
    data = [
        ("poly:a1", "AAPL", "raw-0", "2025-01-01"),
        ("poly:a2", "AAPL", "raw-1", "2025-01-01"),
        ("poly:a2", "TSLA", "raw-1", "2025-01-01"),
        ("poly:a3", "TSLA", "raw-2", "2025-01-01"),
        ("poly:a4", "MSFT", "raw-3", "2025-01-01"),
        ("poly:a5", "JPM", "raw-4", "2025-01-01"),
        ("poly:a5", "MSFT", "raw-4", "2025-01-01"),
    ]
    for article_id, ticker, raw_id, ref_date in data:
        upsert_article_ticker(conn, article_id=article_id, ticker=ticker,
                              raw_asset_id=raw_id, reference_date=ref_date)
    conn.close()
    return conn


def _result(article_id: str) -> dict:
    return {"article_id": article_id, "score": 0.95, "chunk_id": f"{article_id}::l1"}


class TestFilterByTicker:
    def test_single_ticker_filter(self, tmp_path: Path):
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)

        results = [_result(a) for a in
                   ["poly:a1", "poly:a2", "poly:a3", "poly:a4", "poly:a5"]]
        filtered = filter_by_ticker(results, "AAPL", db_path)
        filtered_ids = {r["article_id"] for r in filtered}
        assert filtered_ids == {"poly:a1", "poly:a2"}

    def test_multi_ticker_article_appears_under_all(self, tmp_path: Path):
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)

        results = [_result("poly:a2")]
        # a2 is under AAPL AND TSLA
        assert len(filter_by_ticker(results, "AAPL", db_path)) == 1
        assert len(filter_by_ticker(results, "TSLA", db_path)) == 1
        assert len(filter_by_ticker(results, "MSFT", db_path)) == 0

    def test_empty_results(self, tmp_path: Path):
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)
        assert filter_by_ticker([], "AAPL", db_path) == []

    def test_no_match_returns_empty(self, tmp_path: Path):
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)
        results = [_result("poly:a4")]  # MSFT only
        filtered = filter_by_ticker(results, "GOOGL", db_path)
        assert filtered == []

    def test_missing_article_id_ignored(self, tmp_path: Path):
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)
        results = [{"score": 0.5}]  # no article_id
        filtered = filter_by_ticker(results, "AAPL", db_path)
        assert filtered == []

    def test_no_lancedb_import(self):
        import catalyst_data.storage.ticker_scope as ts
        source = open(ts.__file__).read()
        assert "import lancedb" not in source
        assert "from lancedb" not in source


class TestFilterByTickers:
    def test_multi_ticker_union(self, tmp_path: Path):
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)

        results = [_result(a) for a in
                   ["poly:a1", "poly:a2", "poly:a3", "poly:a4", "poly:a5"]]
        filtered = filter_by_tickers(results, ["AAPL", "TSLA"], db_path)
        filtered_ids = {r["article_id"] for r in filtered}
        assert filtered_ids == {"poly:a1", "poly:a2", "poly:a3"}

    def test_empty_tickers(self, tmp_path: Path):
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)
        assert filter_by_tickers([_result("poly:a1")], [], db_path) == []

    def test_empty_results_multi(self, tmp_path: Path):
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)
        assert filter_by_tickers([], ["AAPL"], db_path) == []
