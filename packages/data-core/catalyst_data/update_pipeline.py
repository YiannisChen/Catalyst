"""Update/backfill pipeline engine — Polygon-first, SEC filings, checkpoint/resume.

Reads the universe → computes freshness/missing windows → fetches (honoring
rate policies) → archives → normalizes → tier-classifies → regenerates
clean_assets → runs incremental indexer dry-run → emits report.

Dry-run (dry_run=True): computes missing cells only.  ZERO network, ZERO
DB writes.  Real path uses an injected fetch_fn (async callable).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Awaitable

from catalyst_data.freshness import latest_local_ohlcv_date, freshness_report
from catalyst_data.quality import (
    open_ingestion_run,
    close_ingestion_run,
    write_source_checkpoint,
    close_stale_runs,
)
from catalyst_data.storage.sqlite import (
    init_db,
    compute_asset_id,
    upsert_raw_asset,
)
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
    fetcher_ns: Any | None = None,
    submissions_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch one (ticker, date, source) cell.

    For polygon_news: delegates to orchestrator.process_request.
    For sec_filings: uses SEC-specific pipeline (fetch submissions,
    normalize, resolve documents, upsert filings/documents, Bronze archive).

    Returns {ticker, date, source, status, error, articles_count,
             filings_count, documents_count}.
    """
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)

    retries = 0
    last_error: str | None = None
    error_class: str | None = None

    max_retries = (retry_config or {}).get("max_retries", 1)

    # ── SEC filings path ──
    if source == "sec_filings":
        if fetcher_ns is None:
            raise ValueError(
                "fetcher_ns (SEC namespace from create_sec_fetcher) is "
                "required for source='sec_filings'"
            )
        try:
            result = await _fetch_cell_sec(
                conn, ticker, date, run_id, fetcher_ns,
                limiter=limiter, submissions_cache=submissions_cache,
            )
            conn.close()
            return result
        except Exception as exc:
            last_error = str(exc)
            error_class = type(exc).__name__
            write_source_checkpoint(
                conn, run_id=run_id, source_type=source,
                ticker=ticker, date=date,
                status="failed", error_class=error_class, retries=0,
            )
            conn.close()
            return {
                "ticker": ticker, "date": date, "source": source,
                "status": "failed", "error": last_error,
                "articles_count": 0, "filings_count": 0,
                "documents_count": 0, "retries": 0,
            }

    # ── Polygon news path (original) ──
    for attempt in range(max_retries + 1):
        try:
            if limiter is not None:
                await limiter.acquire()

            from catalyst_data.orchestrator import process_request

            results = await process_request(
                ticker=ticker,
                date=date,
                sources=[source],
                db_path=db_path,
                fetch_fn=fetch_fn,
                limiter=limiter,
            )

            result = results[0] if results else {"ok": False, "error": "no_result"}
            ok = result.get("ok", False)
            error = result.get("error")
            row = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE source_type = ? "
                "AND ticker = ? AND reference_date = ?",
                (source, ticker, date),
            ).fetchone()
            articles_count = row[0] if row else 0

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
                conn.close()
                return {
                    "ticker": ticker,
                    "date": date,
                    "source": source,
                    "status": "success",
                    "error": None,
                    "articles_count": articles_count,
                    "filings_count": 0,
                    "documents_count": 0,
                    "retries": retries,
                }
            else:
                last_error = str(error) if error else "unknown"
                error_class = type(error).__name__ if error else "UnknownError"
                retries = attempt

        except Exception as exc:
            last_error = str(exc)
            error_class = type(exc).__name__
            retries = attempt

        if attempt < max_retries:
            wait_s = (retry_config or {}).get("base_delay", 5.0) * (2 ** attempt)
            await asyncio.sleep(wait_s)

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
        "filings_count": 0,
        "documents_count": 0,
        "retries": retries,
    }


