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
from catalyst_data.connectors.base import FetchResult
from catalyst_data.storage.sqlite import (
    init_db,
    compute_asset_id,
    upsert_raw_asset,
)
from catalyst_data.quality import ensure_ingestion_quality_tables
from catalyst_data.trading_calendar import trading_days_for_window

logger = logging.getLogger(__name__)

# Type for async fetch functions
FetchFn = Callable[..., Awaitable[Any]]

_CANCEL_REQUESTS: set[str] = set()


def request_cancel(run_id: str) -> None:
    """Request cancellation for an in-flight run_update loop.

    The service loop checks this flag between cells.  This in-process hook is
    intentionally small; a future app worker can persist the same intent.
    """
    _CANCEL_REQUESTS.add(run_id)


# ---------------------------------------------------------------------------
# Missing-cell computation
# ---------------------------------------------------------------------------


def _trading_days_in_window(
    conn: sqlite3.Connection, from_date: str, to_date: str
) -> list[str]:
    """Return trading days in [from_date, to_date] sorted ascending.

    Uses the full calendar window (weekdays minus US market holidays) so
    partially populated ohlcv data cannot truncate future gap-fill dates.
    Existing ohlcv dates are unioned in for backward compatibility with
    historical local calendars.
    """
    return trading_days_for_window(conn, from_date, to_date)


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

