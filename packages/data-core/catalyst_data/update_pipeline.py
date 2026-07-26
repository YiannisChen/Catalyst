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
import httpx
import json
import logging
import sqlite3
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Awaitable

from catalyst_data.freshness import latest_local_ohlcv_date, freshness_report
from catalyst_data.ingestion.run_control import is_cancelled
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


class FatalTransportError(Exception):
    """Non-retryable transport configuration failure (ImportError, ModuleNotFoundError)."""


# ---- W1-C: OHLCV helpers ----

def _normalize_ohlcv(fr) -> dict | None:
    """Normalize a FetchResult from any OHLCV provider into an internal bar dict.

    Returns None when the provider returned no valid bar (empty valid).
    """
    raw = getattr(fr, 'data', None)
    if raw is None:
        return None
    # Polygon: results array with o/c/h/l/v fields
    if isinstance(raw, dict) and raw.get("results") is not None:
        results = raw["results"]
        if not results:
            return None
        r = results[0]
        try:
            return {
                "open": float(r["o"]), "high": float(r["h"]),
                "low": float(r["l"]), "close": float(r["c"]),
                "volume": float(r.get("v", r.get("vw", 0))),
            }
        except (KeyError, TypeError, ValueError):
            return None
    # yfinance: dict with Open/High/Low/Close/Volume
    if isinstance(raw, dict) and "Open" in raw:
        try:
            return {
                "open": float(raw["Open"]), "high": float(raw["High"]),
                "low": float(raw["Low"]), "close": float(raw["Close"]),
                "volume": float(raw["Volume"]),
            }
        except (KeyError, TypeError, ValueError):
            return None
    # Already normalized dict
    if isinstance(raw, dict) and "open" in raw:
        try:
            return {
                "open": float(raw["open"]), "high": float(raw["high"]),
                "low": float(raw["low"]), "close": float(raw["close"]),
                "volume": float(raw.get("volume", 0)),
            }
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _persist_ohlcv_success(
    conn, run_id, ticker, date, source, ohlcv_bar,
    ohlcv_source, retries, fr,
    fallback_provider=None,
):
    """Atomically persist OHLCV success: raw_asset + bar + checkpoint.

    Does NOT close conn — caller owns the connection.
    On failure, rolls back all business writes and returns status='failed'.
    """
    from catalyst_data.storage.sqlite import compute_asset_id, upsert_ohlcv as _upsert
    from catalyst_data.error_taxonomy import ErrorClass

    asset_id = compute_asset_id(ticker, date, source)
    fetched_at = datetime.now(timezone.utc).isoformat()
    fr_status = getattr(fr, 'status', 200)

    def _store_ohlcv(c):
        _upsert(c, symbol=ticker, date=date,
                open=ohlcv_bar["open"], high=ohlcv_bar["high"],
                low=ohlcv_bar["low"], close=ohlcv_bar["close"],
                volume=ohlcv_bar["volume"], source=ohlcv_source,
                commit=False)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id,ticker,source_type,reference_date,fetched_at,data_version,content_raw,http_status) "
            "VALUES (?,?,?,?,?,'v1',?,?)",
            (asset_id, ticker, source, date, fetched_at,
             zlib.compress(json.dumps(ohlcv_bar).encode()),
             fr_status),
        )
        _store_ohlcv(conn)
        from catalyst_data.quality import write_source_checkpoint
        write_source_checkpoint(
            conn, run_id=run_id, source_type=source,
            ticker=ticker, date=date, status="success",
            error_class=None, raw_asset_id=asset_id,
            fallback_provider=fallback_provider,
            fallback_triggered=1 if fallback_provider else 0,
            items_count=1, retries=retries, commit=False,
        )
        conn.commit()
        return {
            "ticker": ticker, "date": date, "source": source,
            "status": "success", "error": None,
            "ohlcv_bar": True, "asset_id": asset_id,
            "fallback_provider": fallback_provider,
        }
    except Exception as exc:
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        try:
            from catalyst_data.quality import write_source_checkpoint
            write_source_checkpoint(
                conn, run_id=run_id, source_type=source,
                ticker=ticker, date=date, status="failed",
                error_class=ErrorClass.UNKNOWN.value,
                error_message_redacted=str(exc)[:500],
                retries=retries, commit=False,
            )
            conn.commit()
        except Exception:
            conn.rollback()
        return {
            "ticker": ticker, "date": date, "source": source,
            "status": "failed", "error": str(exc),
            "ohlcv_bar": False,
        }


def _persist_ohlcv_empty(conn, run_id, ticker, date, source, retries, fr):
    """Persist EMPTY_VALID OHLCV result.  Caller owns conn."""
    from catalyst_data.storage.sqlite import compute_asset_id
    from catalyst_data.error_taxonomy import ErrorClass
    from catalyst_data.quality import write_source_checkpoint

    asset_id = compute_asset_id(ticker, date, source)
    fetched_at = datetime.now(timezone.utc).isoformat()
    fr_status = getattr(fr, 'status', 200)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO raw_assets "
            "(asset_id,ticker,source_type,reference_date,fetched_at,data_version,content_raw,http_status) "
            "VALUES (?,?,?,?,?,'v1',?,?)",
            (asset_id, ticker, source, date, fetched_at, b"", fr_status),
        )
        write_source_checkpoint(
            conn, run_id=run_id, source_type=source,
            ticker=ticker, date=date, status="success_empty",
            error_class=ErrorClass.EMPTY_VALID.value,
            empty_reason="no_bars_returned",
            items_count=0, raw_asset_id=asset_id,
            retries=retries, commit=False,
        )
        conn.commit()
        return {
            "ticker": ticker, "date": date, "source": source,
            "status": "success_empty", "error": None,
            "empty_reason": "no_bars_returned",
            "ohlcv_bar": False, "asset_id": asset_id,
        }
    except Exception as exc:
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        try:
            write_source_checkpoint(
                conn, run_id=run_id, source_type=source,
                ticker=ticker, date=date, status="failed",
                error_class=ErrorClass.UNKNOWN.value,
                error_message_redacted=str(exc)[:500],
                retries=retries, commit=False,
            )
            conn.commit()
        except Exception:
            conn.rollback()
        return {
            "ticker": ticker, "date": date, "source": source,
            "status": "failed", "error": str(exc),
            "ohlcv_bar": False,
        }


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


# ============================================================================
# B2 — execute_update entrypoint with PlanDriftError guarding
# ============================================================================

_B2_PROVIDER_BY_SOURCE = {
    "polygon_news": ("polygon", "news", "news"),
    "polygon_ohlcv": ("polygon", "ohlcv", "ohlcv"),
    "finnhub_company_news": ("finnhub", "company-news", "news"),
    "fmp_fundamentals": ("fmp", "income_statement", "fundamentals"),
    "fred_macro": ("fred", "series_observations", "macro"),
    "sec_filings": ("sec", "sec_submissions", "filings"),
    "yfinance_ohlcv": ("yfinance", "yfinance_ohlcv", "ohlcv"),
    "yfinance_fundamentals": ("yfinance", "yfinance_fundamentals", "fundamentals"),
}


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan_stage_cells(plan: "UpdatePlan", stage_name: str) -> list[tuple[str, str, str]]:
    stage = (plan.stages or {}).get(stage_name) or {}
    cells = []
    for cell in stage.get("cells", []):
        if isinstance(cell, dict):
            cells.append((cell["subject"], cell["window_start"], cell["source_type"]))
        else:
            cells.append(tuple(cell))
    return cells


def _plan_stage_cell_records(plan: "UpdatePlan", stage_name: str) -> list[dict[str, Any]]:
    stage = (plan.stages or {}).get(stage_name) or {}
    records = []
    for cell in stage.get("cells", []):
        if isinstance(cell, dict):
            records.append(cell)
        else:
            ticker, date, source = tuple(cell)
            records.append({
                "stage": stage_name if stage_name != "market" else "market",
                "source_type": source,
                "endpoint_name": source,
                "subject": ticker,
                "window_start": date,
                "window_end": date,
                "date_domain": "trading_sessions",
                "provider_profile_version": "v1",
                "page_cap": None,
                "item_cap": None,
                "cell_id": _b2_cell_id(ticker, date, source, stage_name),
            })
    return records


