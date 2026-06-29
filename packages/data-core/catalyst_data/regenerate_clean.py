"""Historical regeneration: rebuild clean_assets for polygon_news from article_tickers.

Iterates the article_tickers junction table so every (article, ticker) occurrence
gets its own clean_asset row. This restores full per-ticker evidence coverage
that the old per-ticker raw_assets+digests provided.

Operates on polygon_news scope only; non-news rows are untouched. Idempotent.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from catalyst_data.pipeline.transform_v2 import _transform_article

logger = logging.getLogger(__name__)

BATCH_COMMIT_SIZE = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def regenerate_polygon_clean_assets(db_path: str | Path) -> dict[str, int]:
    """Rebuild clean_assets for polygon_news from article_tickers.

    1. DELETE all clean_assets rows where source_type = 'polygon_news'
    2. For each (article_id, ticker) in article_tickers:
       - Build single-article Markdown via _transform_article
       - INSERT one clean_asset row with:
           asset_id = poly:{native_id}:{TICKER}
           raw_asset_id = article_tickers.raw_asset_id
           ticker = article_tickers.ticker
    3. Non-news rows (fundamentals, macro, ohlcv) are never touched

    Uses PRAGMA foreign_keys=OFF temporarily so the legacy FK on asset_id
    (which expects raw_assets-compatible ids) doesn't block new format values.

    Returns {deleted_digest_rows, inserted_article_rows}.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    # Disable FK enforcement temporarily
    conn.execute("PRAGMA foreign_keys=OFF")

    # Count and delete existing polygon_news clean_assets
    deleted = conn.execute(
        "SELECT COUNT(*) FROM clean_assets WHERE source_type = 'polygon_news'"
    ).fetchone()[0]

    conn.execute("DELETE FROM clean_assets WHERE source_type = 'polygon_news'")
    conn.commit()
    logger.info("Deleted %d existing polygon_news clean_assets rows", deleted)

    # Fetch all (article, ticker) associations with article data
    assoc_rows = conn.execute(
        """SELECT at.article_id, at.ticker, at.raw_asset_id, at.reference_date,
                  a.title, a.description, a.article_url, a.published_utc,
                  a.publisher_name
           FROM article_tickers at
           JOIN articles a ON a.article_id = at.article_id
           WHERE a.provider = 'polygon'
           ORDER BY at.article_id, at.ticker"""
    ).fetchall()

    inserted = 0
    now = _now_iso()

    for batch_start in range(0, len(assoc_rows), BATCH_COMMIT_SIZE):
        batch = assoc_rows[batch_start : batch_start + BATCH_COMMIT_SIZE]

        for row in batch:
            (
                article_id, ticker, raw_asset_id, reference_date,
                title, description, article_url, published_utc,
                publisher_name,
            ) = row

            # Extract native_id from article_id (strip "poly:" prefix)
            native_id = article_id.replace("poly:", "")
            clean_asset_id = f"poly:{native_id}:{ticker}"

            # Reconstruct article dict for _transform_article
            article_dict = {
                "id": native_id,
                "title": title,
                "published_utc": published_utc,
                "description": description or "",
                "article_url": article_url,
                "publisher": {"name": publisher_name} if publisher_name else {},
            }

            try:
                transformed = _transform_article(article_dict, ticker)
            except Exception as exc:
                logger.warning(
                    "Failed to transform article %s for ticker %s: %s",
                    article_id, ticker, exc,
                )
                continue

            conn.execute(
                """INSERT OR REPLACE INTO clean_assets
                   (asset_id, ticker, source_type, reference_date, cleaned_at,
                    content_md, title_hash, is_duplicate, raw_asset_id)
                   VALUES (?, ?, 'polygon_news', ?, ?, ?, ?, 0, ?)""",
                (
                    clean_asset_id,
                    ticker,
                    reference_date,
                    now,
                    transformed["content_md"],
                    transformed["title_hash"],
                    raw_asset_id,
                ),
            )
            inserted += 1

        conn.commit()
        logger.info(
            "Batch %d/%d: %d clean_assets inserted so far",
            batch_start // BATCH_COMMIT_SIZE + 1,
            (len(assoc_rows) + BATCH_COMMIT_SIZE - 1) // BATCH_COMMIT_SIZE,
            inserted,
        )

    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()

    logger.info(
        "Regeneration complete: deleted %d digest rows, inserted %d article rows",
        deleted,
        inserted,
    )

    return {
        "deleted_digest_rows": deleted,
        "inserted_article_rows": inserted,
    }