def persist_cell(
    db_path: str,
    *,
    run_id: str,
    ticker: str,
    date: str,
    source: str,
    fetch_result,       # FetchResult
    error_class,        # str — ErrorClass value
    retries: int = 0,
    items_count: int = 0,
    storage_callable: Callable[[sqlite3.Connection], None] | None = None,
) -> dict[str, Any]:
    """Sync: persist one cell atomically — raw_asset + checkpoint in one transaction.

    Runs via asyncio.to_thread.  Opens one connection, BEGIN IMMEDIATE,
    writes raw_asset + checkpoint, COMMIT.  On failure: ROLLBACK,
    write failed checkpoint on separate conn.

    Split rollback semantics:
      TRANSPORT/TIMEOUT → no raw_asset, failed checkpoint on separate conn
      MALFORMED/PARSE  → raw_asset (forensic) + failed checkpoint, atomic
      EMPTY_VALID      → raw_asset + success_empty checkpoint, atomic
      success          → raw_asset + success checkpoint (caller already stored silver)
    """
    import re
    from catalyst_data.error_taxonomy import ErrorClass

    def _redact(msg):
        if msg is None:
            return None
        msg = re.sub(r'sk-[a-zA-Z0-9]+', '[REDACTED]', msg)
        msg = re.sub(r'Bearer\s+[a-zA-Z0-9._\-]+', 'Bearer [REDACTED]', msg)
        return msg

    result_status = getattr(fetch_result, 'status', None)
    result_error = getattr(fetch_result, 'error', None)
    result_latency = getattr(fetch_result, 'latency_ms', None)
    result_retry_after = getattr(fetch_result, 'retry_after_seconds', None)
    result_items = getattr(fetch_result, 'items_count', items_count)

    is_transport = error_class in (ErrorClass.TRANSPORT.value, ErrorClass.TIMEOUT.value)
    is_malformed = error_class in (ErrorClass.MALFORMED_RESPONSE.value, ErrorClass.PARSE_FAILURE.value)

    # Transport/timeout: store nothing in main transaction, write failed checkpoint
    if is_transport:
        sep_conn = sqlite3.connect(db_path)
        try:
            from catalyst_data.quality import ensure_ingestion_quality_tables
            ensure_ingestion_quality_tables(sep_conn)
            from catalyst_data.quality import write_source_checkpoint
            write_source_checkpoint(
                sep_conn, run_id=run_id, source_type=source,
                ticker=ticker, date=date, status="failed",
                error_class=error_class,
                error_message_redacted=_redact(result_error),
                http_status=result_status,
                retry_after_seconds=result_retry_after,
                provider_latency_ms=result_latency,
                retries=retries,
            )
        finally:
            sep_conn.close()
        return {
            "ticker": ticker, "date": date, "source": source,
            "status": "failed", "error": result_error,
            "articles_count": 0, "filings_count": 0, "documents_count": 0,
            "retries": retries, "error_class": error_class,
        }

    # All other cases: open main conn with BEGIN IMMEDIATE
    conn = sqlite3.connect(db_path)
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.quality import ensure_ingestion_quality_tables
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    if source == "polygon_news":
        # Match orchestrator._store_bronze_and_silver's legacy polygon pragma:
        # clean_assets.asset_id stores per-article ids that do not reference
        # raw_assets.asset_id.  The pragma must be set before BEGIN so raw,
        # silver, and checkpoint can live in the same transaction.
        conn.execute("PRAGMA foreign_keys=OFF")

    try:
        conn.execute("BEGIN IMMEDIATE")

        asset_id = f"{source}:{ticker}:{date}:{run_id[:8]}"

        if is_malformed:
            # Store raw_asset forensically + failed checkpoint
            from catalyst_data.storage.sqlite import upsert_raw_asset
            raw_data = getattr(fetch_result, 'data', None)
            raw_bytes = json.dumps(raw_data or {}, default=str).encode("utf-8")
            upsert_raw_asset(
                conn, asset_id=asset_id, ticker=ticker, source_type=source,
                reference_date=date, content_raw=raw_bytes,
                http_status=result_status,
                metadata={"error_class": error_class},
                commit=False,
            )
            from catalyst_data.quality import write_source_checkpoint
            write_source_checkpoint(
                conn, run_id=run_id, source_type=source,
                ticker=ticker, date=date, status="failed",
                error_class=error_class,
                error_message_redacted=_redact(result_error),
                http_status=result_status,
                retry_after_seconds=result_retry_after,
                provider_latency_ms=result_latency,
                raw_asset_id=asset_id,
                retries=retries,
                commit=False,
            )
            conn.commit()
            return {
                "ticker": ticker, "date": date, "source": source,
                "status": "failed", "error": result_error,
                "articles_count": 0, "filings_count": 0,
                "documents_count": 0, "retries": retries,
                "error_class": error_class, "asset_id": asset_id,
            }

        if error_class == ErrorClass.EMPTY_VALID.value:
            # Store raw_asset + success_empty checkpoint
            from catalyst_data.storage.sqlite import upsert_raw_asset
            raw_data = getattr(fetch_result, 'data', None)
            raw_bytes = json.dumps(raw_data or {}, default=str).encode("utf-8")
            upsert_raw_asset(
                conn, asset_id=asset_id, ticker=ticker, source_type=source,
                reference_date=date, content_raw=raw_bytes,
                http_status=result_status,
                commit=False,
            )
            from catalyst_data.quality import write_source_checkpoint
            write_source_checkpoint(
                conn, run_id=run_id, source_type=source,
                ticker=ticker, date=date, status="success_empty",
                error_class=error_class,
                http_status=result_status,
                provider_latency_ms=result_latency,
                raw_asset_id=asset_id,
                items_count=0,
                retries=retries,
                commit=False,
            )
            conn.commit()
            return {
                "ticker": ticker, "date": date, "source": source,
                "status": "success_empty", "error": None,
                "articles_count": 0, "filings_count": 0,
                "documents_count": 0, "retries": retries,
                "asset_id": asset_id,
            }

        # Terminal failures (AUTH, RATE_LIMIT, PROVIDER_5XX, BUDGET_EXHAUSTED, UNKNOWN):
        # These produced a response but aren't malformed — write failed checkpoint
        # without storing raw_asset (no forensic value for auth/rate errors)
        if error_class:
            from catalyst_data.quality import write_source_checkpoint
            write_source_checkpoint(
                conn, run_id=run_id, source_type=source,
                ticker=ticker, date=date, status="failed",
                error_class=error_class,
                error_message_redacted=_redact(result_error),
                http_status=result_status,
                retry_after_seconds=result_retry_after,
                provider_latency_ms=result_latency,
                retries=retries,
                commit=False,
            )
            conn.commit()
            return {
                "ticker": ticker, "date": date, "source": source,
                "status": "failed", "error": result_error,
                "articles_count": 0, "filings_count": 0,
                "documents_count": 0, "retries": retries,
                "error_class": error_class,
            }

        # Success: store raw_asset + silver + checkpoint in one transaction.
        # storage_callable(conn) must upsert raw_asset and silver on *conn*.
        try:
            from catalyst_data.quality import write_source_checkpoint
            actual_asset_id = asset_id
            if storage_callable is not None:
                stored_asset_id = storage_callable(conn)
                if stored_asset_id:
                    actual_asset_id = stored_asset_id
            write_source_checkpoint(
                conn, run_id=run_id, source_type=source,
                ticker=ticker, date=date, status="success",
                http_status=result_status,
                provider_latency_ms=result_latency,
                retry_after_seconds=result_retry_after,
                raw_asset_id=actual_asset_id,
                items_count=result_items,
                retries=retries,
                commit=False,
            )
            conn.commit()
            return {
                "ticker": ticker, "date": date, "source": source,
                "status": "success", "error": None,
                "articles_count": result_items,
                "filings_count": 0, "documents_count": 0,
                "retries": retries, "asset_id": asset_id,
            }
        except Exception:
            conn.rollback()
            # Write failed checkpoint on separate connection
            sep_conn = sqlite3.connect(db_path)
            try:
                ensure_ingestion_quality_tables(sep_conn)
                from catalyst_data.quality import write_source_checkpoint
                write_source_checkpoint(
                    sep_conn, run_id=run_id, source_type=source,
                    ticker=ticker, date=date, status="failed",
                    error_class=ErrorClass.UNKNOWN.value,
                    error_message_redacted=_redact(str(exc) if 'exc' in dir() else "storage failed"),
                    retries=retries,
                )
            finally:
                sep_conn.close()
            raise

    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        # Write failed checkpoint on separate connection
        sep_conn = sqlite3.connect(db_path)
        try:
            ensure_ingestion_quality_tables(sep_conn)
            from catalyst_data.quality import write_source_checkpoint
            write_source_checkpoint(
                sep_conn, run_id=run_id, source_type=source,
                ticker=ticker, date=date, status="failed",
                error_class=error_class or ErrorClass.UNKNOWN.value,
                error_message_redacted=_redact(str(exc)),
                http_status=result_status,
                retries=retries,
            )
        finally:
            sep_conn.close()
        return {
            "ticker": ticker, "date": date, "source": source,
            "status": "failed", "error": str(exc),
            "articles_count": 0, "filings_count": 0,
            "documents_count": 0, "retries": retries,
            "error_class": error_class,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
    last_status_code: int | None = None

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
            conn.close()
            from catalyst_data.error_taxonomy import classify_fetch_error
            ec = classify_fetch_error(None, error_message=last_error,
                                       exception_type=error_class)
            fr = FetchResult(status=0, error=last_error, source_label=source)
            return persist_cell(
                db_path, run_id=run_id, ticker=ticker, date=date,
                source=source, fetch_result=fr, error_class=ec.value,
                retries=0,
            )

    # ── Finnhub company-news path ──
    if source == "finnhub_company_news":
        if fetcher_ns is None:
            raise ValueError(
                "fetcher_ns (Finnhub namespace from create_finnhub_fetcher) "
                "is required for source='finnhub_company_news'"
            )
        try:
            result = await _fetch_cell_finnhub(
                conn, ticker, date, run_id, fetcher_ns,
                limiter=limiter,
            )
            conn.close()
            return result
        except Exception as exc:
            last_error = str(exc)
            error_class = type(exc).__name__
            conn.close()
            from catalyst_data.error_taxonomy import classify_fetch_error
            ec = classify_fetch_error(None, error_message=last_error,
                                       exception_type=error_class)
            fr = FetchResult(status=0, error=last_error, source_label=source)
            return persist_cell(
                db_path, run_id=run_id, ticker=ticker, date=date,
                source=source, fetch_result=fr, error_class=ec.value,
                retries=0,
            )

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
                store=False,
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
                asset_id = result.get("asset_id")
                # Capture storage data from process_request before closing conn
                raw_bytes = result.get("_raw_bytes")
                clean_rows = result.get("_clean_rows")
                http_s = result.get("_http_status")
                endpoints = result.get("_endpoints") or []
                ohlcv_bar = result.get("_ohlcv_bar")
                conn.close()

                # Build storage_callable that runs _store_bronze_and_silver on
                # persist_cell's shared conn — raw+silver+checkpoint in one transaction
                def _store_fn(shared_conn):
                    from catalyst_data.orchestrator import _store_bronze_and_silver
                    if raw_bytes is not None:
                        storage_err = _store_bronze_and_silver(
                            db_path, conn=shared_conn,
                            asset_id=asset_id, ticker=ticker,
                            source=source, date=date,
                            raw_bytes=raw_bytes, http_status=http_s,
                            endpoints=endpoints, clean_rows=clean_rows or [],
                            ohlcv_bar=ohlcv_bar,
                        )
                        if storage_err:
                            raise RuntimeError(storage_err)
                        return asset_id
                    return None

                fr = FetchResult(status=200, data=None, latency_ms=0, source_label=source,
                                 items_count=articles_count)
                return persist_cell(
                    db_path, run_id=run_id, ticker=ticker, date=date,
                    source=source, fetch_result=fr, error_class=None,
                    retries=retries, items_count=articles_count,
                    storage_callable=_store_fn if raw_bytes is not None else None,
                )
            else:
                last_error = str(error) if error else "unknown"
                error_class = type(error).__name__ if error else "UnknownError"
                statuses = result.get("endpoint_statuses") or {}
                failed_statuses = [s for s in statuses.values() if s and s != 200]
                if failed_statuses:
                    last_status_code = failed_statuses[0]
                retries = attempt

        except Exception as exc:
            last_error = str(exc)
            error_class = type(exc).__name__
            retries = attempt

        if attempt < max_retries:
            wait_s = (retry_config or {}).get("base_delay", 5.0) * (2 ** attempt)
            await asyncio.sleep(wait_s)

    conn.close()
    # Route all failure paths through persist_cell
    from catalyst_data.error_taxonomy import classify_fetch_error
    ec = classify_fetch_error(last_status_code, error_message=last_error,
                               exception_type=error_class)
    fr = FetchResult(status=0, error=last_error, source_label=source)
    return persist_cell(
        db_path, run_id=run_id, ticker=ticker, date=date,
        source=source, fetch_result=fr, error_class=ec.value,
        retries=retries,
    )



async def _fetch_cell_finnhub(
    conn: sqlite3.Connection,
    ticker: str,
    date: str,
    run_id: str,
    fetcher_ns: SimpleNamespace,
    limiter: Any | None = None,
) -> dict[str, Any]:
    """Handle one Finnhub company-news cell: fetch → Bronze archive.

    Returns {ticker, date, source, status, articles_count}.
    Silver (articles/article_tickers) is derived post-batch via rederive_finnhub_news().
    """
    import json

    try:
        result = await fetcher_ns.fetch(ticker, "company-news", date)
    except Exception as exc:
        write_source_checkpoint(
            conn, run_id=run_id, source_type="finnhub_company_news",
            ticker=ticker, date=date,
            status="failed", error_class=type(exc).__name__, retries=0,
        )
        return {
            "ticker": ticker, "date": date, "source": "finnhub_company_news",
            "status": "failed", "error": str(exc),
            "articles_count": 0, "filings_count": 0,
            "documents_count": 0, "retries": 0,
        }

    if result.status != 200:
        error_msg = result.error or f"HTTP {result.status}"
        write_source_checkpoint(
            conn, run_id=run_id, source_type="finnhub_company_news",
            ticker=ticker, date=date,
            status="failed",
            error_class=f"Finnhub{result.status}",
            retries=0,
        )
        return {
            "ticker": ticker, "date": date, "source": "finnhub_company_news",
            "status": "failed", "error": error_msg,
            "articles_count": 0, "filings_count": 0,
            "documents_count": 0, "retries": 0,
        }

    # Bronze archive: store raw Finnhub JSON response
    articles_data = result.data if isinstance(result.data, list) else []
    raw_json_bytes = json.dumps(articles_data, ensure_ascii=True).encode("utf-8")

    asset_id = compute_asset_id(ticker, date, "finnhub_company_news")

    try:
        conn.execute("BEGIN IMMEDIATE")
        upsert_raw_asset(
            conn,
            asset_id=asset_id,
            ticker=ticker,
            source_type="finnhub_company_news",
            reference_date=date,
            content_raw=raw_json_bytes,
            http_status=200,
            metadata={
                "endpoints": ["company-news"],
                "article_count": len(articles_data),
            },
            commit=False,
        )

        # Write checkpoint — success even if 0 articles (cell is covered)
        write_source_checkpoint(
            conn, run_id=run_id, source_type="finnhub_company_news",
            ticker=ticker, date=date,
            status="success", retries=0, raw_asset_id=asset_id,
            items_count=len(articles_data), http_status=200,
            provider_latency_ms=getattr(result, "latency_ms", None),
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "ticker": ticker,
        "date": date,
        "source": "finnhub_company_news",
        "status": "success",
        "error": None,
        "articles_count": len(articles_data),
        "filings_count": 0,
        "documents_count": 0,
        "retries": 0,
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
    cache_key = f"submissions:{ticker}:{date}"
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

    # 4. For each filing: upsert filing, resolve documents for rag-eligible 8-Ks
    filings_count = 0
    documents_count = 0

    try:
        conn.execute("BEGIN IMMEDIATE")
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
            commit=False,
        )

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
                commit=False,
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
                        commit=False,
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
                        commit=False,
                    )

        # 5. Write checkpoint — status='success' even if 0 filings (T9 semantics)
        write_source_checkpoint(
            conn, run_id=run_id, source_type="sec_filings",
            ticker=ticker, date=date,
            status="success", retries=0, raw_asset_id=submissions_asset_id,
            items_count=filings_count, http_status=200,
            provider_latency_ms=getattr(submissions_result, "latency_ms", None),
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

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

    import os

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
    finnhub_fetcher_ns: Any | None = None
    submissions_cache: dict[str, Any] = {}

    if "finnhub_company_news" in sources:
        from catalyst_data.connectors.finnhub import create_finnhub_fetcher

        finnhub_api_key = os.environ.get("FINNHUB_API_KEY", "")
        if not finnhub_api_key:
            logger.warning(
                "FINNHUB_API_KEY not set — finnhub_company_news cells will fail"
            )
        finnhub_fetcher_ns = create_finnhub_fetcher(
            api_key=finnhub_api_key,
            limiter=limiter,
        )

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

        if src == "finnhub_company_news":
            if finnhub_fetcher_ns is None:
                raise ValueError(
                    "finnhub_fetcher_ns not constructed; finnhub_company_news "
                    "is in sources but create_finnhub_fetcher failed?"
                )
            cell_fetch_fn = finnhub_fetcher_ns.fetch
            cell_fetcher_ns = finnhub_fetcher_ns
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

        # Unwrap dict fetch_fn (CLI passes {"source_type": callable} dict)
        if isinstance(cell_fetch_fn, dict):
            if src in cell_fetch_fn:
                cell_fetch_fn = cell_fetch_fn[src]

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

    # Post-batch: re-derive ALL sources → classify → dedup → regenerate → index
    # ORDER MATTERS: classify and dedup must see ALL freshly-rederived rows.

    articles_upserted = 0
    finnhub_articles_upserted = 0
    clean_assets_inserted = 0
    dedup_groups = 0

    # 1. Re-derive Polygon articles from Bronze (if polygon_news in sources)
    if "polygon_news" in sources:
        from catalyst_data.rederive import rederive_polygon_news
        rederive_counts = rederive_polygon_news(db_path)
        articles_upserted = rederive_counts.get("articles_upserted", 0)

    # 2. Re-derive Finnhub articles from Bronze (if finnhub_company_news in sources)
    if "finnhub_company_news" in sources:
        from catalyst_data.pipeline.finnhub_normalize import rederive_finnhub_news
        finnhub_counts = rederive_finnhub_news(db_path)
        finnhub_articles_upserted = finnhub_counts.get("articles_upserted", 0)

    # Re-open connection for subsequent steps
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)

    # 3. Classify source tiers — run for ALL articles with NULL source_tier
    from catalyst_data.source_tier import classify_articles
    classify_articles(conn)

    # 4. Cross-source dedup — run after any prose provider update.
    # The materializer scans the full corrected prose corpus, so a Polygon-only
    # batch can still dedup against existing Finnhub rows.
    if any(src in sources for src in ("polygon_news", "finnhub_company_news")):
        from catalyst_data.dedup.cross_source import compute_cross_source_dedup
        dedup_groups = compute_cross_source_dedup(conn)

    # 5. Regenerate clean_assets (polygon only)
    if "polygon_news" in sources:
        from catalyst_data.regenerate_clean import regenerate_polygon_clean_assets
        clean_counts = regenerate_polygon_clean_assets(db_path)
        clean_assets_inserted = clean_counts.get("inserted_article_rows", 0)

    # 6. Incremental index dry-run (delta-only, no writes)
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
        "finnhub_articles_upserted": finnhub_articles_upserted,
        "dedup_groups_resolved": dedup_groups,
        "clean_assets_inserted": clean_assets_inserted,
        "index_delta_new": inc.get("new_article_count", 0),
        "index_delta_changed": inc.get("changed_article_count", 0),
        "index_would_embed": inc.get("would_embed_count", 0),
        "elapsed_sec": round(elapsed, 2),
        "freshness_before": freshness_before,
        "freshness_after": freshness_after,
        "per_cell_report": per_cell,
    }