def _b2_cell_id(ticker: str, date: str, source: str, stage: str) -> str:
    payload = {
        "source_type": source,
        "endpoint_name": source,
        "ticker_or_series": ticker,
        "window_start": date,
        "window_end": date,
        "stage": stage,
        "provider_profile_version": "v1",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _ensure_b2_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    plan_hash: str,
    expected_plan_hash: str,
    allow_stale_ohlcv: bool,
    tickers: list[str] | None = None,
    sources: list[str] | None = None,
    parent_run_id: str | None = None,
) -> None:
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(ingestion_runs)").fetchall()
    }
    row_values: dict[str, Any] = {
        "run_id": run_id,
        "status": "PLANNED",
        "started_at": _utc_now_z(),
        "plan_hash": plan_hash,
        "expected_plan_hash": expected_plan_hash,
        "allow_stale_ohlcv": 1 if allow_stale_ohlcv else 0,
        "allow_stale_ohlcv_overridden": 1 if allow_stale_ohlcv else 0,
        "ticker_list_json": json.dumps(tickers or [], sort_keys=True),
        "source_list_json": json.dumps(sources or [], sort_keys=True),
        "success_count": 0,
        "fail_count": 0,
        "mode": "update",
        "parent_run_id": parent_run_id,
    }
    columns = [col for col in row_values if col in existing_cols]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO ingestion_runs ({', '.join(columns)}) VALUES ({placeholders})",
        [row_values[col] for col in columns],
    )
    conn.commit()


def _set_b2_run_status(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    if status in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}:
        conn.execute(
            "UPDATE ingestion_runs SET status = ?, ended_at = ? WHERE run_id = ?",
            (status, _utc_now_z(), run_id),
        )
    else:
        conn.execute(
            "UPDATE ingestion_runs SET status = ? WHERE run_id = ?",
            (status, run_id),
        )
    conn.commit()


def _fetch_result_payload(response: Any) -> tuple[int, Any, bytes]:
    if isinstance(response, FetchResult):
        status = int(response.status or 0)
        data = response.data if response.data is not None else {}
        if response.raw_body is not None:
            return status, data, response.raw_body
    elif isinstance(response, dict) and "body" in response:
        status = int(response.get("status") or response.get("status_code") or 200)
        body = response.get("body") or b"{}"
        raw = body if isinstance(body, bytes) else str(body).encode("utf-8")
        return status, json.loads(raw.decode("utf-8") or "{}"), raw
    elif isinstance(response, dict):
        status = int(response.get("status") or response.get("status_code") or 200)
        data = response
    else:
        status = int(getattr(response, "status_code", getattr(response, "status", 200)) or 200)
        data = response.json() if hasattr(response, "json") else {}
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return status, data, raw


async def _call_b2_transport(
    transport: Any,
    *,
    provider: str,
    cell: dict[str, Any],
    page_url: str | None = None,
) -> Any:
    if transport is None:
        raise ValueError("transport is required for B2 execute_update")
    subject = cell["subject"]
    source = cell["source_type"]
    endpoint = cell["endpoint_name"]
    if isinstance(transport, dict):
        fetcher = transport.get(source) or transport.get(endpoint) or transport.get(provider)
        if fetcher is None:
            raise ValueError(f"No fake transport registered for source {source}")
        return await fetcher(subject, endpoint, cell["window_start"], cell["window_end"])
    if hasattr(transport, "request"):
        return await transport.request(
            provider=provider,
            source_type=source,
            endpoint_name=endpoint,
            subject=subject,
            window_start=cell["window_start"],
            window_end=cell["window_end"],
            page_url=page_url,
            page_cap=cell.get("page_cap"),
            item_cap=cell.get("item_cap"),
        )
    url = f"https://b2.local/{provider}/{endpoint}"
    return await transport(
        provider,
        "GET",
        url,
        source=source,
        endpoint=endpoint,
        ticker=subject,
        date=cell["window_start"],
        window_start=cell["window_start"],
        window_end=cell["window_end"],
        params={"ticker": subject, "from": cell["window_start"], "to": cell["window_end"]},
    )


