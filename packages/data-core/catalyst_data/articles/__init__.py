"""Articles table: per-article canonical storage layer.

Decouples article-level data from raw_assets (which stores whole API responses)
and clean_assets (which stores the Silver digest). Enables per-article retrieval,
quality scoring, and provenance tracking.

IMPORTANT: articles.ticker and articles.raw_asset_id are scalar (one value per
canonical article) and are NOT sufficient for per-ticker retrieval — use
article_tickers for the full many-to-many (article, ticker) association.
The scalar columns exist for backward compatibility and last-write provenance
only; they should not be used for downstream retrieval scoping.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_ARTICLES_DDL = """
CREATE TABLE IF NOT EXISTS articles (
    article_id        TEXT PRIMARY KEY,
    raw_asset_id      TEXT NOT NULL,
    provider          TEXT NOT NULL DEFAULT 'polygon',
    source_type       TEXT NOT NULL DEFAULT 'polygon_news',
    ticker            TEXT NOT NULL,
    reference_date    TEXT NOT NULL,
    published_utc     TEXT NOT NULL,
    title             TEXT NOT NULL,
    description       TEXT,
    article_url       TEXT,
    image_url         TEXT,
    author            TEXT,
    publisher_name    TEXT,
    publisher_homepage_url TEXT,
    publisher_logo_url    TEXT,
    publisher_favicon_url TEXT,
    keywords_json     TEXT,
    insights_json     TEXT,
    tickers_json      TEXT,
    source_tier       INTEGER,
    dedup_group_id    TEXT,
    is_canonical      INTEGER DEFAULT 1,
    is_rag_eligible   INTEGER DEFAULT 1,
    quality_score     REAL DEFAULT 1.0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (raw_asset_id) REFERENCES raw_assets(asset_id)
);
CREATE INDEX IF NOT EXISTS idx_articles_raw_asset ON articles(raw_asset_id);
CREATE INDEX IF NOT EXISTS idx_articles_ticker_date ON articles(ticker, reference_date);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_utc);
CREATE INDEX IF NOT EXISTS idx_articles_ticker_published ON articles(ticker, published_utc);
CREATE INDEX IF NOT EXISTS idx_articles_provider ON articles(provider);
"""

_ARTICLE_TICKERS_DDL = """
CREATE TABLE IF NOT EXISTS article_tickers (
    article_id    TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    raw_asset_id  TEXT NOT NULL,
    reference_date TEXT NOT NULL,
    dedup_group_id TEXT,
    is_canonical INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (article_id, ticker),
    FOREIGN KEY (article_id) REFERENCES articles(article_id),
    FOREIGN KEY (raw_asset_id) REFERENCES raw_assets(asset_id)
);
CREATE INDEX IF NOT EXISTS idx_article_tickers_ticker_date
    ON article_tickers(ticker, reference_date);
CREATE INDEX IF NOT EXISTS idx_article_tickers_raw_asset
    ON article_tickers(raw_asset_id);
"""


def ensure_articles_table(conn: sqlite3.Connection) -> None:
    """Create the articles and article_tickers tables and indexes."""
    conn.executescript(_ARTICLES_DDL)
    conn.executescript(_ARTICLE_TICKERS_DDL)
    conn.commit()


def compute_article_id(provider: str, native_id: str) -> str:
    """Namespace a provider-native article id for global uniqueness."""
    return f"{provider}:{native_id}"


def upsert_article(conn: sqlite3.Connection, *, article: dict[str, Any]) -> None:
    """Insert or replace a single canonical article row.

    NOTE: articles.ticker and articles.raw_asset_id are set from the FIRST
    occurrence seen (INSERT) and overwritten on REPLACE. These scalar columns
    exist for backward compatibility only. Use article_tickers for the full
    many-to-many (article, ticker) association.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO articles
            (article_id, raw_asset_id, provider, source_type, ticker,
             reference_date, published_utc, title, description, article_url,
             image_url, author, publisher_name, publisher_homepage_url,
             publisher_logo_url, publisher_favicon_url, keywords_json,
             insights_json, tickers_json, source_tier, dedup_group_id,
             is_canonical, is_rag_eligible, quality_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article["article_id"],
            article["raw_asset_id"],
            article.get("provider", "polygon"),
            article.get("source_type", "polygon_news"),
            article["ticker"],
            article["reference_date"],
            article["published_utc"],
            article["title"],
            article.get("description"),
            article.get("article_url"),
            article.get("image_url"),
            article.get("author"),
            article.get("publisher_name"),
            article.get("publisher_homepage_url"),
            article.get("publisher_logo_url"),
            article.get("publisher_favicon_url"),
            article.get("keywords_json"),
            article.get("insights_json"),
            article.get("tickers_json"),
            article.get("source_tier"),
            article.get("dedup_group_id"),
            article.get("is_canonical", 1),
            article.get("is_rag_eligible", 1),
            article.get("quality_score", 1.0),
        ),
    )
    conn.commit()


def upsert_article_ticker(
    conn: sqlite3.Connection,
    *,
    article_id: str,
    ticker: str,
    raw_asset_id: str,
    reference_date: str,
) -> None:
    """Insert or replace one (article, ticker) association.

    One row per occurrence — never collapsed. A single article returned
    for 3 tickers produces 3 article_tickers rows.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO article_tickers
            (article_id, ticker, raw_asset_id, reference_date)
        VALUES (?, ?, ?, ?)
        """,
        (article_id, ticker, raw_asset_id, reference_date),
    )
    conn.commit()


def get_articles_by_raw_asset(
    conn: sqlite3.Connection, raw_asset_id: str
) -> list[dict[str, Any]]:
    """Fetch all articles for a given raw_asset."""
    rows = conn.execute(
        "SELECT * FROM articles WHERE raw_asset_id = ?", (raw_asset_id,)
    ).fetchall()
    cols = [
        desc[0] for desc in conn.execute("SELECT * FROM articles LIMIT 0").description
    ]
    return [dict(zip(cols, row)) for row in rows]
