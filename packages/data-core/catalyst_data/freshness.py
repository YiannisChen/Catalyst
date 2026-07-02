"""Read-only freshness reporting — news staleness + index staleness.

Measures timeliness of ingested data vs the local OHLCV calendar (NOT real
market time).  All functions are pure reads — no side effects on the DB.

Definitions:
  latest_local_ohlcv_date  = MAX(date) FROM ohlcv (not real market date).
  news STALE   = latest article_tickers.reference_date < latest_local_ohlcv_date.
  news FRESH   = latest article_tickers.reference_date == latest_local_ohlcv_date.
  news AHEAD   = latest article_tickers.reference_date > latest_local_ohlcv_date
                 (should not happen; indicates data beyond local calendar).
  news NO_DATA = no articles/tickers for this (source, ticker).

  index FRESH    = every live-indexed article has a matching content_hash row in
                   index_state with the latest live index_manifests build_id.
  index STALE    = a live build exists but some articles are missing or stale.
  index NO_INDEX = no index_manifests row with status='live' exists.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def latest_local_ohlcv_date(conn: sqlite3.Connection) -> str:
    """Return MAX(date) FROM ohlcv as ISO string.

    Raises RuntimeError if ohlcv is empty — freshness cannot be measured
    without a trading calendar.
    """
    row = conn.execute("SELECT MAX(date) FROM ohlcv").fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(
            "ohlcv table is empty — cannot compute freshness.  "
            "Ingest OHLCV data before measuring staleness."
        )
    return row[0]


def _trading_day_diff(later: str, earlier: str) -> int:
    """Return ISO date difference (later - earlier) in days."""
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


# ---------------------------------------------------------------------------
# News freshness
# ---------------------------------------------------------------------------

def news_freshness(
    conn: sqlite3.Connection,
    *,
    watermark: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-(source_type, ticker) news staleness.

    Returns dict keyed by source_type, then by ticker:
        {source_type: {ticker: {latest_date, status, days_behind}}}

    Status ∈ {FRESH, STALE, AHEAD, NO_DATA}.

    If *watermark* is None, uses latest_local_ohlcv_date(conn).
    """
    wm = watermark or latest_local_ohlcv_date(conn)

    rows = conn.execute("""
        SELECT a.source_type, at.ticker, MAX(at.reference_date) AS latest_date
        FROM article_tickers at
        JOIN articles a ON a.article_id = at.article_id
        GROUP BY a.source_type, at.ticker
        ORDER BY a.source_type, at.ticker
    """).fetchall()

    result: dict[str, dict[str, Any]] = {}
    for source_type, ticker, latest_date in rows:
        result.setdefault(source_type, {})[ticker] = {
            "latest_date": latest_date,
            "status": (
                "FRESH" if latest_date == wm
                else "STALE" if latest_date < wm
                else "AHEAD"
            ),
            "days_behind": (
                _trading_day_diff(wm, latest_date) if latest_date < wm else 0
            ),
        }

    # Tick that show up in ohlcv but have zero articles get NO_DATA.
    known = {(stype, ticker) for stype, tmap in result.items() for ticker in tmap}
    ticker_rows = conn.execute(
        "SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol"
    ).fetchall()

    # Resolve source_types for NO_DATA assignment.
    # If we have articles with known source_types, use those.
    # Otherwise, fall back to articles table; if empty, default to ['polygon_news'].
    source_types = list(result.keys())
    if not source_types:
        db_stypes = conn.execute(
            "SELECT DISTINCT source_type FROM articles ORDER BY source_type"
        ).fetchall()
        source_types = [r[0] for r in db_stypes]
    if not source_types:
        source_types = ["polygon_news"]
    for (symbol,) in ticker_rows:
        for st in source_types:
            if (st, symbol) not in known:
                result.setdefault(st, {})[symbol] = {
                    "latest_date": None,
                    "status": "NO_DATA",
                    "days_behind": -1,
                }

    return result


# ---------------------------------------------------------------------------
# Index freshness
# ---------------------------------------------------------------------------