async def _fetch_cell_sec(
    conn: sqlite3.Connection,
    ticker: str,
    date: str,
    run_id: str,
    fetcher_ns: SimpleNamespace,
    limiter: Any | None = None,
    submissions_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Handle one SEC filings cell: fetch → normalize → resolve docs → upsert → archive.

    Returns {ticker, date, source, status, filings_count, documents_count}.
    """
    from catalyst_data.cik_map import ticker_to_cik
    from catalyst_data.pipeline.sec_normalize import normalize_submissions, resolve_filing_documents
    from catalyst_data.storage.sqlite import upsert_filing, upsert_filing_document

    try:
        cik = ticker_to_cik(ticker)
    except KeyError:
        write_source_checkpoint(
            conn, run_id=run_id, source_type="sec_filings",
            ticker=ticker, date=date,
            status="failed", error_class="NoCIK",
            retries=0,
        )
        return {
            "ticker": ticker, "date": date, "source": "sec_filings",
            "status": "failed", "error": f"No CIK for ticker {ticker}",
            "articles_count": 0, "filings_count": 0,
            "documents_count": 0, "retries": 0,
        }

    # 1. Fetch submissions (cached per-run per ticker)
    cache_key = f"submissions:{ticker}"
    submissions_result = None

    if submissions_cache is not None and cache_key in submissions_cache:
        cached = submissions_cache[cache_key]
        submissions_result = cached
    else:
        if limiter is not None:
            await limiter.acquire()
        submissions_result = await fetcher_ns.fetch(
            ticker, "sec_submissions", date
        )
        if submissions_cache is not None:
            submissions_cache[cache_key] = submissions_result

    if submissions_result.status != 200:
        error_msg = submissions_result.error or f"HTTP {submissions_result.status}"
        write_source_checkpoint(
            conn, run_id=run_id, source_type="sec_filings",
            ticker=ticker, date=date,
            status="failed",
            error_class=f"SEC{submissions_result.status}",
            retries=0,
        )
        return {
            "ticker": ticker, "date": date, "source": "sec_filings",
            "status": "failed", "error": error_msg,
            "articles_count": 0, "filings_count": 0,
            "documents_count": 0, "retries": 0,
        }

    raw_data = submissions_result.data

    # 2. Normalize submissions → filings in date range
    filing_dicts = normalize_submissions(raw_data, ticker, cik, date, date)

    # 3. Bronze archive: store submissions JSON (carry-forward C)
    submissions_json_bytes = json.dumps(raw_data, ensure_ascii=True).encode("utf-8")
    fetched_at_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    submissions_asset_id = compute_asset_id(
        ticker, fetched_at_date, "sec_submissions"
    )
    response_hash = hashlib.sha256(submissions_json_bytes).hexdigest()[:16]

    upsert_raw_asset(
        conn,
        asset_id=submissions_asset_id,
        ticker=ticker,
        source_type="sec_submissions",
        reference_date=fetched_at_date,
        content_raw=submissions_json_bytes,
        http_status=200,
        metadata={
            "cik": cik,
            "response_hash": response_hash,
            "endpoints": ["sec_submissions"],
        },
    )

    # 4. For each filing: upsert filing, resolve documents for rag-eligible 8-Ks
    filings_count = 0
    documents_count = 0

    for fd in filing_dicts:
        upsert_filing(
            conn,
            filing_id=fd["filing_id"],
            cik=fd["cik"],
            ticker=fd["ticker"],
            form_type=fd["form_type"],
            filed_at=fd["filed_at"],
            accession_number=fd["accession_number"],
            url=fd["url"],
            period=fd.get("period"),
            primary_document=fd.get("primary_document"),
            items_json=fd.get("items_json"),
            source_tier=fd.get("source_tier", 1),
            dedup_group_id=fd.get("dedup_group_id"),
            is_rag_eligible=fd.get("is_rag_eligible", 1),
            raw_asset_id=submissions_asset_id,
        )
        filings_count += 1

        # 4a. Resolve filing documents for rag-eligible 8-Ks
        if fd.get("is_rag_eligible") and fd.get("form_type") == "8-K":
            docs = await resolve_filing_documents(fetcher_ns, fd)
            for doc_dict in docs:
                upsert_filing_document(
                    conn,
                    filing_id=doc_dict["filing_id"],
                    document_url=doc_dict["document_url"],
                    document_type=doc_dict.get("document_type", "primary_doc"),
                    text=doc_dict.get("text"),
                    char_len=doc_dict.get("char_len"),
                    content_type=doc_dict.get("content_type"),
                    byte_size=doc_dict.get("byte_size"),
                    extraction_status=doc_dict.get("extraction_status", "fetch_failed"),
                )
                documents_count += 1

                # 4b. Bronze archive: store EACH document's RAW HTML (immutable, re-derivable)
                doc_bytes = doc_dict.get("raw_bytes") or b""
                doc_type = doc_dict.get("document_type", "primary_doc")
                doc_asset_id = compute_asset_id(
                    ticker,
                    fd["filed_at"],
                    f"sec_primary_doc:{fd['filing_id']}:{doc_type}",
                )
                upsert_raw_asset(
                    conn,
                    asset_id=doc_asset_id,
                    ticker=ticker,
                    source_type="sec_primary_doc",
                    reference_date=fd["filed_at"],
                    content_raw=doc_bytes,
                    http_status=200,
                    metadata={
                        "url": doc_dict["document_url"],
                        "filing_id": fd["filing_id"],
                        "document_type": doc_type,
                        "content_type": doc_dict.get("content_type", ""),
                        "byte_size": doc_dict.get("byte_size", 0),
                    },
                )

    # 5. Write checkpoint — status='success' even if 0 filings (T9 semantics)
    write_source_checkpoint(
        conn, run_id=run_id, source_type="sec_filings",
        ticker=ticker, date=date,
        status="success", retries=0,
    )

    return {
        "ticker": ticker,
        "date": date,
        "source": "sec_filings",
        "status": "success",
        "error": None,
        "articles_count": 0,
        "filings_count": filings_count,
        "documents_count": documents_count,
        "retries": 0,
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

    # ── Construct SEC fetcher namespace if sec_filings is in sources ──
    fetcher_ns: Any | None = None
    submissions_cache: dict[str, Any] = {}

    if "sec_filings" in sources:
        from catalyst_data.connectors.sec import create_sec_fetcher
        import os

        user_agent = os.environ.get(
            "SEC_USER_AGENT",
            "Catalyst/1.0 (contact@example.com)",
        )
        fetcher_ns = create_sec_fetcher(
            user_agent=user_agent,
            limiter=limiter,
        )
        # Use the namespace's fetch as the fetch_fn for SEC cells
        # (polygon cells still use the original fetch_fn)
        # We'll dispatch per-source in _fetch_cell.

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
        # Determine which fetch_fn to use for this source
        cell_fetch_fn = fetch_fn
        cell_fetcher_ns = None
        cell_cache = None

        if src == "sec_filings":
            if fetcher_ns is None:
                raise ValueError(
                    "fetcher_ns not constructed; sec_filings is in sources "
                    "but create_sec_fetcher failed?"
                )
            cell_fetch_fn = fetcher_ns.fetch  # base fetch callable
            cell_fetcher_ns = fetcher_ns
            cell_cache = submissions_cache

        result = await _fetch_cell(
            db_path, ticker, td, src, run_id, cell_fetch_fn,
            limiter=limiter,
            fetcher_ns=cell_fetcher_ns,
            submissions_cache=cell_cache,
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

    # Re-derive articles (polygon only)
    articles_upserted = 0
    clean_assets_inserted = 0

    if "polygon_news" in sources:
        from catalyst_data.rederive import rederive_polygon_news
        rederive_counts = rederive_polygon_news(db_path)
        articles_upserted = rederive_counts.get("articles_upserted", 0)

        # Classify source tiers (articles only)
        from catalyst_data.source_tier import classify_articles
        classify_articles(conn)

        # Regenerate clean_assets
        from catalyst_data.regenerate_clean import regenerate_polygon_clean_assets
        clean_counts = regenerate_polygon_clean_assets(db_path)
        clean_assets_inserted = clean_counts.get("clean_assets_inserted", 0)

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
        "articles_upserted": articles_upserted,
        "clean_assets_inserted": clean_assets_inserted,
        "index_delta_new": inc.get("new_article_count", 0),
        "index_delta_changed": inc.get("changed_article_count", 0),
        "index_would_embed": inc.get("would_embed_count", 0),
        "elapsed_sec": round(elapsed, 2),
        "freshness_before": freshness_before,
        "freshness_after": freshness_after,
        "per_cell_report": per_cell,
    }