# ---------------------------------------------------------------------------
# H3 service entrypoint
# ---------------------------------------------------------------------------

def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(s in lk for s in ("key", "token", "secret", "password")):
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact_obj(v)
        return out
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    if isinstance(obj, str):
        import re
        obj = re.sub(r"sk-[A-Za-z0-9._-]+", "[REDACTED]", obj)
        obj = re.sub(r"api[_-]?key=[^&\s]+", "api_key=[REDACTED]", obj, flags=re.I)
        obj = re.sub(r"token=[^&\s]+", "token=[REDACTED]", obj, flags=re.I)
    return obj


def _config_report_dict(config: Any) -> dict[str, Any]:
    from dataclasses import asdict

    data = asdict(config)
    data.pop("fetch_fn", None)
    return _redact_obj(data)


def _cell_fetcher(fetch_map: Any, source: str):
    if isinstance(fetch_map, dict):
        return fetch_map.get(source)
    return fetch_map


def _fetcher_namespace(source: str, fetch_callable: Any):
    if fetch_callable is None:
        return None
    if hasattr(fetch_callable, "fetch") or hasattr(fetch_callable, "fetch_document"):
        return fetch_callable
    if source == "finnhub_company_news":
        return SimpleNamespace(fetch=fetch_callable)
    if source == "sec_filings":
        return SimpleNamespace(fetch=fetch_callable, fetch_document=fetch_callable)
    return None


