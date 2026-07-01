"""Backfill pipeline — date-window chunking around run_update_batch.

Splits a wide date range into smaller non-overlapping windows for safety
and resumability.  Each chunk runs independently through
update_pipeline.run_update_batch; checkpoint/resume means an interrupted
backfill continues from the last successful cell.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from catalyst_data.update_pipeline import run_update_batch

FetchFn = Any  # async callable


def _chunk_date_range(
    from_date: str, to_date: str, chunk_days: int
) -> list[tuple[str, str]]:
    """Split [from_date, to_date] into non-overlapping windows of chunk_days.

    Each window is (chunk_from, chunk_to).  The last window may be shorter.
    """
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    if start > end:
        return []

    chunks: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        chunks.append((cursor.isoformat(), chunk_end.isoformat()))
        cursor = chunk_end + timedelta(days=1)
    return chunks


async def run_backfill(
    db_path: str | Path,
    *,
    tickers: list[str] | None = None,
    sources: list[str] | None = None,
    from_date: str,
    to_date: str,
    fetch_fn: FetchFn | None = None,
    limiter: Any | None = None,
    chunk_days: int = 7,
    limit_per_chunk: int | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Run a backfill over [from_date, to_date] in chunk_days-sized windows.

    Each chunk calls run_update_batch independently.
    Returns a list of per-chunk report dicts.
    """
    chunks = _chunk_date_range(from_date, to_date, chunk_days)
    results: list[dict[str, Any]] = []

    for chunk_idx, (chunk_from, chunk_to) in enumerate(chunks):
        report = await run_update_batch(
            db_path,
            tickers=tickers,
            sources=sources,
            from_date=chunk_from,
            to_date=chunk_to,
            fetch_fn=fetch_fn,
            limiter=limiter,
            limit=limit_per_chunk,
            dry_run=dry_run,
        )
        report["chunk_index"] = chunk_idx
        report["chunk_from"] = chunk_from
        report["chunk_to"] = chunk_to
        results.append(report)

    return results
