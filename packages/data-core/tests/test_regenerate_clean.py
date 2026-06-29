"""Tests for historical clean_assets regeneration from article_tickers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_data.articles import ensure_articles_table, upsert_article, upsert_article_ticker
from catalyst_data.regenerate_clean import regenerate_polygon_clean_assets
from catalyst_data.storage.sqlite import init_db, upsert_clean_asset


def _seed_article(
    conn: sqlite3.Connection,
    article_id: str,
    ticker: str = "AAPL",
    raw_asset_id: str = "raw-1",
    **kwargs,
) -> None:
    """Insert a minimal article + article_tickers row."""
    defaults = {
        "article_id": article_id,
        "raw_asset_id": raw_asset_id,
        "provider": "polygon",
        "source_type": "polygon_news",
        "ticker": ticker,
        "reference_date": "2025-01-02",
        "published_utc": "2025-01-02T10:00:00Z",
        "title": f"Title for {article_id}",
        "description": f"Description for {article_id}",
        "article_url": f"https://example.com/{article_id}",
        "publisher_name": "Test Publisher",
    }
    defaults.update(kwargs)
    upsert_article(conn, article=defaults)
    upsert_article_ticker(
        conn,
        article_id=article_id,
        ticker=ticker,
        raw_asset_id=raw_asset_id,
        reference_date=defaults["reference_date"],
    )


def _seed_non_news_clean(conn: sqlite3.Connection) -> None:
    """Insert a non-news clean_asset to verify it survives."""
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date,
           fetched_at, content_raw) VALUES ('raw-nonnews', 'AAPL', 'fmp_fundamentals',
           '2025-01-02', datetime('now'), ?)""",
        (b"{}",),
    )
    conn.commit()
    upsert_clean_asset(
        conn,
        asset_id="raw-nonnews",
        ticker="AAPL",
        source_type="fmp_fundamentals",
        reference_date="2025-01-02",
        content_md="## Fundamentals\nData here",
    )


def test_regenerate_idempotent(tmp_path: Path):
    """Running regenerate twice produces the same clean_assets count."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)

    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date,
           fetched_at, content_raw) VALUES ('raw-1', 'AAPL', 'polygon_news',
           '2025-01-02', datetime('now'), ?)""",
        (b"{}",),
    )
    conn.commit()

    _seed_article(conn, "poly:a1")
    _seed_article(conn, "poly:a2")
    conn.close()

    result1 = regenerate_polygon_clean_assets(str(db))
    result2 = regenerate_polygon_clean_assets(str(db))

    assert result1["inserted_article_rows"] == result2["inserted_article_rows"]
    assert result1["inserted_article_rows"] == 2


def test_regenerate_n_in_n_out(tmp_path: Path):
    """Each article_tickers row → one clean_asset row."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)

    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date,
           fetched_at, content_raw) VALUES ('raw-1', 'AAPL', 'polygon_news',
           '2025-01-02', datetime('now'), ?)""",
        (b"{}",),
    )
    conn.commit()

    n = 25
    for i in range(n):
        _seed_article(conn, f"poly:a{i}")
    conn.close()

    result = regenerate_polygon_clean_assets(str(db))
    assert result["inserted_article_rows"] == n

    conn = sqlite3.connect(str(db))
    count = conn.execute(
        "SELECT COUNT(*) FROM clean_assets WHERE source_type = 'polygon_news'"
    ).fetchone()[0]
    assert count == n
    conn.close()


def test_non_news_untouched(tmp_path: Path):
    """Non-news clean_assets survive regeneration."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)

    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date,
           fetched_at, content_raw) VALUES ('raw-1', 'AAPL', 'polygon_news',
           '2025-01-02', datetime('now'), ?)""",
        (b"{}",),
    )
    conn.commit()

    _seed_article(conn, "poly:a1")
    _seed_non_news_clean(conn)
    conn.close()

    regenerate_polygon_clean_assets(str(db))

    conn = sqlite3.connect(str(db))
    non_news_count = conn.execute(
        "SELECT COUNT(*) FROM clean_assets WHERE source_type != 'polygon_news'"
    ).fetchone()[0]
    assert non_news_count == 1
    conn.close()