def _record_b2_entity(
    conn: sqlite3.Connection,
    *,
    source: str,
    ticker: str,
    date: str,
    data: Any,  # FMP returns list; other providers return dict
    raw_asset_id: str,
    endpoint_name: str | None = None,
    window_end: str | None = None,
) -> int:
    from catalyst_data.ingestion.provenance import record_provenance
    from catalyst_data.articles import compute_article_id, ensure_articles_table, upsert_article, upsert_article_ticker
    from catalyst_data.storage.sqlite import ensure_filings_tables, ensure_macro_tables, upsert_filing, upsert_filing_document, upsert_macro_observation

    if source in {"polygon_ohlcv", "yfinance_ohlcv"}:
        ohlcv_source = "yfinance" if source.startswith("yfinance") else "polygon"
        raw_bars = data.get("results") if source == "polygon_ohlcv" and isinstance(data, dict) else None
        if not isinstance(raw_bars, list):
            raw_bars = [data]
        count = 0
        rejected = 0
        for raw_bar in raw_bars:
            if not isinstance(raw_bar, dict):
                continue
            bar = _normalize_ohlcv(SimpleNamespace(
                data={"results": [raw_bar]} if source == "polygon_ohlcv" else raw_bar
            ))
            if bar is None:
                continue
            bar_date = date
            if source == "polygon_ohlcv" and raw_bar.get("t") is not None:
                try:
                    bar_date = datetime.fromtimestamp(
                        float(raw_bar["t"]) / 1000.0, tz=timezone.utc
                    ).date().isoformat()
                except (TypeError, ValueError, OSError):
                    continue
            if bar_date < date or (window_end is not None and bar_date > window_end):
                continue
            conn.execute(
                """INSERT OR REPLACE INTO ohlcv
                   (symbol, date, open, high, low, close, volume, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ticker, bar_date, bar["open"], bar["high"], bar["low"],
                    bar["close"], bar["volume"], ohlcv_source,
                ),
            )
            entity_id = f"{ticker}:{bar_date}:{ohlcv_source}"
            entity_version = hashlib.sha256(
                json.dumps(
                    {"entity_id": entity_id, **bar},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            record_provenance(
                conn, entity_type="ohlcv", entity_id=entity_id,
                entity_version=entity_version, raw_asset_id=raw_asset_id,
            )
            count += 1
        return {"items_written": count, "rejected_count": rejected}

    if source == "polygon_news":
        items = data.get("results", []) if isinstance(data, dict) else []
        count = 0
        rejected = 0
        _ensure_b2o_article_domain(conn)
        if isinstance(items, list):
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                published_utc = item.get("published_utc")
                if not isinstance(published_utc, str) or not published_utc.strip():
                    rejected += 1
                    continue
                try:
                    pub_dt = datetime.fromisoformat(published_utc.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    rejected += 1
                    continue
                if pub_dt.tzinfo is None or pub_dt.utcoffset() is None:
                    rejected += 1
                    continue
                # Convert to UTC for comparison
                pub_dt_utc = pub_dt.astimezone(timezone.utc)
                win_start_dt = datetime.fromisoformat(f"{date}T00:00:00+00:00")
                win_end_dt = datetime.fromisoformat(f"{window_end}T00:00:00+00:00") + timedelta(days=1)
                if pub_dt_utc < win_start_dt or pub_dt_utc >= win_end_dt:
                    rejected += 1
                    continue
                native_id = str(item.get("id") or item.get("article_url") or item.get("url") or idx)
                article_id = compute_article_id("poly", native_id)
                entity_version = hashlib.sha256(
                    json.dumps(item, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
                ).hexdigest()
                record_provenance(
                    conn, entity_type="article", entity_id=article_id,
                    entity_version=entity_version, raw_asset_id=raw_asset_id,
                )
                publisher = item.get("publisher") if isinstance(item.get("publisher"), dict) else {}
                upsert_article(conn, article={
                    "article_id": article_id,
                    "raw_asset_id": raw_asset_id,
                    "provider": "polygon",
                    "source_type": "polygon_news",
                    "ticker": ticker,
                    "reference_date": date,
                    "published_utc": item.get("published_utc") or f"{date}T00:00:00Z",
                    "title": item.get("title") or "Untitled",
                    "description": item.get("description"),
                    "article_url": item.get("article_url") or item.get("url"),
                    "publisher_name": publisher.get("name") or item.get("publisher_name"),
                    "tickers_json": json.dumps(item.get("tickers") or [ticker], separators=(",", ":")),
                })
                upsert_article_ticker(
                    conn,
                    article_id=article_id,
                    ticker=ticker,
                    raw_asset_id=raw_asset_id,
                    reference_date=date,
                )
                count += 1
        return {"items_written": count, "rejected_count": rejected}

    if source == "fmp_fundamentals":
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fundamental_statements'"
        ).fetchone() is not None
        statement_type = endpoint_name or (data.get("statement_type") if isinstance(data, dict) else None)
        if not table_exists or statement_type not in {"income_statement", "balance_sheet", "cash_flow"}:
            entity_id = f"{source}:{ticker}:{date}"
            entity_version = hashlib.sha256(
                json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            ).hexdigest()
            record_provenance(
                conn, entity_type="fundamental_snapshot", entity_id=entity_id,
                entity_version=entity_version, raw_asset_id=raw_asset_id,
            )
            return 1

        # ── payload extraction ──────────────────────────────────────────
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("statements") or data.get("data") or data.get("results") or []
        else:
            raise ValueError(
                f"FMP {statement_type}: expected list or dict, got {type(data).__name__}"
            )

        if isinstance(rows, dict):
            rows = [rows]

        if not isinstance(rows, list):
            raise ValueError(
                f"FMP {statement_type}: rows must be list, got {type(rows).__name__}"
            )

        total_input = len(rows)
        if total_input == 0:
            # Legally empty: no statement rows, return 0
            return 0

        # ── projection (atomic via SAVEPOINT) ──────────────────────────
        conn.execute("SAVEPOINT _fmp_normalize")
        try:
            count = 0
            valid_row_seen = False
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError(
                        f"FMP {statement_type}: non-dict row in list at position {count}"
                    )
                fiscal_date = row.get("date") or row.get("fiscal_date") or row.get("fillingDate")
                if not fiscal_date:
                    continue
                valid_row_seen = True
                fiscal_period = row.get("period") or row.get("fiscal_period")
                currency = row.get("reportedCurrency") or row.get("reported_currency")
                statement_identity = {
                    "provider": "fmp",
                    "ticker": ticker,
                    "statement_type": statement_type,
                    "fiscal_date": fiscal_date,
                    "fiscal_period": fiscal_period,
                    "reported_currency": currency,
                }
                statement_id = hashlib.sha256(
                    json.dumps(statement_identity, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
                ).hexdigest()
                payload_json = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
                conn.execute(
                    """INSERT OR IGNORE INTO fundamental_statements
                       (statement_id, raw_asset_id, provider, ticker, statement_type,
                        fiscal_date, fiscal_period, reported_currency, available_at,
                        payload_json, created_at)
                       VALUES (?, ?, 'fmp', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        statement_id, raw_asset_id, ticker, statement_type,
                        fiscal_date, fiscal_period, currency,
                        row.get("acceptedDate") or row.get("available_at") or date,
                        payload_json, _utc_now_z(),
                    ),
                )
                entity_version = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                record_provenance(
                    conn, entity_type="fundamental_snapshot", entity_id=statement_id,
                    entity_version=entity_version, raw_asset_id=raw_asset_id,
                )
                count += 1

            # Non-empty input but zero valid rows: fail, don't success_empty
            if not valid_row_seen:
                raise ValueError(
                    f"FMP {statement_type}: {total_input} rows, none had fiscal_date"
                )

            conn.execute("RELEASE _fmp_normalize")
        except Exception:
            conn.execute("ROLLBACK TO _fmp_normalize")
            conn.execute("RELEASE _fmp_normalize")
            raise

        return count

    if source == "sec_filings":
        checkpoint_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(source_checkpoints)")
        }
        entity_id = (
            None if "cell_id" in checkpoint_columns else f"sec:{ticker}:{date}"
        )
        entity_type = "filing"
    elif source == "fred_macro":
        entity_id = f"fred:{ticker}:{date}"
        entity_type = "macro_observation"
    elif source in {"fmp_fundamentals", "yfinance_fundamentals"}:
        entity_id = f"{source}:{ticker}:{date}"
        entity_type = "fundamental_snapshot"
    elif source == "finnhub_company_news":
        items = data if isinstance(data, list) else data.get("results", data.get("data", []))
        count = 0
        _ensure_b2o_article_domain(conn)
        if isinstance(items, list):
            for idx, item in enumerate(items):
                native_id = str(item.get("id") or item.get("url") or idx) if isinstance(item, dict) else str(idx)
                entity_id = f"finnhub:{native_id}"
                entity_version = hashlib.sha256(
                    json.dumps(item, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
                ).hexdigest()
                record_provenance(
                    conn, entity_type="article", entity_id=entity_id,
                    entity_version=entity_version, raw_asset_id=raw_asset_id,
                )
                published = item.get("datetime")
                if isinstance(published, (int, float)):
                    published_utc = datetime.fromtimestamp(published, tz=timezone.utc).isoformat()
                else:
                    published_utc = str(published or f"{date}T00:00:00+00:00")
                article_id = compute_article_id("finnhub", native_id)
                upsert_article(conn, article={
                    "article_id": article_id,
                    "raw_asset_id": raw_asset_id,
                    "provider": "finnhub",
                    "source_type": "finnhub_company_news",
                    "ticker": ticker,
                    "reference_date": date,
                    "published_utc": published_utc,
                    "title": item.get("headline") or item.get("title") or "Untitled",
                    "description": item.get("summary") or item.get("description"),
                    "article_url": item.get("url"),
                    "publisher_name": item.get("source"),
                    "tickers_json": json.dumps([ticker], separators=(",", ":")),
                })
                upsert_article_ticker(
                    conn,
                    article_id=article_id,
                    ticker=ticker,
                    raw_asset_id=raw_asset_id,
                    reference_date=date,
                )
                count += 1
        return count
    else:
        return 0

    if entity_id is not None:
        entity_version = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        record_provenance(
            conn, entity_type=entity_type, entity_id=entity_id,
            entity_version=entity_version, raw_asset_id=raw_asset_id,
        )
    if source == "sec_filings":
        ensure_filings_tables(conn)
        filings = _normalize_b2o_sec_filings(data, ticker)
        if not isinstance(filings, list):
            return 0
        count = 0
        for idx, filing in enumerate(filings):
            filing_id = filing.get("filing_id") or f"sec:{ticker}:{filing.get('accession_number', idx)}"
            upsert_filing(
                conn,
                filing_id=filing_id,
                cik=filing.get("cik") or "",
                ticker=ticker,
                form_type=filing.get("form_type") or "8-K",
                filed_at=filing.get("filed_at") or date,
                accession_number=filing.get("accession_number") or str(idx),
                url=filing.get("url") or filing.get("document_url") or "",
                period=filing.get("period"),
                primary_document=filing.get("primary_document"),
                items_json=filing.get("items_json"),
                is_rag_eligible=filing.get("is_rag_eligible", 1),
                raw_asset_id=raw_asset_id,
            )
            filing_version = hashlib.sha256(
                json.dumps(
                    filing, sort_keys=True, separators=(",", ":"), default=str
                ).encode("utf-8")
            ).hexdigest()
            record_provenance(
                conn,
                entity_type="filing",
                entity_id=filing_id,
                entity_version=filing_version,
                raw_asset_id=raw_asset_id,
            )
            if filing.get("document_url") or filing.get("text"):
                upsert_filing_document(
                    conn,
                    filing_id=filing_id,
                    document_url=filing.get("document_url") or filing.get("url") or f"memory:{filing_id}",
                    document_type=filing.get("document_type", "primary_doc"),
                    text=filing.get("text"),
                    char_len=len(filing.get("text") or ""),
                    extraction_status="success" if filing.get("text") else "empty",
                )
            count += 1
        return count
    if source == "fred_macro":
        ensure_macro_tables(conn)
        observations = data.get("observations", []) if isinstance(data, dict) else []
        series_id = data.get("id") or ticker
        count = 0
        for obs in observations:
            if not isinstance(obs, dict) or not obs.get("date"):
                continue
            raw_value = obs.get("value")
            value = None if raw_value in (None, ".") else float(raw_value)
            upsert_macro_observation(
                conn,
                series_id=series_id,
                observation_date=obs["date"],
                value=value,
                released_at=obs.get("realtime_start"),
                raw_asset_id=raw_asset_id,
            )
            count += 1
        return count
    return 1


def _normalize_b2o_sec_filings(data: dict[str, Any], ticker: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    filings_value = data.get("filings")
    if isinstance(filings_value, list):
        return [row for row in filings_value if isinstance(row, dict)]
    recent = filings_value.get("recent", {}) if isinstance(filings_value, dict) else {}
    if not isinstance(recent, dict):
        return []

    from catalyst_data.manifests.universe import load_universe_spec

    spec_path = (
        Path(__file__).resolve().parent
        / "manifests"
        / "universe_v1_2025_08.spec.json"
    )
    spec = load_universe_spec(spec_path)
    company = spec.companies.get(ticker, {})
    allowed_forms = set(company.get("filing_form_profile") or ())
    cik = str(data.get("cik") or company.get("cik") or "").zfill(10)
    forms = recent.get("form") or []
    filing_dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    primary_documents = recent.get("primaryDocument") or []
    report_dates = recent.get("reportDate") or []
    items_values = recent.get("items") or []
    normalized: list[dict[str, Any]] = []
    for idx, form in enumerate(forms):
        if form not in allowed_forms:
            continue
        accession = str(accessions[idx] if idx < len(accessions) else "")
        if not accession:
            continue
        primary_document = (
            primary_documents[idx] if idx < len(primary_documents) else ""
        )
        cik_path = cik.lstrip("0") or "0"
        accession_path = accession.replace("-", "")
        document_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_path}/"
            f"{accession_path}/{primary_document}"
            if primary_document
            else ""
        )
        items = str(items_values[idx] if idx < len(items_values) else "")
        normalized.append({
            "filing_id": f"sec:{cik}:{accession}",
            "cik": cik,
            "ticker": ticker,
            "form_type": form,
            "filed_at": filing_dates[idx] if idx < len(filing_dates) else "",
            "period": report_dates[idx] if idx < len(report_dates) else None,
            "accession_number": accession,
            "primary_document": primary_document or None,
            "url": document_url,
            "document_url": document_url,
            "items_json": json.dumps(
                [part.strip() for part in items.split(",") if part.strip()],
                separators=(",", ":"),
            ),
            "is_rag_eligible": 1,
        })
    return normalized


