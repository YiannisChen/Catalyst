"""Update/backfill pipeline engine — Polygon-first, checkpoint/resume.

Reads the universe → computes freshness/missing windows → fetches (honoring
rate policies) → archives → normalizes → tier-classifies → regenerates
clean_assets → runs incremental indexer dry-run → emits report.

Dry-run (dry_run=True): computes missing cells only.  ZERO network, ZERO
DB writes.  Real path uses an injected fetch_fn (async callable).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

from catalyst_data.freshness import latest_local_ohlcv_date, freshness_report
from catalyst_data.quality import (
    open_ingestion_run,
    close_ingestion_run,
    write_source_checkpoint,
    close_stale_runs,
)
from catalyst_data.storage.sqlite import init_db
from catalyst_data.quality import ensure_ingestion_quality_tables

logger = logging.getLogger(__name__)

# Type for async fetch functions
FetchFn = Callable[..., Awaitable[Any]]


# ---------------------------------------------------------------------------
# Missing-cell computation
# ---------------------------------------------------------------------------

def _trading_days_in_window(
    conn: sqlite3.Connection, from_date: str, to_date: str
) -> list[str]:
    """Return all ohlcv dates in [from_date, to_date] sorted ascending."""
    rows = conn.execute(
        """SELECT DISTINCT date FROM ohlcv
           WHERE date >= ? AND date <= ?
           ORDER BY date""",
        (from_date, to_date),
    ).fetchall()
    return [r[0] for r in rows]


def compute_missing_cells(
    conn: sqlite3.Connection,
    *,
    tickers: list[str] | None = None,
    sources: list[str] | None = None,
    from_date: str,
    to_date: str,
) -> list[tuple[str, str, str]]:
    """Return (ticker, date, source_type) cells without a success checkpoint.

    Only trading days from ohlcv are considered.  Failed cells (status='failed')
    are INCLUDED as missing so they get retried.

    If tickers is None, uses all distinct ohlcv symbols as the universe.
    If sources is None, uses ['polygon_news'].
    """
    if sources is None:
        sources = ["polygon_news"]

    # Resolve tickers
    if tickers is None:
        ticker_rows = conn.execute(
            "SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol"
        ).fetchall()
        tickers = [r[0] for r in ticker_rows]
    if not tickers:
        return []

    trading_days = _trading_days_in_window(conn, from_date, to_date)
    if not trading_days:
        return []

    # Build the set of (ticker, date, source) cells that have a success
    # checkpoint (any run_id)
    success_rows = conn.execute(
        f"""SELECT ticker, date, source_type FROM source_checkpoints
            WHERE status = 'success'
              AND ticker IN ({','.join('?' for _ in tickers)})
              AND source_type IN ({','.join('?' for _ in sources)})
              AND date >= ? AND date <= ?""",
        tickers + sources + [from_date, to_date],
    ).fetchall()
    success_cells: set[tuple[str, str, str]] = {
        (r[0], r[1], r[2]) for r in success_rows
    }

    missing: list[tuple[str, str, str]] = []
    for ticker in tickers:
        for td in trading_days:
            for src in sources:
                cell = (ticker, td, src)
                if cell not in success_cells:
                    missing.append(cell)
    return missing


# ---------------------------------------------------------------------------
# Single-cell fetch wrapper
# ---------------------------------------------------------------------------

async def _fetch_cell(
    db_path: str,
    ticker: str,
    date: str,
    source: str,
    run_id: str,
    fetch_fn: FetchFn,
    limiter: Any | None = None,
    retry_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch one (ticker, date, source) cell through the orchestrator.

    Respects limiter (TokenBucketLimiter) and retry.
    Writes a source_checkpoint immediately after Bronze+Silver storage
    (success or failure).

    Returns {ticker, date, source, status, error, articles_count}.
    """
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)

    retries = 0
    last_error: str | None = None
    error_class: str | None = None

    max_retries = (retry_config or {}).get("max_retries", 1)

    for attempt in range(max_retries + 1):
        try:
            if limiter is not None:
                await limiter.acquire()

            # Call orchestrator.process_request for the single source
            from catalyst_data.orchestrator import process_request

            results = await process_request(
                ticker=ticker,
                date=date,
                sources=[source],
                db_path=db_path,
                fetch_fn=fetch_fn,
                limiter=limiter,
            )

            # Inspect the result
            result = results[0] if results else {"ok": False, "error": "no_result"}
            ok = result.get("ok", False)
            error = result.get("error")
            articles_count = len(
                conn.execute(
                    "SELECT COUNT(*) FROM articles WHERE source_type = ? "
                    "AND ticker = ? AND reference_date = ?",
                    (source, ticker, date),
                ).fetchall()
            )

            if ok:
                write_source_checkpoint(
                    conn,
                    run_id=run_id,
                    source_type=source,
                    ticker=ticker,
                    date=date,
                    status="success",
                    retries=retries,
                )
                return {
                    "ticker": ticker,
                    "date": date,
                    "source": source,
                    "status": "success",
                    "error": None,
                    "articles_count": articles_count,
                    "retries": retries,
                }
            else:
                # Some sources return ok=False without an exception
                last_error = str(error) if error else "unknown"
                error_class = type(error).__name__ if error else "UnknownError"
                retries = attempt

        except Exception as exc:
            last_error = str(exc)
            error_class = type(exc).__name__
            retries = attempt

        # Wait before retry
        if attempt < max_retries:
            wait_s = (retry_config or {}).get("base_delay", 5.0) * (2 ** attempt)
            await asyncio.sleep(wait_s)

    # All attempts exhausted — record failure
    write_source_checkpoint(
        conn,
        run_id=run_id,
        source_type=source,
        ticker=ticker,
        date=date,
        status="failed",
        error_class=error_class,
        retries=retries,
    )
    conn.close()
    return {
        "ticker": ticker,
        "date": date,
        "source": source,
        "status": "failed",
        "error": last_error,
        "articles_count": 0,
        "retries": retries,
    }


