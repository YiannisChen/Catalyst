"""Ticker-scoping post-filter via SQLite (Mac-safe — no LanceDB imports).

Problem: the article-level index stores tickers as a JSON array on each chunk
(tickers[]), not as a scalar column.  LanceDB prefiltering on array membership
is environment-dependent.  Until Step 4 validates LanceDB-level list filtering,
we post-filter in SQLite — one batch query per filter operation.

Usage:
    results = hybrid_search(...)          # no ticker prefilter
    filtered = filter_by_ticker(results, "AAPL", db_path)
"""

from __future__ import annotations

import sqlite3
from typing import Any


def filter_by_ticker(
    results: list[dict[str, Any]], ticker: str, db_path: str
) -> list[dict[str, Any]]:
    """Return only results whose article_id is associated with `ticker`.

    Uses a single batch SQL query against article_tickers — not N individual
    queries.  If results is empty, returns empty list immediately (no DB call).
    """
    if not results:
        return []

    article_ids = list({r["article_id"] for r in results if "article_id" in r})
    if not article_ids:
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in article_ids)
        rows = conn.execute(
            f"""SELECT DISTINCT article_id FROM article_tickers
                WHERE ticker = ? AND article_id IN ({placeholders})""",
            [ticker] + article_ids,
        ).fetchall()
        valid_ids: set[str] = {r["article_id"] for r in rows}
    finally:
        conn.close()

    return [r for r in results if r.get("article_id") in valid_ids]


def filter_by_tickers(
    results: list[dict[str, Any]], tickers: list[str], db_path: str
) -> list[dict[str, Any]]:
    """Return results associated with ANY of the given tickers (union).

    Single batch query — one SQL call regardless of ticker count.
    """
    if not results or not tickers:
        return []

    article_ids = list({r["article_id"] for r in results if "article_id" in r})
    if not article_ids:
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        a_placeholders = ",".join("?" for _ in article_ids)
        t_placeholders = ",".join("?" for _ in tickers)
        rows = conn.execute(
            f"""SELECT DISTINCT article_id FROM article_tickers
                WHERE ticker IN ({t_placeholders})
                  AND article_id IN ({a_placeholders})""",
            tickers + article_ids,
        ).fetchall()
        valid_ids: set[str] = {r["article_id"] for r in rows}
    finally:
        conn.close()

    return [r for r in results if r.get("article_id") in valid_ids]