def _ensure_b2o_article_domain(conn: sqlite3.Connection) -> None:
    from catalyst_data.articles import ensure_articles_table

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
    ).fetchone()
    if not exists:
        ensure_articles_table(conn)
        return
    article_cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    additions = {
        "raw_asset_id": "TEXT",
        "provider": "TEXT DEFAULT 'polygon'",
        "source_type": "TEXT DEFAULT 'polygon_news'",
        "ticker": "TEXT DEFAULT ''",
        "reference_date": "TEXT DEFAULT ''",
        "publisher_name": "TEXT",
        "publisher_homepage_url": "TEXT",
        "publisher_logo_url": "TEXT",
        "publisher_favicon_url": "TEXT",
        "author": "TEXT",
        "keywords_json": "TEXT",
        "insights_json": "TEXT",
        "tickers_json": "TEXT",
        "source_tier": "INTEGER",
        "dedup_group_id": "TEXT",
        "is_canonical": "INTEGER DEFAULT 1",
        "is_rag_eligible": "INTEGER DEFAULT 1",
        "quality_score": "REAL DEFAULT 1.0",
        "created_at": "TEXT",
    }
    for col, spec in additions.items():
        if col not in article_cols:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {spec}")
    ticker_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_tickers'"
    ).fetchone()
    if not ticker_exists:
        conn.execute(
            """CREATE TABLE article_tickers (
                article_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                raw_asset_id TEXT,
                reference_date TEXT,
                dedup_group_id TEXT,
                is_canonical INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (article_id, ticker)
            )"""
        )
    else:
        ticker_cols = {row[1] for row in conn.execute("PRAGMA table_info(article_tickers)").fetchall()}
        for col, spec in {
            "raw_asset_id": "TEXT",
            "reference_date": "TEXT",
            "dedup_group_id": "TEXT",
            "is_canonical": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            if col not in ticker_cols:
                conn.execute(f"ALTER TABLE article_tickers ADD COLUMN {col} {spec}")
    conn.commit()


def _write_b2_checkpoint(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source: str,
    ticker: str,
    date: str,
    status: str,
    logical_fetch_id: str,
    items_count: int,
    http_status: int | None,
    is_complete: int,
    raw_asset_id: str | None = None,
    cell: dict[str, Any] | None = None,
    request_count: int = 1,
    pages_received: int = 1,
    error_class: str | None = None,
) -> None:
    checkpoint_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(source_checkpoints)").fetchall()
    }
    has_full_identity = {
        "cell_id", "window_start", "window_end", "endpoint_name", "provider_profile_version",
    }.issubset(checkpoint_cols)
    if cell is not None and cell.get("cell_id") and has_full_identity:
        conn.execute(
            """INSERT OR REPLACE INTO source_checkpoints
               (run_id, source_type, ticker, date, status, logical_fetch_id,
                request_count, pages_received, items_received, is_complete,
                raw_asset_id, items_count, http_status, error_class, cell_id, window_start,
                window_end, endpoint_name, provider_profile_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, source, ticker, date, status, logical_fetch_id,
                request_count, pages_received, items_count, is_complete,
                raw_asset_id, items_count, http_status, error_class,
                cell["cell_id"], cell["window_start"], cell["window_end"],
                cell["endpoint_name"], cell.get("provider_profile_version", "v1"),
            ),
        )
    else:
        conn.execute(
            """INSERT OR REPLACE INTO source_checkpoints
               (run_id, source_type, ticker, date, status, logical_fetch_id,
                request_count, pages_received, items_received, is_complete,
                raw_asset_id, items_count, http_status, error_class)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, source, ticker, date, status, logical_fetch_id,
                request_count, pages_received, items_count, is_complete,
                raw_asset_id, items_count, http_status, error_class,
            ),
        )
    conn.commit()