# ---------------------------------------------------------------------------
# Batch update runner
# ---------------------------------------------------------------------------

async def run_update_batch(
    db_path: str | Path,
    *,
    tickers: list[str] | None = None,
    sources: list[str] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    fetch_fn: FetchFn | None = None,
    limiter: Any | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one batch of the update pipeline.

    Parameters
    ----------
    dry_run : bool
        If True, compute missing cells only — ZERO network, ZERO DB writes.
    limit : int | None
        Cap the number of cells processed (for testing / rate-limit safety).

    Returns a report dict.
    """
    db_path = str(db_path)
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)

    if sources is None:
        sources = ["polygon_news"]

    # Resolve date window
    if from_date is None or to_date is None:
        wm = latest_local_ohlcv_date(conn)
        from_date = from_date or wm
        to_date = to_date or wm

    # Compute missing cells
    missing = compute_missing_cells(
        conn, tickers=tickers, sources=sources,
        from_date=from_date, to_date=to_date,
    )

    if limit is not None:
        missing = missing[:limit]

    # Freshness before
    freshness_before = freshness_report(conn)
    conn.close()

    if dry_run:
        return {
            "run_id": None,
            "mode": "dry-run",
            "cells_total": len(missing),
            "cells_success": 0,
            "cells_failed": 0,
            "cells_skipped": 0,
            "missing_cells": missing,
            "articles_upserted": 0,
            "clean_assets_inserted": 0,
            "index_delta_new": 0,
            "index_delta_changed": 0,
            "index_would_embed": 0,
            "elapsed_sec": 0.0,
            "freshness_before": freshness_before,
            "freshness_after": freshness_before,
            "per_cell_report": [],
        }

    # Real run — requires fetch_fn
    if fetch_fn is None:
        raise ValueError("fetch_fn is required for non-dry-run execution")

    # Open ingestion run
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    resolved_tickers = tickers or [
        r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol"
        ).fetchall()
    ]
    run_id = open_ingestion_run(
        conn, tickers=resolved_tickers, sources=sources, mode="update",
    )
    conn.close()

    # Close stale runs
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    close_stale_runs(conn)
    conn.close()

    start_time = time.monotonic()
    per_cell: list[dict[str, Any]] = []
    cells_success = 0
    cells_failed = 0

    # Process each missing cell sequentially
    for ticker, td, src in missing:
        result = await _fetch_cell(
            db_path, ticker, td, src, run_id, fetch_fn,
            limiter=limiter,
        )
        if result["status"] == "success":
            cells_success += 1
        else:
            cells_failed += 1
        per_cell.append(result)

    elapsed = time.monotonic() - start_time

    # Post-batch: re-derive, classify, regenerate, incremental index dry-run
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)

    # Re-derive articles
    from catalyst_data.rederive import rederive_polygon_news
    rederive_counts = rederive_polygon_news(db_path)

    # Classify source tiers
    from catalyst_data.source_tier import classify_articles
    classify_articles(conn)

    # Regenerate clean_assets
    from catalyst_data.regenerate_clean import regenerate_polygon_clean_assets
    clean_counts = regenerate_polygon_clean_assets(db_path)

    # Incremental index dry-run (delta-only, no writes)
    from catalyst_data.index_builder import build_incremental_records
    inc = build_incremental_records(conn, min_l2_chars=800)

    # Close ingestion run
    close_ingestion_run(
        conn,
        run_id=run_id,
        success_count=cells_success,
        fail_count=cells_failed,
    )

    # Freshness after
    freshness_after = freshness_report(conn)
    conn.close()

    return {
        "run_id": run_id,
        "mode": "update",
        "cells_total": len(missing),
        "cells_success": cells_success,
        "cells_failed": cells_failed,
        "cells_skipped": 0,
        "missing_cells": [],
        "articles_upserted": rederive_counts.get("articles_upserted", 0),
        "clean_assets_inserted": clean_counts.get("clean_assets_inserted", 0),
        "index_delta_new": inc.get("new_article_count", 0),
        "index_delta_changed": inc.get("changed_article_count", 0),
        "index_would_embed": inc.get("would_embed_count", 0),
        "elapsed_sec": round(elapsed, 2),
        "freshness_before": freshness_before,
        "freshness_after": freshness_after,
        "per_cell_report": per_cell,
    }
