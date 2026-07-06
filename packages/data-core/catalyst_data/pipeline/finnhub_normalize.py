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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from catalyst_data.articles import compute_article_id, upsert_article, upsert_article_ticker
from catalyst_data.pipeline.align import map_to_trade_date
from catalyst_data.trading_calendar import trading_days_through

logger = logging.getLogger(__name__)

BATCH_COMMIT_SIZE = 100


def _load_trading_calendar(
    conn: sqlite3.Connection, through_date: str | None = None
) -> list[str]:
    """Extract sorted trading dates from ohlcv union calendar oracle."""
    try:
        days = trading_days_through(conn, through_date)
    except RuntimeError as exc:
        raise RuntimeError(
            "Trading calendar is empty — ohlcv table has no dates. "
            "Cannot re-derive articles without reference_date alignment."
        ) from exc
    return days


def _published_utc_from_finnhub(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value) if value else ""


def _calendar_ceiling_from_published(values: list[str]) -> str | None:
    max_date = None
    for value in values:
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        candidate = (dt.date() + timedelta(days=7)).isoformat()
        max_date = candidate if max_date is None else max(max_date, candidate)
    return max_date


def _max_finnhub_publication_ceiling(raw_rows: list[tuple[str, str, bytes]]) -> str | None:
    published: list[str] = []
    for raw_asset_id, _ticker, compressed in raw_rows:
        try:
            payload = zlib.decompress(compressed)
            data = json.loads(payload)
        except (zlib.error, json.JSONDecodeError) as exc:
            logger.warning("Skipping calendar scan for %s: %s", raw_asset_id, exc)
            continue
        if not isinstance(data, list):
            continue
        for article_data in data:
            if isinstance(article_data, dict):
                published.append(_published_utc_from_finnhub(article_data.get("datetime")))
    return _calendar_ceiling_from_published(published)


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

    # Finnhub datetime is Unix timestamp (int); conversion uses datetime.fromtimestamp.
    published_utc = _published_utc_from_finnhub(result.get("datetime"))

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

    raw_rows = conn.execute(
        "SELECT asset_id, ticker, content_raw FROM raw_assets "
        "WHERE source_type = 'finnhub_company_news'"
    ).fetchall()

    # Load trading calendar once, extending beyond local OHLCV when Bronze does.
    trading_days = _load_trading_calendar(
        conn, through_date=_max_finnhub_publication_ceiling(raw_rows)
    )

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