async def _request_b2_page(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    logical_fetch_id: str,
    provider: str,
    request_source_type: str,
    source: str,
    endpoint: str,
    ticker: str,
    date: str,
    cell: dict[str, Any],
    page_no: int,
    page_url: str | None,
    parent_request_id: str | None,
    transport: Any,
) -> dict[str, Any]:
    from catalyst_data.ingestion.redaction import (
        compute_cursor_fingerprint,
        compute_request_fingerprint,
        redact_request,
    )
    from catalyst_data.ingestion.request_ledger import insert_attempt, transition_attempt
    from catalyst_data.ingestion.raw_store import store_raw_response
    from catalyst_data.retry import _compute_rule_delay, get_retry_policy

    retry_sleep = getattr(transport, "retry_sleep", None)
    retry_policy = get_retry_policy(provider)
    ledger_attempt_no = 0
    transport_attempts = 0
    rate_limit_attempts = 0
    server_error_attempts = 0
    while True:
        ledger_attempt_no += 1
        request_id = hashlib.sha256(
            f"{run_id}:{cell['cell_id']}:{ledger_attempt_no}:{page_no}".encode("utf-8")
        ).hexdigest()
        url = page_url or f"https://b2.local/{provider}/{endpoint}"
        redacted = redact_request({
            "method": "GET",
            "url": url,
            "params": {
                "ticker": ticker,
                "window_start": cell["window_start"],
                "window_end": cell["window_end"],
                "page_no": page_no,
            },
            "provider_profile_version": cell.get("provider_profile_version", "v1"),
        })
        insert_attempt(conn, {
            "request_id": request_id,
            "run_id": run_id,
            "logical_fetch_id": logical_fetch_id,
            "source_type": request_source_type,
            "provider": provider,
            "endpoint_name": endpoint,
            "ticker_or_series": ticker,
            "window_start": cell["window_start"],
            "window_end": cell["window_end"],
            "attempt_no": ledger_attempt_no,
            "page_no": page_no,
            "parent_request_id": parent_request_id,
            "request_fingerprint": compute_request_fingerprint(redacted),
            "request_params_redacted": json.dumps(
                redacted["sorted_redacted_params"],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "cursor_fingerprint": (
                compute_cursor_fingerprint(page_url) if page_url else None
            ),
            "started_at": _utc_now_z(),
            "completed_at": None,
            "status": "STARTED",
        })
        conn.commit()

        try:
            response = await _call_b2_transport(
                transport,
                provider=provider,
                cell={**cell, "endpoint_name": endpoint},
                page_url=page_url,
            )
            status_code, data, raw_body = _fetch_result_payload(response)
        except (ImportError, ModuleNotFoundError) as exc:
            transition_attempt(
                conn,
                request_id,
                "TRANSPORT_ERROR",
                error_class="fatal_transport_configuration",
                error_message_redacted="transport configuration error",
            )
            conn.commit()
            raise FatalTransportError("transport configuration unavailable") from exc
        except FatalTransportError:
            raise
        except (ConnectionError, TimeoutError, httpx.TransportError) as exc:
            transport_attempts += 1
            transition_attempt(
                conn,
                request_id,
                "TRANSPORT_ERROR",
                error_class="transport_error",
                error_message_redacted=f"transport exception: {type(exc).__name__}",
            )
            conn.commit()
            rule = retry_policy.timeout
            if callable(retry_sleep) and transport_attempts < rule.max_retries:
                await retry_sleep(_compute_rule_delay(rule, transport_attempts))
                continue
            return {
                "ok": False,
                "attempt_count": ledger_attempt_no,
                "request_id": request_id,
                "status_code": None,
                "raw_asset_id": None,
                "page_error_class": "transport_error",
            }
        except Exception as exc:
            transition_attempt(
                conn,
                request_id,
                "TRANSPORT_ERROR",
                error_class="fatal_transport_configuration",
                error_message_redacted=f"non-transient exception: {type(exc).__name__}",
            )
            conn.commit()
            raise FatalTransportError(f"non-transient exception: {type(exc).__name__}") from exc

        stored_http_status = status_code if 100 <= status_code <= 599 else None
        raw_asset_id = store_raw_response(
            conn,
            request_id=request_id,
            response_bytes=raw_body,
            content_encoding="identity",
            page_no=page_no,
            ticker=ticker,
            reference_date=date,
            source_type=source,
            http_status=stored_http_status,
        )
        response_sha256 = hashlib.sha256(raw_body).hexdigest()
        if 200 <= status_code < 300:
            return {
                "ok": True,
                "attempt_count": ledger_attempt_no,
                "request_id": request_id,
                "status_code": status_code,
                "data": data,
                "raw_body": raw_body,
                "raw_asset_id": raw_asset_id,
                "response_sha256": response_sha256,
            }

        if status_code == 429:
            ledger_status = "RATE_LIMITED"
            rule = retry_policy.rate_limit
            rate_limit_attempts += 1
            retry_count = rate_limit_attempts
        elif status_code in {500, 502, 503, 504}:
            ledger_status = "HTTP_ERROR"
            rule = retry_policy.server_error
            server_error_attempts += 1
            retry_count = server_error_attempts
        elif status_code == 0:
            ledger_status = "TIMEOUT"
            rule = retry_policy.timeout
            transport_attempts += 1
            retry_count = transport_attempts
        else:
            ledger_status = (
                "AUTH_ERROR" if status_code in {401, 403} else "HTTP_ERROR"
            )
            rule = None
            retry_count = 0
        retry_after = (
            response.retry_after_seconds
            if isinstance(response, FetchResult)
            else None
        )
        transition_attempt(
            conn,
            request_id,
            ledger_status,
            http_status=stored_http_status,
            items_count=0,
            raw_asset_id=raw_asset_id,
            response_sha256=response_sha256,
            response_bytes=len(raw_body),
            retry_after_seconds=retry_after,
            error_message_redacted=f"HTTP {status_code}",
        )
        conn.commit()
        if (
            rule is not None
            and callable(retry_sleep)
            and retry_count < rule.max_retries
        ):
            await retry_sleep(
                _compute_rule_delay(
                    rule,
                    retry_count,
                    retry_after=retry_after,
                )
            )
            continue
        return {
            "ok": False,
            "attempt_count": ledger_attempt_no,
            "request_id": request_id,
            "status_code": status_code,
            "raw_asset_id": raw_asset_id,
            "page_error_class": ledger_status.lower() if ledger_status else "http_error",
        }


async def _execute_b2_cell(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    cell: dict[str, Any] | None = None,
    ticker: str | None = None,
    date: str | None = None,
    source: str | None = None,
    stage: str | None = None,
    transport: Any,
) -> dict[str, Any]:
    from catalyst_data.ingestion.request_ledger import (
        compute_logical_fetch_id,
        transition_attempt,
    )

    if cell is None:
        assert ticker is not None and date is not None and source is not None
        endpoint_default = _B2_PROVIDER_BY_SOURCE.get(
            source, (source.split("_", 1)[0], source, source)
        )[1]
        cell = {
            "stage": stage or "evidence",
            "source_type": source,
            "endpoint_name": endpoint_default,
            "subject": ticker,
            "window_start": date,
            "window_end": date,
            "date_domain": "calendar_days",
            "provider_profile_version": "v1",
            "page_cap": None,
            "item_cap": None,
            "cell_id": _b2_cell_id(ticker, date, source, stage or "evidence"),
        }
    source = cell["source_type"]
    ticker = cell["subject"]
    date = cell["window_start"]
    provider, default_endpoint, request_source_type = _B2_PROVIDER_BY_SOURCE.get(
        source, (source.split("_", 1)[0], source, source)
    )
    endpoint = cell.get("endpoint_name") or default_endpoint
    cell_id = cell["cell_id"]
    logical_fetch_id = compute_logical_fetch_id(run_id, cell_id)

    page_limit = int(cell.get("page_cap") or 1) if source == "polygon_news" else 1
    item_limit = cell.get("item_cap")
    item_limit = int(item_limit) if item_limit is not None else None
    page_no = 0
    page_url: str | None = None
    parent_request_id: str | None = None
    seen_page_urls: set[str] = set()
    total_items = 0
    last_raw_asset_id: str | None = None
    last_http_status: int | None = None
    incomplete_due_to_cap = False
    request_count_total = 0
    pages_received = 0

    cell_error_class: str | None = None
    while page_no < page_limit:
        page_no += 1
        if page_url:
            if page_url in seen_page_urls:
                _write_b2_checkpoint(
                    conn,
                    run_id=run_id,
                    source=source,
                    ticker=ticker,
                    date=date,
                    status="partial",
                    logical_fetch_id=logical_fetch_id,
                    items_count=total_items,
                    http_status=last_http_status,
                    is_complete=0,
                    raw_asset_id=last_raw_asset_id,
                    error_class="pagination_loop",
                    cell={**cell, "endpoint_name": endpoint},
                    request_count=request_count_total,
                    pages_received=pages_received,
                )
                return {
                    "ticker": ticker,
                    "date": date,
                    "window_start": cell["window_start"],
                    "window_end": cell["window_end"],
                    "cell_id": cell_id,
                    "endpoint_name": endpoint,
                    "source": source,
                    "status": "partial",
                    "items_count": total_items,
                    "raw_asset_id": last_raw_asset_id,
                    "error_class": "pagination_loop",
                }
            seen_page_urls.add(page_url)
        page_result = await _request_b2_page(
            conn,
            run_id=run_id,
            logical_fetch_id=logical_fetch_id,
            provider=provider,
            request_source_type=request_source_type,
            source=source,
            endpoint=endpoint,
            ticker=ticker,
            date=date,
            cell=cell,
            page_no=page_no,
            page_url=page_url,
            parent_request_id=parent_request_id,
            transport=transport,
        )
        request_count_total += page_result["attempt_count"]
        if not page_result["ok"]:
            last_raw_asset_id = (
                page_result["raw_asset_id"] or last_raw_asset_id
            )
            cell_error_class = page_result.get("page_error_class", "transport_error")
            _write_b2_checkpoint(
                conn,
                run_id=run_id,
                source=source,
                ticker=ticker,
                date=date,
                status="failed",
                logical_fetch_id=logical_fetch_id,
                items_count=total_items,
                http_status=page_result["status_code"],
                is_complete=0,
                raw_asset_id=last_raw_asset_id,
                error_class=cell_error_class,
                cell={**cell, "endpoint_name": endpoint},
                request_count=request_count_total,
                pages_received=pages_received,
            )
            return {
                "ticker": ticker,
                "date": date,
                "source": source,
                "status": "failed",
                "items_count": total_items,
                "raw_asset_id": last_raw_asset_id,
                "error_class": cell_error_class,
            }

        request_id = page_result["request_id"]
        status_code = page_result["status_code"]
        data = page_result["data"]
        raw_body = page_result["raw_body"]
        raw_asset_id = page_result["raw_asset_id"]
        response_sha256 = page_result["response_sha256"]
        last_http_status = status_code
        last_raw_asset_id = raw_asset_id
        pages_received += 1

        page_data = data
        if source == "polygon_news" and isinstance(data, dict):
            raw_items = data.get("results")
            if isinstance(raw_items, list) and item_limit is not None:
                remaining = max(item_limit - total_items, 0)
                if len(raw_items) > remaining:
                    page_data = {**data, "results": raw_items[:remaining]}
                    incomplete_due_to_cap = True
        elif source == "finnhub_company_news" and isinstance(data, list) and item_limit is not None:
            remaining = max(item_limit - total_items, 0)
            if len(data) > remaining:
                page_data = data[:remaining]
                incomplete_due_to_cap = True
        try:
            entity_result = _record_b2_entity(
                conn,
                source=source,
                ticker=ticker,
                date=date,
                data=page_data,
                raw_asset_id=raw_asset_id,
                endpoint_name=endpoint,
                window_end=cell["window_end"],
            )
        except ValueError as norm_err:
            transition_attempt(
                conn,
                request_id,
                "PARSE_ERROR",
                http_status=status_code,
                items_count=0,
                raw_asset_id=raw_asset_id,
                response_sha256=response_sha256,
                response_bytes=len(raw_body),
            )
            conn.commit()
            _write_b2_checkpoint(
                conn,
                run_id=run_id,
                source=source,
                ticker=ticker,
                date=date,
                status="failed",
                logical_fetch_id=logical_fetch_id,
                items_count=0,
                http_status=status_code,
                is_complete=0,
                raw_asset_id=raw_asset_id,
                error_class="normalization_error",
                cell={**cell, "endpoint_name": endpoint},
                request_count=request_count_total,
                pages_received=pages_received,
            )
            return {
                "ticker": ticker,
                "date": date,
                "source": source,
                "status": "failed",
                "items_count": 0,
                "raw_asset_id": raw_asset_id,
                "error_class": "normalization_error",
            }
        if isinstance(entity_result, dict):
            page_items = entity_result["items_written"]
            if entity_result.get("rejected_count", 0) > 0:
                cell_error_class = "response_out_of_window"
        else:
            page_items = entity_result
        total_items += page_items
        transition_attempt(
            conn,
            request_id,
            "SUCCEEDED",
            http_status=status_code,
            items_count=page_items,
            raw_asset_id=raw_asset_id,
            response_sha256=response_sha256,
            response_bytes=len(raw_body),
        )
        conn.commit()

        # Stop immediately if items were rejected (out of window)
        if cell_error_class:
            break

        next_url = (
            data.get("next_url")
            if source == "polygon_news" and isinstance(data, dict)
            else None
        )
        if item_limit is not None and total_items >= item_limit and next_url:
            incomplete_due_to_cap = True
        if not next_url or incomplete_due_to_cap:
            break
        parent_request_id = request_id
        page_url = str(next_url)

    if source == "polygon_news" and page_no >= page_limit:
        next_url = (
            data.get("next_url")
            if isinstance(data, dict)
            else None
        )
        incomplete_due_to_cap = incomplete_due_to_cap or bool(next_url)

    cp_status = (
        "partial"
        if incomplete_due_to_cap or cell_error_class
        else ("success_empty" if total_items == 0 else "success")
    )
    _write_b2_checkpoint(
        conn,
        run_id=run_id,
        source=source,
        ticker=ticker,
        date=date,
        status=cp_status,
        logical_fetch_id=logical_fetch_id,
        items_count=total_items,
        http_status=last_http_status,
        is_complete=0 if (incomplete_due_to_cap or cell_error_class) else 1,
        raw_asset_id=last_raw_asset_id,
        error_class=cell_error_class,
        cell={**cell, "endpoint_name": endpoint},
        request_count=request_count_total,
        pages_received=pages_received,
    )
    return {
        "ticker": ticker,
        "date": date,
        "window_start": cell["window_start"],
        "window_end": cell["window_end"],
        "cell_id": cell_id,
        "endpoint_name": endpoint,
        "source": source,
        "status": cp_status,
        "items_count": total_items,
        "raw_asset_id": last_raw_asset_id,
        "error_class": cell_error_class,
    }


def _latest_ohlcv_watermark_for_plan(conn: sqlite3.Connection, plan: "UpdatePlan") -> str | None:
    tickers = (plan.universe or {}).get("tickers") or (plan.config or {}).get("tickers") or []
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        row = conn.execute(
            f"SELECT MAX(date) FROM ohlcv WHERE symbol IN ({placeholders})",
            list(tickers),
        ).fetchone()
    else:
        row = conn.execute("SELECT MAX(date) FROM ohlcv").fetchone()
    return row[0] if row else None


async def execute_update(
    *,
    db: "sqlite3.Connection",
    plan: "UpdatePlan",
    transport: "Any" = None,
    parent_run_id: str | None = None,
    max_runtime_seconds: float | None = None,
) -> dict:
    """B2 execution entrypoint with two-stage OHLCV-first orchestration."""

    if max_runtime_seconds is not None and max_runtime_seconds < 0:
        raise ValueError("max_runtime_seconds must be non-negative")

    # Plan drift check — before any write or network call
    from catalyst_data.update_planner import _check_plan_drift
    _check_plan_drift(plan)

    run_id = f"b2-{uuid.uuid4().hex}"
    plan_hash = plan.plan_hash
    expected_plan_hash = plan.expected_plan_hash or plan_hash
    if parent_run_id is not None:
        _validate_b2_resume_lineage(db, parent_run_id, plan_hash, expected_plan_hash)
    allow_stale_ohlcv = bool((plan.config or {}).get("allow_stale_ohlcv", False))
    _ensure_b2_run(
        db, run_id=run_id, plan_hash=plan_hash,
        expected_plan_hash=expected_plan_hash,
        allow_stale_ohlcv=allow_stale_ohlcv,
        tickers=(plan.universe or {}).get("tickers") or (plan.config or {}).get("tickers") or [],
        sources=(plan.config or {}).get("sources") or [],
        parent_run_id=parent_run_id,
    )

    per_cell: list[dict[str, Any]] = []
    cells_success = 0
    cells_failed = 0
    cells_skipped = 0
    started = time.monotonic()
    evidence_replan: dict[str, Any] = {}
    stopped_for_runtime_budget = False
    consecutive_transport_failures = 0
    B2_TRANSPORT_CIRCUIT_THRESHOLD = 3
    stopped_for_circuit = False
    stopped_for_cancel = False
    stopped_for_fatal = False
    stopped_for_market_incomplete = False
    stop_reason: str | None = None
    missing_cell_ids: list[str] = []

    def runtime_budget_exhausted() -> bool:
        return (
            max_runtime_seconds is not None
            and time.monotonic() - started >= max_runtime_seconds
        )

    try:
        _set_b2_run_status(db, run_id, "RUNNING_OHLCV")
        for cell in _remaining_b2_cells(db, plan, "market", parent_run_id):
            if is_cancelled(db, run_id):
                stopped_for_cancel = True
                break
            if runtime_budget_exhausted():
                stopped_for_runtime_budget = True
                break
            if consecutive_transport_failures >= B2_TRANSPORT_CIRCUIT_THRESHOLD:
                stopped_for_circuit = True
                break
            result = await _execute_b2_cell(
                db, run_id=run_id, cell=cell, transport=transport,
            )
            per_cell.append(result)
            if result.get("status") in {"success", "success_empty"}:
                cells_success += 1
                consecutive_transport_failures = 0
            else:
                cells_failed += 1
                err_class = result.get("error_class", "")
                if err_class in ("transport_error", "timeout"):
                    consecutive_transport_failures += 1
                else:
                    consecutive_transport_failures = 0
            # Circuit check immediately after cell result
            if consecutive_transport_failures >= B2_TRANSPORT_CIRCUIT_THRESHOLD:
                stopped_for_circuit = True
                break
        db.commit()

        evidence_replan = {
            "latest_ohlcv_watermark": _latest_ohlcv_watermark_for_plan(db, plan),
            "plan_hash": plan_hash,
        }

        # Market completeness gate: verify all market cells are terminal-complete
        # Uses _remaining_b2_cells which checks lineage INCLUDING this run's checkpoints
        if not stopped_for_runtime_budget and not stopped_for_cancel and not stopped_for_circuit:
            all_market_cells = _plan_stage_cell_records(plan, "market")
            lineage = _lineage_run_ids(db, parent_run_id) + [run_id]
            # Check if cell_id column exists (added in migration v11)
            cols = {row[1] for row in db.execute("PRAGMA table_info(source_checkpoints)").fetchall()}
            has_cell_id = "cell_id" in cols
            _missing_market = []
            for cell in all_market_cells:
                cid = cell.get("cell_id")
                if cid and has_cell_id:
                    placeholders = ",".join("?" for _ in lineage)
                    row = db.execute(
                        f"SELECT status, COALESCE(is_complete,0) FROM source_checkpoints WHERE cell_id=? AND run_id IN ({placeholders}) ORDER BY rowid DESC LIMIT 1",
                        [cid] + lineage,
                    ).fetchone()
                    if not row or row[0] not in ("success", "success_empty") or not row[1]:
                        _missing_market.append(cell)
                elif not has_cell_id:
                    # Fallback: use source_type/ticker/date query for pre-v11 DBs
                    placeholders = ",".join("?" for _ in lineage)
                    row = db.execute(
                        f"SELECT status, COALESCE(is_complete,0) FROM source_checkpoints WHERE source_type=? AND ticker=? AND date=? AND run_id IN ({placeholders}) ORDER BY rowid DESC LIMIT 1",
                        [cell.get("source_type", ""), cell.get("subject", ""), cell.get("window_start", "")] + lineage,
                    ).fetchone()
                    if not row or row[0] not in ("success", "success_empty") or not row[1]:
                        _missing_market.append(cell)
            missing_cell_ids = [c.get("cell_id", "") for c in _missing_market]
            if _missing_market and not stopped_for_market_incomplete:
                stopped_for_market_incomplete = True
        if runtime_budget_exhausted():
            stopped_for_runtime_budget = True
        if not stopped_for_runtime_budget and not stopped_for_cancel and not stopped_for_circuit and not stopped_for_fatal and not stopped_for_market_incomplete:
            if is_cancelled(db, run_id):
                stopped_for_cancel = True
        if not stopped_for_runtime_budget and not stopped_for_cancel and not stopped_for_circuit and not stopped_for_fatal and not stopped_for_market_incomplete:
            _set_b2_run_status(db, run_id, "RUNNING_EVIDENCE")
            for cell in _remaining_b2_cells(db, plan, "evidence", parent_run_id):
                if is_cancelled(db, run_id):
                    stopped_for_cancel = True
                    break
                if runtime_budget_exhausted():
                    stopped_for_runtime_budget = True
                    break
                if consecutive_transport_failures >= B2_TRANSPORT_CIRCUIT_THRESHOLD:
                    stopped_for_circuit = True
                    break
                result = await _execute_b2_cell(
                    db, run_id=run_id, cell=cell, transport=transport,
                )
                per_cell.append(result)
                if result.get("status") in {"success", "success_empty"}:
                    cells_success += 1
                    consecutive_transport_failures = 0
                else:
                    cells_failed += 1
                    err_class = result.get("error_class", "")
                    if err_class in ("transport_error", "timeout"):
                        consecutive_transport_failures += 1
                    else:
                        consecutive_transport_failures = 0
                # Circuit check immediately after cell result
                if consecutive_transport_failures >= B2_TRANSPORT_CIRCUIT_THRESHOLD:
                    stopped_for_circuit = True
                    break

        # Centralized stop-state resolution
        # Priority: fatal > circuit > cancel > runtime > market_incomplete > normal
        if stopped_for_fatal:
            stop_reason = "fatal_transport_configuration"
            final = "FAILED"
        elif stopped_for_circuit:
            stop_reason = "transport_circuit_open"
            final = "PARTIAL" if cells_success else "FAILED"
        elif stopped_for_cancel:
            stop_reason = "operator_cancelled"
            final = "CANCELLED"
        elif stopped_for_runtime_budget:
            stop_reason = "runtime_budget_exhausted"
            final = "CANCELLED"
        elif stopped_for_market_incomplete:
            stop_reason = "market_stage_incomplete"
            final = "PARTIAL" if cells_success else "FAILED"
        else:
            final = "PARTIAL" if cells_failed and cells_success else ("FAILED" if cells_failed else "SUCCEEDED")
        _set_b2_run_status(db, run_id, final)
    except FatalTransportError:
        stopped_for_fatal = True
        cells_failed = 1
        final = "FAILED"
        stop_reason = "fatal_transport_configuration"
        # Collect all incomplete market cells using lineage: current run + ancestors
        all_market = _plan_stage_cell_records(plan, "market")
        lineage = _lineage_run_ids(db, parent_run_id) + [run_id]
        missing_cell_ids = []
        if lineage:
            for cell in all_market:
                cid = cell.get("cell_id")
                if cid:
                    placeholders = ",".join("?" for _ in lineage)
                    row = db.execute(
                        f"SELECT status, COALESCE(is_complete,0) FROM source_checkpoints WHERE cell_id=? AND run_id IN ({placeholders}) ORDER BY rowid DESC LIMIT 1",
                        [cid] + lineage,
                    ).fetchone()
                    if not row or row[0] not in ("success", "success_empty") or not row[1]:
                        missing_cell_ids.append(cid)
        else:
            missing_cell_ids = [c.get("cell_id", "") for c in all_market]
        try:
            _set_b2_run_status(db, run_id, "FAILED")
        except Exception:
            db.rollback()
    except Exception:
        try:
            _set_b2_run_status(db, run_id, "FAILED")
        except Exception:
            db.rollback()
        raise

    elapsed = round(time.monotonic() - started, 2)
    all_cells = _plan_stage_cell_records(plan, "market") + _plan_stage_cell_records(plan, "evidence")
    remaining_after_skip = _remaining_b2_cells(db, plan, "market", parent_run_id) + _remaining_b2_cells(db, plan, "evidence", parent_run_id)
    if parent_run_id is not None:
        cells_skipped = len(all_cells) - len(remaining_after_skip)
    total_cells = len(per_cell)
    report = {
        "run_id": run_id,
        "mode": "update",
        "status": final,
        "stage_sequence": ["ohlcv", "evidence"],
        "stop_reason": stop_reason,
        "plan_hash": plan_hash,
        "expected_plan_hash": expected_plan_hash,
        "allow_stale_ohlcv_overridden": allow_stale_ohlcv,
        "evidence_replan": evidence_replan,
        "cells_total": total_cells,
        "cells_success": cells_success,
        "cells_failed": cells_failed,
        "cells_skipped": cells_skipped,
        "missing_cells": missing_cell_ids,
        "articles_upserted": db.execute(
            "SELECT COUNT(*) FROM articles"
        ).fetchone()[0],
        "clean_assets_inserted": 0,
        "index_delta_new": 0,
        "index_delta_changed": 0,
        "index_would_embed": 0,
        "elapsed_sec": elapsed,
        "freshness_before": {},
        "freshness_after": {},
        "per_cell_report": per_cell,
    }
    return report


def _validate_b2_resume_lineage(
    conn: sqlite3.Connection,
    parent_run_id: str,
    plan_hash: str,
    expected_plan_hash: str,
) -> None:
    seen: set[str] = set()
    current = parent_run_id
    while current:
        if current in seen:
            raise ValueError("cycle in B2 resume lineage")
        seen.add(current)
        row = conn.execute(
            "SELECT plan_hash, expected_plan_hash, parent_run_id FROM ingestion_runs WHERE run_id = ?",
            (current,),
        ).fetchone()
        if row is None:
            raise ValueError(f"parent run not found: {current}")
        if row[0] != plan_hash or row[1] != expected_plan_hash:
            raise ValueError("resume lineage plan hash drift")
        current = row[2]


def _lineage_run_ids(conn: sqlite3.Connection, parent_run_id: str | None) -> list[str]:
    if not parent_run_id:
        return []
    out: list[str] = []
    current = parent_run_id
    while current:
        out.append(current)
        row = conn.execute("SELECT parent_run_id FROM ingestion_runs WHERE run_id = ?", (current,)).fetchone()
        current = row[0] if row else None
    return out


def _remaining_b2_cells(
    conn: sqlite3.Connection,
    plan: "UpdatePlan",
    stage_name: str,
    parent_run_id: str | None,
) -> list[dict[str, Any]]:
    cells = _plan_stage_cell_records(plan, stage_name)
    lineage = _lineage_run_ids(conn, parent_run_id)
    if not lineage:
        return cells
    placeholders = ",".join("?" for _ in lineage)
    checkpoint_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(source_checkpoints)").fetchall()
    }
    has_cell_id = "cell_id" in checkpoint_cols
    remaining = []
    for cell in cells:
        if cell.get("cell_id") and has_cell_id:
            row = conn.execute(
                f"""SELECT status, COALESCE(is_complete, 0) FROM source_checkpoints
                    WHERE run_id IN ({placeholders}) AND cell_id = ?
                    ORDER BY rowid DESC LIMIT 1""",
                lineage + [cell["cell_id"]],
            ).fetchone()
        else:
            row = conn.execute(
                f"""SELECT status, COALESCE(is_complete, 0) FROM source_checkpoints
                    WHERE run_id IN ({placeholders}) AND source_type = ? AND ticker = ? AND date = ?
                    ORDER BY rowid DESC LIMIT 1""",
                lineage + [cell["source_type"], cell["subject"], cell["window_start"]],
            ).fetchone()
        if row and row[0] in {"success", "success_empty"} and bool(row[1]):
            continue
        remaining.append(cell)
    return remaining