def index_freshness(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return index staleness measured against the latest live manifest.

    Only index_manifests rows with status='live' count as a real build.
    Dry-run manifests are ignored — the index cannot report FRESH from dry-run.

    Returns:
        { status: "FRESH"|"STALE"|"NO_INDEX",
          stale_count: int,
          total_articles: int,
          latest_build_id: str|None,
          last_built_at: str|None,
          model: str|None }
    """
    # Find the latest live build
    live = conn.execute("""
        SELECT build_id, created_at, model, indexed_through_date
        FROM index_manifests
        WHERE status = 'live'
        ORDER BY created_at DESC
        LIMIT 1
    """).fetchone()

    total_articles = conn.execute(
        "SELECT COUNT(*) FROM articles"
    ).fetchone()[0]

    if live is None:
        # Check for filings (Step 3+)
        try:
            total_filings = conn.execute(
                "SELECT COUNT(*) FROM filings"
            ).fetchone()
            if total_filings:
                total_articles += total_filings[0]
        except sqlite3.OperationalError:
            pass  # filings table doesn't exist yet (Step 3)

        return {
            "status": "NO_INDEX",
            "stale_count": total_articles,
            "total_articles": total_articles,
            "latest_build_id": None,
            "last_built_at": None,
            "model": None,
        }

    build_id, created_at, model, _indexed_through = live

    # Count stale: articles not in index_state for this build, or with
    # a mismatched content_hash.
    from catalyst_data.index_builder import compute_content_hash

    stale_count = 0

    # articles without an index_state row at all
    missing_rows = conn.execute("""
        SELECT a.article_id, a.title, a.description
        FROM articles a
        LEFT JOIN index_state s
            ON s.corpus_item_id = a.article_id AND s.source_kind = 'article'
               AND s.indexed_build_id = ?
        WHERE s.corpus_item_id IS NULL
    """, (build_id,)).fetchall()
    stale_count += len(missing_rows)

    # articles whose content_hash doesn't match the latest live build
    indexed_rows = conn.execute("""
        SELECT s.corpus_item_id, s.content_hash,
               a.title, a.description
        FROM index_state s
        JOIN articles a ON a.article_id = s.corpus_item_id
        WHERE s.source_kind = 'article'
          AND s.indexed_build_id = ?
    """, (build_id,)).fetchall()

    for (_, stored_hash, title, desc) in indexed_rows:
        current_hash = compute_content_hash(title, desc)
        if current_hash != stored_hash:
            stale_count += 1

    is_fresh = stale_count == 0

    return {
        "status": "FRESH" if is_fresh else "STALE",
        "stale_count": stale_count,
        "total_articles": total_articles,
        "latest_build_id": build_id,
        "last_built_at": created_at,
        "model": model,
    }


# ---------------------------------------------------------------------------
# Combined report
# ---------------------------------------------------------------------------

def filings_freshness(
    conn: sqlite3.Connection,
    *,
    watermark: str | None = None,
) -> dict[str, Any]:
    """Return per-ticker SEC filings freshness.

    SEC filings are sparse by nature — no STALE/FRESH binary.
    Reports latest_filing_date, latest_checked_date, filings_30d_count.
    """
    wm = watermark or latest_local_ohlcv_date(conn)
    wm_date = date.fromisoformat(wm)

    # Check if filings table exists
    try:
        conn.execute("SELECT 1 FROM filings LIMIT 0")
    except sqlite3.OperationalError:
        return {"per_ticker": {}, "overall": {"total_filings": 0, "checked_tickers": 0, "never_checked_tickers": 0}}

    # Universe tickers from ohlcv
    ticker_rows = conn.execute(
        "SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol"
    ).fetchall()
    all_tickers = [r[0] for r in ticker_rows]

    # Latest filing per ticker
    filing_rows = conn.execute("""
        SELECT ticker, MAX(filed_at), COUNT(*)
        FROM filings
        WHERE filed_at >= date(?, '-30 days')
        GROUP BY ticker
        ORDER BY ticker
    """, (wm,)).fetchall()
    filing_map: dict[str, dict] = {}
    for ticker, latest_fd, count_30d in filing_rows:
        filing_map[ticker] = {
            "latest_filing_date": latest_fd,
            "filings_30d_count": count_30d,
        }

    # Latest checked date per ticker from source_checkpoints
    checked_map: dict[str, str] = {}
    try:
        conn.execute("SELECT 1 FROM source_checkpoints LIMIT 0")
    except sqlite3.OperationalError:
        pass
    else:
        checkpoint_rows = conn.execute("""
            SELECT ticker, MAX(date)
            FROM source_checkpoints
            WHERE source_type = 'sec_filings' AND status = 'success'
            GROUP BY ticker
            ORDER BY ticker
        """).fetchall()
        checked_map = {r[0]: r[1] for r in checkpoint_rows}

    # Build per-ticker report
    per_ticker: dict[str, dict] = {}
    for ticker in all_tickers:
        entry = filing_map.get(ticker, {})
        latest_filing_date = entry.get("latest_filing_date")
        filings_30d_count = entry.get("filings_30d_count", 0)
        latest_checked_date = checked_map.get(ticker)

        if latest_checked_date is None:
            status = "never_checked"
        elif latest_checked_date == wm:
            status = "current"
        else:
            checked_dt = date.fromisoformat(latest_checked_date)
            status = "stale_check" if checked_dt < wm_date else "current"

        per_ticker[ticker] = {
            "latest_filing_date": latest_filing_date,
            "latest_checked_date": latest_checked_date,
            "filings_30d_count": filings_30d_count,
            "status": status,
        }

    return {
        "per_ticker": per_ticker,
        "overall": {
            "total_filings": sum(
                t["filings_30d_count"] for t in per_ticker.values()
            ),
            "checked_tickers": len(checked_map),
            "never_checked_tickers": len(all_tickers) - len(checked_map),
        },
    }


def freshness_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return combined news + index + SEC filings freshness report (read-only)."""
    return {
        "local_ohlcv_date": latest_local_ohlcv_date(conn),
        "news": news_freshness(conn),
        "index": index_freshness(conn),
        "sec_filings": filings_freshness(conn),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
