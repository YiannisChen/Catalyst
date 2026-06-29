"""Tests for offline re-derive: polygon_news raw_assets → articles table."""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from catalyst_data.articles import ensure_articles_table, upsert_article
from catalyst_data.rederive import _load_trading_calendar, _parse_article, rederive_polygon_news
from catalyst_data.storage.sqlite import init_db, upsert_raw_asset


def _seed_raw_asset(
    conn: sqlite3.Connection,
    asset_id: str,
    ticker: str,
    articles: list[dict],
) -> None:
    """Insert a raw_asset with a synthetic Polygon news payload."""
    payload = json.dumps({"news": {"results": articles}}).encode("utf-8")
    compressed = zlib.compress(payload)
    conn.execute(
        """INSERT OR REPLACE INTO raw_assets
           (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES (?, ?, 'polygon_news', ?, datetime('now'), ?)""",
        (asset_id, ticker, "2025-01-02", compressed),
    )
    conn.commit()


def _seed_ohlcv(conn: sqlite3.Connection) -> None:
    """Insert a minimal trading calendar."""
    for date in ("2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08"):
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, open, high, low, close, volume) "
            "VALUES (?, ?, 100, 105, 99, 103, 1000)",
            ("AAPL", date),
        )
    conn.commit()


def test_rederive_idempotent(tmp_path: Path):
    """Running re-derive twice produces the same article count."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)
    _seed_ohlcv(conn)
    _seed_raw_asset(
        conn,
        "raw-1",
        "AAPL",
        [
            {"id": "a1", "title": "Article 1", "published_utc": "2025-01-02T10:00:00Z",
             "description": "desc1", "article_url": "http://x.com/1"},
            {"id": "a2", "title": "Article 2", "published_utc": "2025-01-02T12:00:00Z",
             "description": "desc2", "article_url": "http://x.com/2"},
        ],
    )
    conn.close()

    result1 = rederive_polygon_news(str(db))
    result2 = rederive_polygon_news(str(db))

    assert result1["articles_upserted"] == result2["articles_upserted"]
    assert result1["articles_upserted"] == 2
    assert result2["articles_skipped"] == 0


def test_rederive_n_in_n_out(tmp_path: Path):
    """A raw_asset with N articles produces N article rows."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)
    _seed_ohlcv(conn)
    _seed_raw_asset(
        conn,
        "raw-1",
        "AAPL",
        [
            {"id": f"a{i}", "title": f"Article {i}",
             "published_utc": "2025-01-02T10:00:00Z",
             "description": f"desc{i}", "article_url": f"http://x.com/{i}"}
            for i in range(5)
        ],
    )
    conn.close()

    result = rederive_polygon_news(str(db))
    assert result["articles_upserted"] == 5

    # Verify count in DB
    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    assert count == 5
    conn.close()


def test_rederive_provenance_completeness(tmp_path: Path):
    """Every article row joins back to raw_assets."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)
    _seed_ohlcv(conn)
    _seed_raw_asset(conn, "raw-1", "AAPL", [
        {"id": "a1", "title": "T", "published_utc": "2025-01-02T10:00:00Z",
         "description": "d", "article_url": "http://x.com/1"},
    ])
    conn.close()

    rederive_polygon_news(str(db))

    conn = sqlite3.connect(str(db))
    orphans = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE raw_asset_id NOT IN (SELECT asset_id FROM raw_assets)"
    ).fetchone()[0]
    assert orphans == 0
    conn.close()


def test_rederive_field_mapping(tmp_path: Path):
    """All provenance fields mapped correctly."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)
    _seed_ohlcv(conn)
    _seed_raw_asset(conn, "raw-1", "AAPL", [
        {
            "id": "abc123",
            "title": "AMD Earnings Report",
            "published_utc": "2025-01-02T10:00:00Z",
            "description": "AMD reports strong earnings.",
            "article_url": "https://example.com/amd",
            "image_url": "https://example.com/amd.jpg",
            "author": "Jane Doe",
            "publisher": {
                "name": "Motley Fool",
                "homepage_url": "https://fool.com",
                "logo_url": "https://fool.com/logo.svg",
                "favicon_url": "https://fool.com/favicon.ico",
            },
            "keywords": ["AMD", "earnings"],
            "insights": [{"ticker": "AMD", "sentiment": "positive", "sentiment_reasoning": "Strong"}],
            "tickers": ["AMD"],
        },
    ])
    conn.close()

    rederive_polygon_news(str(db))

    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT * FROM articles WHERE article_id = 'poly:abc123'").fetchone()
    assert row is not None

    cols = [d[0] for d in conn.execute("SELECT * FROM articles LIMIT 0").description]
    d = dict(zip(cols, row))

    assert d["article_id"] == "poly:abc123"
    assert d["title"] == "AMD Earnings Report"
    assert d["description"] == "AMD reports strong earnings."
    assert d["article_url"] == "https://example.com/amd"
    assert d["image_url"] == "https://example.com/amd.jpg"
    assert d["author"] == "Jane Doe"
    assert d["publisher_name"] == "Motley Fool"
    assert d["publisher_logo_url"] == "https://fool.com/logo.svg"
    assert d["publisher_favicon_url"] == "https://fool.com/favicon.ico"
    assert d["publisher_homepage_url"] == "https://fool.com"
    assert json.loads(d["keywords_json"]) == ["AMD", "earnings"]
    insights = json.loads(d["insights_json"])
    assert len(insights) == 1
    assert insights[0]["ticker"] == "AMD"
    assert json.loads(d["tickers_json"]) == ["AMD"]
    conn.close()