async def execute_update_v2(
    *,
    db: "sqlite3.Connection",
    plan: "UpdatePlan",
    transport: "Any" = None,
) -> dict:
    """Compatibility alias for the single authoritative B2 executor."""
    return await execute_update(db=db, plan=plan, transport=transport)



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

    # ── OHLCV path (W1-C) ──
    if source == "polygon_ohlcv":
        from catalyst_data.error_taxonomy import ErrorClass, classify_fetch_error
        from catalyst_data.fallback import FallbackPolicy

        TERMINAL_ERRORS = {
            ErrorClass.AUTH.value, ErrorClass.PERMISSION_PAID.value,
            ErrorClass.RATE_LIMIT.value, ErrorClass.BUDGET_EXHAUSTED.value,
        }

        # ── Resolve fetch callables from fetch_fn/fetch_map ──
        primary_fn = _cell_fetcher(fetch_fn, "polygon_ohlcv") if fetch_fn else None
        fallback_fn = _cell_fetcher(fetch_fn, "yfinance_ohlcv") if fetch_fn else None

        # ── Primary fetch ──
        primary_result = None
        primary_error = None
        primary_ec = None
        if primary_fn:
            try:
                primary_result = await primary_fn(ticker, "ohlcv", date)
            except Exception as exc:
                primary_error = str(exc)
                primary_ec = classify_fetch_error(
                    None, error_message=primary_error,
                    exception_type=type(exc).__name__,
                )

        # ── Primary success ──
        if primary_result is not None and getattr(primary_result, 'status', 0) == 200:
            ohlcv_bar = _normalize_ohlcv(primary_result)
            if ohlcv_bar is not None:
                return _persist_ohlcv_success(
                    conn, run_id, ticker, date, source,
                    ohlcv_bar, "polygon", retries, primary_result,
                )
            else:
                # Empty valid response from provider
                return _persist_ohlcv_empty(
                    conn, run_id, ticker, date, source, retries, primary_result,
                )

        # ── Classify primary error before fallback decision ──
        if primary_ec is None and primary_result is not None:
            primary_ec = classify_fetch_error(
                primary_result.status,
                error_message=getattr(primary_result, 'error', None),
            )

        # ── Fallback decision ──
        try_fallback = (
            fallback_fn
            and primary_ec
            and primary_ec.value not in TERMINAL_ERRORS
        )
        fallback_result = None
        fallback_error = None
        if try_fallback:
            try:
                fallback_result = await fallback_fn(ticker, "ohlcv", date)
                if fallback_result is not None and getattr(fallback_result, 'status', 0) == 200:
                    ohlcv_bar = _normalize_ohlcv(fallback_result)
                    if ohlcv_bar is not None:
                        return _persist_ohlcv_success(
                            conn, run_id, ticker, date, source,
                            ohlcv_bar, "yfinance", retries, fallback_result,
                            fallback_provider="yfinance_ohlcv",
                        )
            except Exception as exc:
                fallback_error = str(exc)

        # ── Failure path: preserve both primary and fallback evidence ──
        final_error = primary_error or fallback_error or "OHLCV fetch failed"
        final_ec = primary_ec or classify_fetch_error(
            None, error_message=final_error,
            exception_type="Unknown",
        )
        fr = FetchResult(
            status=0, error=final_error, source_label=source,
        )
        if hasattr(fr, 'error_class'):
            fr.error_class = final_ec.value
        result = persist_cell(
            db_path, run_id=run_id, ticker=ticker, date=date,
            source=source, fetch_result=fr, error_class=final_ec.value,
            retries=retries,
        )
        conn.close()
        return result

    # ── SEC filings path ──    # ── SEC filings path ──    # ── SEC filings path ──
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