def test_provenance_intact(tmp_path: Path):
    """Every regenerated clean_asset has non-null raw_asset_id."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)

    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date,
           fetched_at, content_raw) VALUES ('raw-1', 'AAPL', 'polygon_news',
           '2025-01-02', datetime('now'), ?)""",
        (b"{}",),
    )
    conn.commit()

    _seed_article(conn, "poly:a1", raw_asset_id="raw-1")
    _seed_article(conn, "poly:a2", raw_asset_id="raw-1")
    conn.close()

    regenerate_polygon_clean_assets(str(db))

    conn = sqlite3.connect(str(db))
    null_count = conn.execute(
        """SELECT COUNT(*) FROM clean_assets
           WHERE source_type = 'polygon_news' AND raw_asset_id IS NULL"""
    ).fetchone()[0]
    assert null_count == 0

    rows = conn.execute(
        "SELECT asset_id, raw_asset_id FROM clean_assets WHERE source_type = 'polygon_news'"
    ).fetchall()
    for asset_id, raw_id in rows:
        assert raw_id == "raw-1"
    conn.close()


def test_legacy_digests_gone(tmp_path: Path):
    """No clean_asset row has multi-article content."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)

    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date,
           fetched_at, content_raw) VALUES ('raw-1', 'AAPL', 'polygon_news',
           '2025-01-02', datetime('now'), ?)""",
        (b"{}",),
    )
    conn.commit()

    _seed_article(conn, "poly:a1", title="First Unique Article")
    _seed_article(conn, "poly:a2", title="Second Unique Article")
    conn.close()

    regenerate_polygon_clean_assets(str(db))

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT content_md FROM clean_assets WHERE source_type = 'polygon_news'"
    ).fetchall()

    for (content_md,) in rows:
        header_count = content_md.count("## AAPL:")
        assert header_count == 1, (
            f"Digest detected! Found {header_count} headers in:\n{content_md[:200]}"
        )
    conn.close()


def test_multi_ticker_coverage(tmp_path: Path):
    """An article returned for >=2 tickers appears in all those tickers' clean_assets."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)

    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date,
           fetched_at, content_raw) VALUES ('raw-1', 'AAPL', 'polygon_news',
           '2025-01-02', datetime('now'), ?)""",
        (b"{}",),
    )
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date,
           fetched_at, content_raw) VALUES ('raw-2', 'MSFT', 'polygon_news',
           '2025-01-02', datetime('now'), ?)""",
        (b"{}",),
    )
    conn.commit()

    # Same article appears under AAPL and MSFT
    _seed_article(conn, "poly:multi1", ticker="AAPL", raw_asset_id="raw-1")
    _seed_article(conn, "poly:multi1", ticker="MSFT", raw_asset_id="raw-2")
    conn.close()

    regenerate_polygon_clean_assets(str(db))

    conn = sqlite3.connect(str(db))
    # Should have 2 clean_assets for the same article under different tickers
    count = conn.execute(
        "SELECT COUNT(*) FROM clean_assets WHERE source_type = 'polygon_news'"
    ).fetchone()[0]
    assert count == 2

    aapl = conn.execute(
        "SELECT content_md FROM clean_assets WHERE ticker = 'AAPL' AND source_type = 'polygon_news'"
    ).fetchone()
    msft = conn.execute(
        "SELECT content_md FROM clean_assets WHERE ticker = 'MSFT' AND source_type = 'polygon_news'"
    ).fetchone()
    assert aapl is not None
    assert msft is not None
    assert "## AAPL:" in aapl[0]
    assert "## MSFT:" in msft[0]
    conn.close()


def test_article_tickers_count_matches_raw_occurrences(tmp_path: Path):
    """article_tickers rows == total (article, ticker) occurrences in raw_assets."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    ensure_articles_table(conn)

    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date,
           fetched_at, content_raw) VALUES ('raw-1', 'AAPL', 'polygon_news',
           '2025-01-02', datetime('now'), ?)""",
        (b"{}",),
    )
    conn.commit()

    _seed_article(conn, "poly:a1", ticker="AAPL")
    _seed_article(conn, "poly:a2", ticker="AAPL")
    _seed_article(conn, "poly:a1", ticker="MSFT", raw_asset_id="raw-1")
    conn.close()

    conn = sqlite3.connect(str(db))
    at_count = conn.execute("SELECT COUNT(*) FROM article_tickers").fetchone()[0]
    art_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()

    # 3 occurrences, 2 unique articles
    assert at_count == 3
    assert art_count == 2
