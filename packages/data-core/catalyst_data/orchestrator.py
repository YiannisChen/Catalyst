"""Orchestrator: ties source mapping, fetch, ingest, clean, transform, and storage together.

External callers invoke ``process_request`` with a ticker, date, and list of
logical source names.  The orchestrator fans out to physical endpoints, runs the
full Bronze-to-Silver pipeline, and persists results to SQLite.

Concurrency model (BUG-003 / BUG-004):
  Sources are processed concurrently via ``asyncio.gather``.  SQLite writes are
  offloaded to a thread pool via ``asyncio.to_thread`` so they never block the
  event loop.  Each thread-bound worker opens its own ``sqlite3.Connection`` from
  ``db_path`` to avoid cross-thread connection sharing (sqlite3 is not thread-safe
  when sharing a single connection object).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Awaitable

from catalyst_data.connectors.base import FetchResult
from catalyst_data.config import (
    FALLBACK_PRICE_MOVE_THRESHOLD,
    FALLBACK_RETRY_THRESHOLD,
)
from catalyst_data.dedup.cross_source import AssetCandidate as NormalizedAsset
from catalyst_data.pipeline.clean import run_clean
from catalyst_data.pipeline.ingest import run_ingest
from catalyst_data.pipeline.transform import run_transform
from catalyst_data.pipeline.transform_v2 import run_transform_v2
from catalyst_data.quality import assess_quality_fields, normalize_title
from catalyst_data.source_mapping import map_logical_source
from catalyst_data.storage.sqlite import (
    compute_asset_id,
    init_db,
    upsert_clean_asset,
    upsert_ohlcv,
    upsert_raw_asset,
)

logger = logging.getLogger(__name__)

FetchFn = Callable[..., Awaitable[FetchResult]]


@dataclass(frozen=True)
class PrimaryFetchResult:
    articles: list[NormalizedAsset]
    connectivity_failure_count: int
    price_move_pct: float | None


def _extract_ohlcv_bar(validated_data: dict[str, Any]) -> dict[str, float | str] | None:
    payload = validated_data.get("ohlcv")
    if not isinstance(payload, dict):
        return None

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None

    first_bar = results[0]
    if not isinstance(first_bar, dict):
        return None

    try:
        return {
            "open": float(first_bar["o"]),
            "high": float(first_bar["h"]),
            "low": float(first_bar["l"]),
            "close": float(first_bar["c"]),
            "volume": float(first_bar["v"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def should_trigger_fallback(
    ticker: str,
    date: str,
    primary_result: PrimaryFetchResult,
) -> tuple[bool, str | None]:
    """Return whether fallback should run, plus the first matching trigger reason."""
    del ticker, date

    if (
        len(primary_result.articles) == 0
        and primary_result.price_move_pct is not None
        and abs(primary_result.price_move_pct) >= FALLBACK_PRICE_MOVE_THRESHOLD
    ):
        return True, "empty_primary_with_big_move"

    if primary_result.connectivity_failure_count >= FALLBACK_RETRY_THRESHOLD:
        return True, "primary_connectivity_failures"

    if primary_result.articles:
        title_counts: dict[str, int] = {}
        for article in primary_result.articles:
            normalized = normalize_title(article.title)
            if normalized is None:
                continue
            title_counts[normalized] = title_counts.get(normalized, 0) + 1

        assessments = [
            assess_quality_fields(
                title=article.title,
                source=article.source_type,
                published_utc=article.published_utc.isoformat(),
                body_md=article.body_md,
                title_counts=title_counts,
            )
            for article in primary_result.articles
        ]
        if assessments and all(not assessment.is_rag_eligible for assessment in assessments):
            return True, "primary_all_fails_quality"

    return False, None


async def _fetch_endpoints(
    ticker: str,
    date: str,
    endpoints: list[str],
    fetch_fn: FetchFn,
) -> dict[str, FetchResult]:
    """Fetch all endpoints for a logical source, collecting results."""
    results: dict[str, FetchResult] = {}
    for ep in endpoints:
        try:
            results[ep] = await fetch_fn(ticker, ep, date)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fetch failed for endpoint %s: %s", ep, exc)
            results[ep] = FetchResult(
                status=0,
                error=str(exc),
                latency_ms=0.0,
                source_label=ep,
            )
    return results


def _store_bronze_and_silver(
    db_path: str | Path,
    *,
    asset_id: str,
    ticker: str,
    source: str,
    date: str,
    raw_bytes: bytes,
    http_status: int | None,
    endpoints: list[str],
    clean_rows: list[dict],
    ohlcv_bar: dict[str, float | str] | None = None,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Synchronous helper that writes Bronze + Silver in one thread-safe call.

    When *conn* is provided, uses it instead of opening its own — caller
    manages init_db/PRAGMAs/commit/close.  Otherwise opens its own connection
    (backward-compatible for existing callers).

    Returns None on success or an error string on failure.
    """
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(str(db_path))
        init_db(conn)
        # Legacy FK on clean_assets.asset_id expects raw_assets-compatible ids,
        # but polygon_news now uses poly:{article_id} format. Disable FK for that scope.
        # Must be set AFTER init_db (which sets foreign_keys=ON).
        if source == "polygon_news":
            conn.execute("PRAGMA foreign_keys=OFF")
    try:
        upsert_raw_asset(
            conn,
            asset_id=asset_id,
            ticker=ticker,
            source_type=source,
            reference_date=date,
            content_raw=raw_bytes,
            http_status=http_status,
            metadata={"endpoints": endpoints},
            commit=own_conn,
        )
        for row in clean_rows:
            upsert_clean_asset(
                conn,
                asset_id=row["asset_id"],
                ticker=ticker,
                source_type=source,
                reference_date=date,
                content_md=row["content_md"],
                title_hash=row.get("title_hash"),
                raw_asset_id=asset_id,
                commit=own_conn,
            )
        if ohlcv_bar is not None:
            upsert_ohlcv(
                conn,
                symbol=ticker,
                date=date,
                open=float(ohlcv_bar["open"]),
                high=float(ohlcv_bar["high"]),
                low=float(ohlcv_bar["low"]),
                close=float(ohlcv_bar["close"]),
                volume=float(ohlcv_bar["volume"]),
                source="polygon",
                commit=own_conn,
            )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("Storage failed for %s: %s", asset_id, exc)
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass
        return str(exc)
    finally:
        if own_conn:
            try:
                conn.commit()
                conn.close()
            except Exception:
                pass