def test_rederive_reference_date_alignment(tmp_path: Path):
    """published_utc maps to correct reference_date via trading calendar."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)
    _seed_ohlcv(conn)

    # Before 16:00 ET → same trading day
    _seed_raw_asset(conn, "raw-1", "AAPL", [
        {"id": "a1", "title": "Morning", "published_utc": "2025-01-02T15:00:00Z",
         "description": "d", "article_url": "http://x.com"},
    ])
    conn.close()

    rederive_polygon_news(str(db))

    conn = sqlite3.connect(str(db))
    ref = conn.execute(
        "SELECT reference_date FROM articles WHERE article_id = 'poly:a1'"
    ).fetchone()[0]
    assert ref == "2025-01-02"  # Before 16:00 ET → same day
    conn.close()


def test_rederive_payload_decoding(tmp_path: Path):
    """Handles edge cases: empty results, malformed JSON, missing fields."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)
    _seed_ohlcv(conn)

    # Empty results
    payload = json.dumps({"news": {"results": []}}).encode("utf-8")
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw-empty', 'AAPL', 'polygon_news', '2025-01-02', datetime('now'), ?)""",
        (zlib.compress(payload),),
    )

    # Malformed JSON
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw-bad', 'AAPL', 'polygon_news', '2025-01-02', datetime('now'), ?)""",
        (zlib.compress(b"not json"),),
    )

    # Missing fields
    payload3 = json.dumps({"news": {"results": [{"id": "minimal", "title": "Min"}]}}).encode("utf-8")
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw-min', 'AAPL', 'polygon_news', '2025-01-02', datetime('now'), ?)""",
        (zlib.compress(payload3),),
    )
    conn.commit()
    conn.close()

    result = rederive_polygon_news(str(db))
    # Should handle all without crashing; minimal article still upserted
    assert result["articles_upserted"] >= 1
    assert result["articles_skipped"] >= 0


def test_rederive_missing_calendar_is_error(tmp_path: Path):
    """Raises RuntimeError when ohlcv table is empty."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)
    _seed_raw_asset(conn, "raw-1", "AAPL", [
        {"id": "a1", "title": "T", "published_utc": "2025-01-02T10:00:00Z",
         "description": "d", "article_url": "http://x.com"},
    ])
    conn.close()

    with pytest.raises(RuntimeError, match="Trading calendar"):
        rederive_polygon_news(str(db))


def test_rederive_batch_commit(tmp_path: Path):
    """Processing many raw_assets does not crash."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)
    _seed_ohlcv(conn)

    for i in range(150):
        _seed_raw_asset(conn, f"raw-{i}", "AAPL", [
            {"id": f"a{i}-1", "title": f"Article {i}",
             "published_utc": "2025-01-02T10:00:00Z",
             "description": f"desc{i}", "article_url": f"http://x.com/{i}"},
        ])
    conn.close()

    result = rederive_polygon_news(str(db))
    assert result["articles_upserted"] == 150
    assert result["raw_rows_processed"] == 150
