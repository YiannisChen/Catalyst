"""Offline re-derive: polygon_news raw_assets → articles table.

Reads existing raw_assets, decompresses zlib-encoded JSON, extracts every
article from news.results[], maps all available provenance fields, aligns
reference_date via map_to_trade_date, and upserts into articles.

Designed for idempotent, resumable execution — safe to re-run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import zlib
from datetime import datetime, timedelta
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
    """Extract sorted trading dates from ohlcv union calendar oracle.

    Raises RuntimeError if empty — no silent misalignment fallback.
    """
    try:
        days = trading_days_through(conn, through_date)
    except RuntimeError as exc:
        raise RuntimeError(
            "Trading calendar is empty — ohlcv table has no dates. "
            "The dev DB must be a copy of the frozen DB to include the calendar. "
            "Cannot re-derive articles without reference_date alignment."
        ) from exc
    return days


def _calendar_ceiling_from_published(values: list[str]) -> str | None:
    """Return a safe calendar ceiling for publication timestamps."""
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


def _max_polygon_publication_ceiling(raw_rows: list[tuple[str, str, bytes]]) -> str | None:
    published: list[str] = []
    for raw_asset_id, _ticker, compressed in raw_rows:
        try:
            payload = zlib.decompress(compressed)
            data = json.loads(payload)
        except (zlib.error, json.JSONDecodeError) as exc:
            logger.warning("Skipping calendar scan for %s: %s", raw_asset_id, exc)
            continue
        results = (data.get("news") or {}).get("results", [])
        if not isinstance(results, list):
            continue
        for article_data in results:
            if isinstance(article_data, dict):
                published.append(article_data.get("published_utc", "") or "")
    return _calendar_ceiling_from_published(published)


def _parse_article(
    result: dict[str, Any],
    raw_asset_id: str,
    ticker: str,
    trading_days: list[str],
) -> dict[str, Any] | None:
    """Map a single Polygon news.results[] entry to an articles row dict."""
    native_id = result.get("id")
    if not native_id:
        logger.warning("Skipping article with no id in raw_asset %s", raw_asset_id)
        return None

    published_utc = result.get("published_utc", "")
    description = result.get("description", "") or ""
    publisher = result.get("publisher") or {}

    # Align reference date using existing trading calendar
    reference_date = (
        map_to_trade_date(published_utc, trading_days) if published_utc else ""
    )

    return {
        "article_id": compute_article_id("poly", native_id),
        "raw_asset_id": raw_asset_id,
        "provider": "polygon",
        "source_type": "polygon_news",
        "ticker": ticker,
        "reference_date": reference_date or "",
        "published_utc": published_utc,
        "title": result.get("title", "Untitled"),
        "description": description,
        "article_url": result.get("article_url"),
        "image_url": result.get("image_url"),
        "author": result.get("author"),
        "publisher_name": publisher.get("name"),
        "publisher_homepage_url": publisher.get("homepage_url"),
        "publisher_logo_url": publisher.get("logo_url"),
        "publisher_favicon_url": publisher.get("favicon_url"),
        "keywords_json": json.dumps(result.get("keywords") or [], ensure_ascii=False),
        "insights_json": json.dumps(result.get("insights") or [], ensure_ascii=False),
        "tickers_json": json.dumps(result.get("tickers") or [], ensure_ascii=False),
        "source_tier": None,
        "dedup_group_id": None,
        "is_canonical": 1,
        "is_rag_eligible": 1,
        "quality_score": 1.0,
    }


def rederive_polygon_news(db_path: str | Path) -> dict[str, int]:
    """Re-derive all polygon_news articles from raw_assets.

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
        "SELECT asset_id, ticker, content_raw FROM raw_assets WHERE source_type = 'polygon_news'"
    ).fetchall()

    # Load trading calendar once, extending beyond local OHLCV when Bronze does.
    trading_days = _load_trading_calendar(
        conn, through_date=_max_polygon_publication_ceiling(raw_rows)
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

            results = (data.get("news") or {}).get("results", [])
            if not isinstance(results, list):
                continue

            for article_data in results:
                if not isinstance(article_data, dict):
                    continue
                row = _parse_article(article_data, raw_asset_id, ticker, trading_days)
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
            "Batch %d/%d: %d articles upserted so far",
            batch_start // BATCH_COMMIT_SIZE + 1,
            (len(raw_rows) + BATCH_COMMIT_SIZE - 1) // BATCH_COMMIT_SIZE,
            articles_upserted,
        )

    conn.close()

    logger.info(
        "Re-derive complete: %d raw rows → %d articles upserted, %d skipped",
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