async def _process_source(
    ticker: str,
    date: str,
    source: str,
    db_path: str | Path,
    fetch_fn: FetchFn,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run the full pipeline for a single logical source.

    Returns a summary dict with keys: source, ok, asset_id (if stored),
    error (if failed), and stage_latencies.
    """
    summary: dict[str, Any] = {
        "source": source,
        "ok": False,
        "skipped": False,
        "skip_reason": None,
        "asset_id": None,
        "error": None,
        "endpoint_statuses": {},
        "failed_endpoints": {},
        "stage_latencies": {},
    }

    # 1. Map logical source to physical endpoints
    endpoints = map_logical_source(source)

    # 2. Fetch each endpoint
    fetch_results = await _fetch_endpoints(ticker, date, endpoints, fetch_fn)
    summary["endpoint_statuses"] = {
        ep: result.status for ep, result in fetch_results.items()
    }
    summary["failed_endpoints"] = {
        ep: {
            "status": result.status,
            "error": result.error,
            "source_label": result.source_label or ep,
        }
        for ep, result in fetch_results.items()
        if result.status != 200 or result.error
    }

    # 3. Run ingest validation
    endpoint_data: dict[str, Any] = {}
    endpoint_statuses: dict[str, int] = {}
    for ep, result in fetch_results.items():
        endpoint_data[ep] = result.data
        endpoint_statuses[ep] = result.status

    ingest_result = run_ingest(endpoint_data, endpoint_statuses)
    summary["stage_latencies"]["ingest"] = ingest_result.latency_ms

    if not ingest_result.ok:
        summary["error"] = f"ingest: {ingest_result.error}"
        return summary

    validated_data = ingest_result.data

    # 4. Prepare data for storage
    asset_id = compute_asset_id(ticker, date, source)
    raw_bytes = json.dumps(validated_data, default=str).encode("utf-8")

    first_status = next(
        (r.status for r in fetch_results.values() if r.status == 200), None
    )

    # 5. Clean
    clean_result = run_clean(validated_data, source)
    summary["stage_latencies"]["clean"] = clean_result.latency_ms

    if not clean_result.ok:
        summary["error"] = f"clean: {clean_result.error}"
        return summary

    cleaned_data = clean_result.data
    if "news" in source and isinstance(cleaned_data, list) and len(cleaned_data) == 0:
        summary["skipped"] = True
        summary["skip_reason"] = "no_articles"
        return summary

    # 6. Transform to Markdown
    if source == "polygon_news":
        # Per-article transform (no merge) — each article becomes its own clean_asset
        transform_results = run_transform_v2(cleaned_data, source, ticker)
        total_latency = sum(r.latency_ms for r in transform_results)
        summary["stage_latencies"]["transform"] = total_latency
        ok_results = [r for r in transform_results if r.ok]
        if not ok_results:
            summary["error"] = "transform: all articles failed"
            return summary
        clean_rows = [r.data for r in ok_results]
    else:
        transform_result = run_transform(cleaned_data, source, ticker)
        summary["stage_latencies"]["transform"] = transform_result.latency_ms
        if not transform_result.ok:
            summary["error"] = f"transform: {transform_result.error}"
            return summary
        clean_rows = [{"asset_id": asset_id, "content_md": transform_result.data, "title_hash": None}]

    # 7. Prepare or store Bronze + Silver.
    #
    # H2 atomic-cell path passes _store=False so the caller can write
    # raw+silver+checkpoint in one transaction.  In that mode this function is
    # strictly prepare-only: no DB connection is opened and no commit happens.
    summary["asset_id"] = asset_id
    ohlcv_bar = _extract_ohlcv_bar(validated_data) if source == "polygon_ohlcv" else None
    if source == "polygon_ohlcv" and ohlcv_bar is None:
        summary["asset_id"] = None
        summary["skipped"] = True
        summary["skip_reason"] = "no_ohlcv_bar"
        return summary
    # Include storage data in result for callers that want to re-store atomically
    summary["_raw_bytes"] = raw_bytes
    summary["_clean_rows"] = clean_rows
    summary["_http_status"] = first_status
    summary["_endpoints"] = endpoints
    summary["_ohlcv_bar"] = ohlcv_bar

    if kwargs.get("_store") is False:
        summary["ok"] = True
        return summary

    # Store Bronze + Silver (offloaded to thread pool to avoid blocking loop).
    # Allow caller to supply a shared connection for atomic writes
    shared_conn = kwargs.get("_conn")
    if shared_conn is not None:
        storage_err = _store_bronze_and_silver(
            db_path,
            asset_id=asset_id,
            ticker=ticker,
            source=source,
            date=date,
            raw_bytes=raw_bytes,
            http_status=first_status,
            endpoints=endpoints,
            clean_rows=clean_rows,
            ohlcv_bar=ohlcv_bar,
            conn=shared_conn,
        )
    else:
        storage_err = await asyncio.to_thread(
            _store_bronze_and_silver,
            db_path,
            asset_id=asset_id,
            ticker=ticker,
            source=source,
            date=date,
            raw_bytes=raw_bytes,
            http_status=first_status,
            endpoints=endpoints,
            clean_rows=clean_rows,
            ohlcv_bar=ohlcv_bar,
        )
    if storage_err:
        summary["error"] = f"storage: {storage_err}"
        return summary

    summary["ok"] = True
    return summary


async def process_request(
    ticker: str,
    date: str,
    sources: list[str],
    db_path: str | Path,
    fetch_fn: FetchFn,
    limiter: Any | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    store: bool = True,
) -> list[dict]:
    """Orchestrate the full data pipeline for one ticker/date across sources.

    Parameters
    ----------
    ticker : str
        Stock symbol, e.g. ``"AAPL"``.
    date : str
        Reference date in ``YYYY-MM-DD`` format.
    sources : list[str]
        Logical source names, e.g. ``["polygon_news", "fmp_fundamentals"]``.
    db_path : str | Path
        Path to the SQLite database file (or ``":memory:"``).  Each source
        opens its own connection from this path so writes never block the
        event loop and concurrent sources don't share a connection object.
    fetch_fn : async callable
        ``async def fetch(ticker, endpoint, date) -> FetchResult``.
    limiter : optional
        Reserved for future rate-limiter integration.
    conn : sqlite3.Connection | None
        **Deprecated.** Ignored — kept for backward compatibility only.  Pass
        ``db_path`` instead.

    Returns
    -------
    list[dict]
        One summary dict per source with keys: source, ok, asset_id, error,
        stage_latencies.
    """

    async def _safe_process(source: str) -> dict:
        try:
            extra = {"_store": store}
            if conn is not None:
                extra["_conn"] = conn
            return await _process_source(
                ticker, date, source, db_path, fetch_fn, **extra,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Unhandled error processing source %s: %s", source, exc)
            return {
                "source": source,
                "ok": False,
                "asset_id": None,
                "error": f"unhandled: {exc}",
                "stage_latencies": {},
            }

    results = await asyncio.gather(*[_safe_process(s) for s in sources])
    return list(results)
