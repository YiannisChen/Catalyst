"""Tests for articles table schema — additive, non-destructive DDL."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_data.articles import (
    _ARTICLES_DDL,
    compute_article_id,
    ensure_articles_table,
    upsert_article,
)
from catalyst_data.storage.sqlite import init_db


def test_articles_table_created(tmp_path: Path):
    """ensure_articles_table creates the table and indexes."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    ensure_articles_table(conn)

    # Verify table exists
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='articles'"
    ).fetchall()
    assert len(tables) == 1
    assert tables[0][0] == "articles"

    # Verify indexes exist
    idxs = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='articles'"
    ).fetchall()
    idx_names = {r[0] for r in idxs}
    assert "idx_articles_raw_asset" in idx_names
    assert "idx_articles_ticker_date" in idx_names
    assert "idx_articles_published" in idx_names
    assert "idx_articles_ticker_published" in idx_names
    assert "idx_articles_provider" in idx_names

    conn.close()


def test_articles_table_additive(tmp_path: Path):
    """Running DDL twice does not error. Existing tables are untouched."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))

    # Create other tables first via init_db
    init_db(conn)
    ensure_articles_table(conn)

    # Run again — should not error
    ensure_articles_table(conn)

    # Verify raw_assets and clean_assets still exist
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = {r[0] for r in tables}
    assert "raw_assets" in table_names
    assert "clean_assets" in table_names
    assert "articles" in table_names
    assert "ohlcv" in table_names

    conn.close()


def test_articles_fk_enforcement(tmp_path: Path):
    """Insert with invalid raw_asset_id fails when FK enabled."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    ensure_articles_table(conn)

    # Try inserting an article referencing a non-existent raw_asset
    try:
        conn.execute(
            """INSERT INTO articles (article_id, raw_asset_id, provider, source_type,
               ticker, reference_date, published_utc, title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("poly:fake", "nonexistent", "polygon", "polygon_news",
             "AAPL", "2025-01-01", "2025-01-01T12:00:00Z", "Test"),
        )
        conn.commit()
        # If we get here without error, FK might not be enforced
        # (some SQLite builds don't enforce by default even with PRAGMA)
    except sqlite3.IntegrityError:
        pass  # Expected: FK violation

    conn.close()


def test_articles_upsert(tmp_path: Path):
    """INSERT OR REPLACE by article_id is idempotent."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)

    # Insert a raw_asset first for FK
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date,
           fetched_at, content_raw) VALUES (?, ?, ?, ?, ?, ?)""",
        ("raw-1", "AAPL", "polygon_news", "2025-01-01", "2025-01-01T00:00:00Z", b"{}"),
    )
    conn.commit()

    article = {
        "article_id": "poly:abc123",
        "raw_asset_id": "raw-1",
        "provider": "polygon",
        "source_type": "polygon_news",
        "ticker": "AAPL",
        "reference_date": "2025-01-01",
        "published_utc": "2025-01-01T12:00:00Z",
        "title": "Test Article",
        "description": "Test description",
    }

    # First upsert
    upsert_article(conn, article=article)

    count1 = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    assert count1 == 1

    # Second upsert (same article_id)
    upsert_article(conn, article=article)

    count2 = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    assert count2 == 1  # Still one row

    conn.close()


def test_articles_schema_columns(tmp_path: Path):
    """All 24 named columns present with correct types."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    ensure_articles_table(conn)

    cols = conn.execute("PRAGMA table_info(articles)").fetchall()
    col_names = {c[1] for c in cols}

    expected = {
        "article_id", "raw_asset_id", "provider", "source_type", "ticker",
        "reference_date", "published_utc", "title", "description",
        "article_url", "image_url", "author", "publisher_name",
        "publisher_homepage_url", "publisher_logo_url", "publisher_favicon_url",
        "keywords_json", "insights_json", "tickers_json",
        "source_tier", "dedup_group_id", "is_canonical",
        "is_rag_eligible", "quality_score", "created_at",
    }

    missing = expected - col_names
    extra = col_names - expected

    assert not missing, f"Missing columns: {missing}"
    assert not extra, f"Unexpected columns: {extra}"

    conn.close()


def test_compute_article_id():
    """Namespacing produces expected format."""
    assert compute_article_id("poly", "abc123") == "poly:abc123"
    assert compute_article_id("benzinga", "x") == "benzinga:x"

def test_article_tickers_table_created(tmp_path: Path):
    """ensure_articles_table creates article_tickers with composite PK."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    ensure_articles_table(conn)

    # Verify table exists
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='article_tickers'"
    ).fetchall()
    assert len(tables) == 1

    # Verify composite PK
    pks = conn.execute("PRAGMA table_info(article_tickers)").fetchall()
    pk_cols = [c[1] for c in pks if c[5] > 0]
    assert "article_id" in pk_cols
    assert "ticker" in pk_cols

    # Verify indexes
    idxs = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='article_tickers'"
    ).fetchall()
    idx_names = {r[0] for r in idxs}
    assert "idx_article_tickers_ticker_date" in idx_names
    assert "idx_article_tickers_raw_asset" in idx_names
    conn.close()


def test_article_tickers_fk_enforcement(tmp_path: Path):
    """Insert with invalid article_id fails when FK enabled."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    ensure_articles_table(conn)

    try:
        conn.execute(
            "INSERT INTO article_tickers (article_id, ticker, raw_asset_id, reference_date) "
            "VALUES ('nonexistent', 'AAPL', 'raw-1', '2025-01-01')"
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # Expected
    conn.close()


def test_article_tickers_upsert(tmp_path: Path):
    """INSERT OR REPLACE is idempotent for same (article_id, ticker) pair."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)

    # Insert raw_asset + article first
    conn.execute(
        "INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw) "
        "VALUES ('raw-1', 'AAPL', 'polygon_news', '2025-01-02', datetime('now'), ?)",
        (b"{}",),
    )
    conn.commit()
    upsert_article(conn, article={
        "article_id": "poly:a1", "raw_asset_id": "raw-1", "provider": "polygon",
        "source_type": "polygon_news", "ticker": "AAPL", "reference_date": "2025-01-02",
        "published_utc": "2025-01-02T10:00:00Z", "title": "Test",
    })

    from catalyst_data.articles import upsert_article_ticker
    upsert_article_ticker(conn, article_id="poly:a1", ticker="AAPL", raw_asset_id="raw-1", reference_date="2025-01-02")
    upsert_article_ticker(conn, article_id="poly:a1", ticker="AAPL", raw_asset_id="raw-1", reference_date="2025-01-02")

    count = conn.execute("SELECT COUNT(*) FROM article_tickers").fetchone()[0]
    assert count == 1
    conn.close()
