"""Offline re-derive: finnhub_company_news raw_assets → articles table.

Reads existing raw_assets, decompresses zlib-encoded JSON, extracts every
article from the top-level JSON array, maps all available provenance fields,
aligns reference_date via map_to_trade_date, and upserts into articles.

Designed for idempotent, resumable execution — safe to re-run.
Mirrors rederive_polygon_news column set exactly to avoid schema drift.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_data.articles import compute_article_id, upsert_article, upsert_article_ticker
from catalyst_data.pipeline.align import map_to_trade_date

logger = logging.getLogger(__name__)

BATCH_COMMIT_SIZE = 100


def _load_trading_calendar(conn: sqlite3.Connection) -> list[str]:
    """Extract sorted distinct trading dates from ohlcv table."""
    rows = conn.execute(
        "SELECT DISTINCT date FROM ohlcv ORDER BY date"
    ).fetchall()
    days = [r[0] for r in rows]
    if not days:
        raise RuntimeError(
            "Trading calendar is empty — ohlcv table has no dates. "
            "Cannot re-derive articles without reference_date alignment."
        )
    return days


def _parse_finnhub_article(
    result: dict[str, Any],
    raw_asset_id: str,
    ticker: str,
    trading_days: list[str],
) -> dict[str, Any] | None:
    """Map a single Finnhub company-news article to an articles row dict.

    Mirror _parse_article() in rederive.py column set EXACTLY.
    """
    native_id = result.get("id")
    if not native_id:
        logger.warning("Skipping article with no id in raw_asset %s", raw_asset_id)
        return None

    # Finnhub datetime is Unix timestamp (int)
    published_utc = result.get("datetime")
    if isinstance(published_utc, (int, float)):
        published_utc = datetime.fromtimestamp(published_utc, tz=timezone.utc).isoformat()
    else:
        published_utc = str(published_utc) if published_utc else ""

    description = result.get("summary", "") or ""

    # Align reference date using existing trading calendar
    reference_date = (
        map_to_trade_date(published_utc, trading_days) if published_utc else ""
    )

    return {
        "article_id": compute_article_id("finnhub", str(native_id)),
        "raw_asset_id": raw_asset_id,
        "provider": "finnhub",
        "source_type": "finnhub_company_news",
        "ticker": ticker,
        "reference_date": reference_date or "",
        "published_utc": published_utc,
        "title": result.get("headline", "Untitled"),
        "description": description,
        "article_url": result.get("url"),
        "image_url": result.get("image"),
        "author": None,
        "publisher_name": result.get("source"),
        "publisher_homepage_url": None,
        "publisher_logo_url": None,
        "publisher_favicon_url": None,
        "keywords_json": "[]",
        "insights_json": "[]",
        "tickers_json": json.dumps([ticker], ensure_ascii=False),
        "source_tier": None,
        "dedup_group_id": None,
        "is_canonical": 1,
        "is_rag_eligible": 1,
        "quality_score": 1.0,
    }


def rederive_finnhub_news(db_path: str | Path) -> dict[str, int]:
    """Re-derive all finnhub_company_news articles from raw_assets.

    Args:
        db_path: Path to the SQLite database containing raw_assets.

    Returns:
        Dict with counts: raw_rows_processed, articles_upserted, articles_skipped.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Load trading calendar once
    trading_days = _load_trading_calendar(conn)

    raw_rows = conn.execute(
        "SELECT asset_id, ticker, content_raw FROM raw_assets "
        "WHERE source_type = 'finnhub_company_news'"
    ).fetchall()

    articles_upserted = 0
    articles_skipped = 0

    for batch_start in range(0, len(raw_rows), BATCH_COMMIT_SIZE):
        batch = raw_rows[batch_start : batch_start + BATCH_COMMIT_SIZE]

        for raw_asset_id, ticker, compressed in batch:
            try:
                payload = zlib.decompress(compressed)
            except zlib.error as exc:
                logger.warning("Failed to decompress %s: %s", raw_asset_id, exc)
                continue

            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                logger.warning("Invalid JSON in %s: %s", raw_asset_id, exc)
                continue

            # Finnhub response is a flat JSON array (not nested like Polygon)
            if not isinstance(data, list):
                continue

            for article_data in data:
                if not isinstance(article_data, dict):
                    continue
                row = _parse_finnhub_article(
                    article_data, raw_asset_id, ticker, trading_days
                )
                if row is None:
                    articles_skipped += 1
                    continue
                upsert_article(conn, article=row)
                upsert_article_ticker(
                    conn,
                    article_id=row["article_id"],
                    ticker=ticker,
                    raw_asset_id=raw_asset_id,
                    reference_date=row["reference_date"],
                )
                articles_upserted += 1

        conn.commit()
        logger.info(
            "Finnhub batch %d/%d: %d articles upserted so far",
            batch_start // BATCH_COMMIT_SIZE + 1,
            (len(raw_rows) + BATCH_COMMIT_SIZE - 1) // BATCH_COMMIT_SIZE,
            articles_upserted,
        )

    conn.close()

    logger.info(
        "Finnhub re-derive complete: %d raw rows → %d articles upserted, %d skipped",
        len(raw_rows),
        articles_upserted,
        articles_skipped,
    )

    return {
        "raw_rows_processed": len(raw_rows),
        "articles_upserted": articles_upserted,
        "articles_skipped": articles_skipped,
        "article_tickers_upserted": articles_upserted,
    }