def _count_rows(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("raw_assets", "articles", "article_tickers", "filings",
              "filing_documents", "macro_observations", "index_state")
    counts = {}
    for table in tables:
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = 0
    return counts


def _checkpoint_aggregates(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    providers: dict[str, dict[str, int]] = {}
    retry_histogram: dict[str, Any] = {"total_retries": 0}
    top_errors: dict[str, int] = {}
    fallbacks = {"triggered": 0, "successful": 0,
                 "chain": "polygon_news -> finnhub_company_news"}

    rows = conn.execute(
        """SELECT source_type, status, COALESCE(retries,0), error_class,
                  COALESCE(fallback_triggered,0)
           FROM source_checkpoints WHERE run_id = ?""",
        (run_id,),
    ).fetchall()
    for source, status, retries, error_class, fallback_triggered in rows:
        p = providers.setdefault(source, {
            "cells_total": 0, "cells_success": 0, "cells_failed": 0,
            "cells_skipped": 0, "cells_success_empty": 0,
        })
        p["cells_total"] += 1
        if status == "success":
            p["cells_success"] += 1
        elif status == "success_empty":
            p["cells_success_empty"] += 1
        elif status == "skipped":
            p["cells_skipped"] += 1
        elif status == "failed":
            p["cells_failed"] += 1
        rh = retry_histogram.setdefault(source, {})
        rh[str(retries)] = rh.get(str(retries), 0) + 1
        retry_histogram["total_retries"] += int(retries or 0)
        if error_class:
            top_errors[error_class] = top_errors.get(error_class, 0) + 1
        if fallback_triggered:
            fallbacks["triggered"] += 1
            if status in ("success", "success_empty"):
                fallbacks["successful"] += 1
    return {
        "providers": providers,
        "retry_histogram": retry_histogram,
        "top_error_classes": top_errors,
        "fallbacks": fallbacks,
    }


def _final_status(cells_success: int, cells_failed: int, cells_skipped: int, canceled: bool) -> str:
    if canceled:
        return "canceled"
    if cells_failed and cells_success:
        return "partial"
    if cells_failed:
        return "failed"
    return "succeeded"


def _mark_progress(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source: str | None = None,
    ticker: str | None = None,
    date: str | None = None,
    cells_done: int | None = None,
    canceled: bool = False,
) -> None:
    sets = []
    params: list[Any] = []
    if source is not None:
        sets.append("current_source = ?"); params.append(source)
    if ticker is not None:
        sets.append("current_ticker = ?"); params.append(ticker)
    if date is not None:
        sets.append('"current_date" = ?'); params.append(date)
    if cells_done is not None:
        sets.append("cells_done = ?"); params.append(cells_done)
    if canceled:
        sets.append("canceled_at = ?"); params.append(datetime.now(timezone.utc).isoformat())
    if not sets:
        return
    params.append(run_id)
    conn.execute(f"UPDATE ingestion_runs SET {', '.join(sets)} WHERE run_id = ?", params)
    conn.commit()


def run_update(config) -> Any:
    """Single button-callable update entrypoint.

    Drives the real cell loop, persists a RunReport JSON, updates
    ingestion_runs.report_path, and returns the RunReport.  Never sys.exit().
    """
    from dataclasses import asdict
    from catalyst_data.fallback import FallbackPolicy
    from catalyst_data.run_report import RunReport, save_run_report

    db_path = str(getattr(config, "db_path", "data/catalyst_dev_ws4b.db"))
    sources = list(config.sources or ["polygon_news"])
    fetch_map = getattr(config, "fetch_fn", None)
    report_dir = getattr(config, "report_dir", "data/run_reports")
    started = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()

    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    before_counts = _count_rows(conn)

    if config.from_date is None or config.to_date is None:
        wm = latest_local_ohlcv_date(conn)
        from_date = config.from_date or wm
        to_date = config.to_date or wm
    else:
        from_date = config.from_date
        to_date = config.to_date

    if config.resume_from:
        rows = conn.execute(
            """SELECT ticker, date, source_type FROM source_checkpoints
               WHERE run_id = ? AND status IN ('failed','skipped')
               ORDER BY source_type, ticker, date""",
            (config.resume_from,),
        ).fetchall()
        missing = [(r[0], r[1], r[2]) for r in rows]
    else:
        missing = compute_missing_cells(
            conn, tickers=config.tickers, sources=sources,
            from_date=from_date, to_date=to_date,
        )
    if config.limit is not None:
        missing = missing[:config.limit]

    resolved_tickers = sorted({m[0] for m in missing}) or (config.tickers or [])
    resolved_sources = sorted({m[2] for m in missing}) or sources
    run_config_json = json.dumps(_config_report_dict(config), default=str, sort_keys=True)
    run_id = open_ingestion_run(
        conn, tickers=resolved_tickers, sources=resolved_sources,
        mode="resume" if config.resume_from else "update",
        notes=config.notes,
        cells_total=len(missing),
        run_config_json=run_config_json,
    )
    if config.resume_from:
        conn.execute(
            "UPDATE ingestion_runs SET run_config_json = ? WHERE run_id = ?",
            (json.dumps({**_config_report_dict(config), "parent_run_id": config.resume_from}, default=str), run_id),
        )
        conn.commit()

    if config.dry_run:
        ended = datetime.now(timezone.utc).isoformat()
        report = RunReport(
            run_id=run_id, mode="dry-run", resume_from=config.resume_from,
            config=_config_report_dict(config), started_at=started, ended_at=ended,
            elapsed_sec=round(time.monotonic() - start_time, 2),
            providers={src: {"cells_total": sum(1 for m in missing if m[2] == src),
                             "cells_success": 0, "cells_failed": 0, "cells_skipped": 0}
                       for src in resolved_sources},
            rows_changed={}, index_state={}, doctor=None,
        )
        path = save_run_report(report, report_dir=report_dir)
        report.report_path = path
        save_run_report(report, report_dir=report_dir)
        conn.execute(
            "UPDATE ingestion_runs SET status='succeeded', ended_at=?, report_path=? WHERE run_id=?",
            (ended, path, run_id),
        )
        conn.commit()
        conn.close()
        return report

    policy = FallbackPolicy()
    cells_success = cells_failed = cells_skipped = 0
    submissions_cache: dict[str, Any] = {}

    async def _run_cells():
        nonlocal cells_success, cells_failed, cells_skipped
        for idx, (ticker, td, src) in enumerate(missing, start=1):
            if run_id in _CANCEL_REQUESTS:
                cells_skipped += len(missing) - idx + 1
                _mark_progress(conn, run_id=run_id, cells_done=idx - 1, canceled=True)
                for st, tk, dt in [(m[2], m[0], m[1]) for m in missing[idx-1:]]:
                    write_source_checkpoint(
                        conn, run_id=run_id, source_type=st,
                        ticker=tk, date=dt, status="skipped",
                        error_class="canceled",
                    )
                break

            _mark_progress(conn, run_id=run_id, source=src, ticker=ticker, date=td)
            fetch_callable = _cell_fetcher(fetch_map, src)
            fetcher_ns = _fetcher_namespace(src, fetch_callable)
            result = await _fetch_cell(
                db_path, ticker, td, src, run_id, fetch_callable,
                fetcher_ns=fetcher_ns,
                submissions_cache=submissions_cache if src == "sec_filings" else None,
            )
            status = result.get("status")

            if (
                status == "failed"
                and config.enable_fallback
                and policy.should_attempt(str(result.get("error_class") or ""), 0)
            ):
                fallback_provider = policy.next_provider(src)
                fallback_callable = _cell_fetcher(fetch_map, fallback_provider) if fallback_provider else None
                if fallback_provider and fallback_callable is not None:
                    fb_ns = _fetcher_namespace(fallback_provider, fallback_callable)
                    fb_result = await _fetch_cell(
                        db_path, ticker, td, fallback_provider, run_id,
                        fallback_callable, fetcher_ns=fb_ns,
                    )
                    conn.execute(
                        """UPDATE source_checkpoints
                           SET fallback_provider = ?, fallback_triggered = 1
                           WHERE run_id = ? AND source_type = ? AND ticker = ? AND date = ?""",
                        (fallback_provider, run_id, fallback_provider, ticker, td),
                    )
                    conn.commit()
                    if fb_result.get("status") in ("success", "success_empty"):
                        cells_success += 1
                    else:
                        cells_failed += 1
                    _mark_progress(conn, run_id=run_id, cells_done=idx)
                    continue

            if status in ("success", "success_empty"):
                cells_success += 1
            elif status == "skipped":
                cells_skipped += 1
            else:
                cells_failed += 1
            _mark_progress(conn, run_id=run_id, cells_done=idx)

    asyncio.run(_run_cells())

    rows_changed = {
        "articles_upserted": 0,
        "finnhub_articles_upserted": 0,
        "clean_assets_inserted": 0,
        "dedup_groups_resolved": 0,
    }
    if any(src in sources for src in ("polygon_news", "finnhub_company_news")):
        if "polygon_news" in sources:
            from catalyst_data.rederive import rederive_polygon_news
            rows_changed["articles_upserted"] = rederive_polygon_news(db_path).get("articles_upserted", 0)
            from catalyst_data.regenerate_clean import regenerate_polygon_clean_assets
            rows_changed["clean_assets_inserted"] = regenerate_polygon_clean_assets(db_path).get("inserted_article_rows", 0)
        if "finnhub_company_news" in sources:
            from catalyst_data.pipeline.finnhub_normalize import rederive_finnhub_news
            rows_changed["finnhub_articles_upserted"] = rederive_finnhub_news(db_path).get("articles_upserted", 0)
        from catalyst_data.source_tier import classify_articles
        classify_articles(conn)
        from catalyst_data.dedup.cross_source import compute_cross_source_dedup
        rows_changed["dedup_groups_resolved"] = compute_cross_source_dedup(conn)

    from catalyst_data.index_builder import build_incremental_records
    inc = build_incremental_records(conn, min_l2_chars=800)
    index_state = {
        "delta_new": inc.get("new_article_count", 0),
        "delta_changed": inc.get("changed_article_count", 0),
        "would_embed": inc.get("would_embed_count", 0),
    }

    doctor_result = None
    if not config.skip_doctor:
        try:
            from catalyst_data.doctor import doctor
            doctor_result = doctor(db_path)
        except Exception as exc:
            doctor_result = {"invoked": True, "all_gates_passed": False, "error": str(exc)}

    ended = datetime.now(timezone.utc).isoformat()
    status = _final_status(cells_success, cells_failed, cells_skipped, run_id in _CANCEL_REQUESTS)
    close_ingestion_run(
        conn, run_id=run_id, success_count=cells_success,
        fail_count=cells_failed, status=status,
    )
    aggregates = _checkpoint_aggregates(conn, run_id)
    after_counts = _count_rows(conn)
    rows_changed["table_deltas"] = {
        k: after_counts.get(k, 0) - before_counts.get(k, 0)
        for k in sorted(set(before_counts) | set(after_counts))
    }

    report = RunReport(
        run_id=run_id,
        mode="resume" if config.resume_from else "update",
        resume_from=config.resume_from,
        config=_config_report_dict(config),
        started_at=started,
        ended_at=ended,
        elapsed_sec=round(time.monotonic() - start_time, 2),
        providers=aggregates["providers"],
        retry_histogram=aggregates["retry_histogram"],
        top_error_classes=aggregates["top_error_classes"],
        fallbacks=aggregates["fallbacks"],
        rows_changed=rows_changed,
        index_state=index_state,
        doctor=doctor_result,
    )
    path = save_run_report(report, report_dir=report_dir)
    report.report_path = path
    save_run_report(report, report_dir=report_dir)
    conn.execute(
        "UPDATE ingestion_runs SET report_path = ? WHERE run_id = ?",
        (path, run_id),
    )
    conn.commit()
    conn.close()
    _CANCEL_REQUESTS.discard(run_id)
    return report