def _b2_transport_from_legacy_fetch_fn(fetch_fn: Any):
    endpoint_aliases = {
        "polygon_news": "news",
        "polygon_ohlcv": "ohlcv",
        "yfinance_ohlcv": "ohlcv",
        "finnhub_company_news": "company-news",
        "sec_filings": "sec_submissions",
    }

    async def _transport(provider: str, method: str, url: str, **kwargs: Any):
        source = kwargs["source"]
        ticker = kwargs["ticker"]
        date = kwargs["date"]
        endpoint = endpoint_aliases.get(source, kwargs.get("endpoint", source))
        if isinstance(fetch_fn, dict):
            fetcher = fetch_fn.get(source) or fetch_fn.get(provider) or fetch_fn.get(endpoint)
            if fetcher is None:
                raise ValueError(f"No fake fetch_fn registered for source {source}")
        else:
            fetcher = fetch_fn
        return await fetcher(ticker, endpoint, date)

    return _transport


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

    if sources is None:
        sources = ["polygon_news"]

    if dry_run:
        import warnings
        warnings.warn(
            "run_update_batch(dry_run=True) is deprecated. "
            "Use plan_update from catalyst_data.update_planner instead.",
            DeprecationWarning, stacklevel=2,
        )
        from catalyst_data.update_planner import plan_update as _plan_update
        plan = _plan_update(
            db_path,
            tickers=tickers,
            sources=sources,
            from_date=from_date,
            to_date=to_date,
        )
        all_cells = plan.stages["market"]["cells"] + plan.stages["evidence"]["cells"]
        return {
            "run_id": None,
            "mode": "dry-run",
            "plan_preview": True,
            "cells_total": len(all_cells),
            "cells_success": 0,
            "cells_failed": 0,
            "cells_skipped": 0,
            "missing_cells": all_cells,
            "articles_upserted": 0,
            "clean_assets_inserted": 0,
            "index_delta_new": 0,
            "index_delta_changed": 0,
            "index_would_embed": 0,
            "elapsed_sec": 0.0,
            "freshness_before": {},
            "freshness_after": {},
            "per_cell_report": [],
            "plan_hash": plan.plan_hash,
        }

    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)

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

    # dry_run handled above via early return in run_update_batch

    # Real run — requires fetch_fn
    if fetch_fn is None:
        conn.close()
        raise ValueError("fetch_fn is required for non-dry-run execution")

    resolved_tickers = tickers or [
        r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol"
        ).fetchall()
    ]
    has_b2_schema = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_request_attempts'"
    ).fetchone() is not None
    b2_delegate_sources = {"polygon_news", "polygon_ohlcv", "yfinance_ohlcv"}
    if has_b2_schema and set(sources).issubset(b2_delegate_sources):
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
        market_cells = [c for c in missing if c[2] in ("polygon_ohlcv", "yfinance_ohlcv")]
        evidence_cells = [c for c in missing if c[2] not in ("polygon_ohlcv", "yfinance_ohlcv")]
        plan = UpdatePlan(
            config={
                "tickers": resolved_tickers,
                "sources": sources,
                "from_date": from_date,
                "to_date": to_date,
            },
            universe={"tickers": resolved_tickers, "provenance": "run_update_batch"},
            reference_today=to_date or "",
            latest_closed_session=to_date or "",
            stages={
                "market": {"cells": market_cells, "count": len(market_cells)},
                "evidence": {"cells": evidence_cells, "count": len(evidence_cells), "provisional": True},
            },
            estimates={"requests": {src: len([c for c in missing if c[2] == src]) for src in sources}},
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = plan.plan_hash
        conn.close()
        b2_conn = sqlite3.connect(db_path)
        b2_conn.row_factory = sqlite3.Row
        try:
            report = await execute_update(
                db=b2_conn,
                plan=plan,
                transport=_b2_transport_from_legacy_fetch_fn(fetch_fn),
            )
            if any(src in sources for src in ("polygon_news", "finnhub_company_news")):
                from catalyst_data.source_tier import classify_articles
                classify_articles(b2_conn)
                from catalyst_data.dedup.cross_source import compute_cross_source_dedup
                report["dedup_groups_resolved"] = compute_cross_source_dedup(b2_conn)
            return report
        finally:
            b2_conn.close()

    conn.close()

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

    if config.dry_run:
        import warnings
        warnings.warn(
            "run_update(dry_run=True) is deprecated. "
            "Use plan_update from catalyst_data.update_planner instead.",
            DeprecationWarning, stacklevel=2,
        )
        from catalyst_data.update_planner import plan_update as _plan_update
        plan = _plan_update(
            db_path,
            tickers=config.tickers,
            sources=config.sources or None,
            from_date=config.from_date,
            to_date=config.to_date,
        )
        ended = datetime.now(timezone.utc).isoformat()
        all_cells = plan.stages["market"]["cells"] + plan.stages["evidence"]["cells"]
        report = RunReport(
            run_id="", mode="dry-run", resume_from=config.resume_from,
            config={**_config_report_dict(config), "plan_preview": True, "plan_hash": plan.plan_hash}, started_at=started, ended_at=ended,
            elapsed_sec=round(time.monotonic() - start_time, 2),
            providers={src: {"cells_total": plan.estimates["requests"].get(src, 0),
                             "cells_success": 0, "cells_failed": 0, "cells_skipped": 0}
                       for src in sources},
            rows_changed={}, index_state={}, doctor=None,
        )
        return report

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

    # dry_run handled at top of function

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
