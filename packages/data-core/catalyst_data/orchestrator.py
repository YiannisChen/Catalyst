"""Orchestrator: ties source mapping, fetch, ingest, clean, transform, and storage together.

External callers invoke ``process_request`` with a ticker, date, and list of
logical source names.  The orchestrator fans out to physical endpoints, runs the
full Bronze-to-Silver pipeline, and persists results to SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Awaitable

from catalyst_data.connectors.base import FetchResult
from catalyst_data.pipeline.clean import run_clean
from catalyst_data.pipeline.ingest import run_ingest
from catalyst_data.pipeline.transform import run_transform
from catalyst_data.source_mapping import map_logical_source
from catalyst_data.storage.sqlite import (
    compute_asset_id,
    upsert_clean_asset,
    upsert_raw_asset,
)

logger = logging.getLogger(__name__)

FetchFn = Callable[..., Awaitable[FetchResult]]


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


async def _process_source(
    ticker: str,
    date: str,
    source: str,
    conn: sqlite3.Connection,
    fetch_fn: FetchFn,
) -> dict[str, Any]:
    """Run the full pipeline for a single logical source.

    Returns a summary dict with keys: source, ok, asset_id (if stored),
    error (if failed), and stage_latencies.
    """
    summary: dict[str, Any] = {
        "source": source,
        "ok": False,
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

    # 4. Store Bronze (raw_assets)
    asset_id = compute_asset_id(ticker, date, source)
    raw_bytes = json.dumps(validated_data, default=str).encode("utf-8")

    # Use the first successful HTTP status for metadata
    first_status = next(
        (r.status for r in fetch_results.values() if r.status == 200), None
    )

    try:
        upsert_raw_asset(
            conn,
            asset_id=asset_id,
            ticker=ticker,
            source_type=source,
            reference_date=date,
            content_raw=raw_bytes,
            http_status=first_status,
            metadata={"endpoints": endpoints},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to store Bronze asset %s: %s", asset_id, exc)
        summary["error"] = f"bronze_storage: {exc}"
        return summary

    summary["asset_id"] = asset_id

    # 5. Clean
    clean_result = run_clean(validated_data, source)
    summary["stage_latencies"]["clean"] = clean_result.latency_ms

    if not clean_result.ok:
        summary["error"] = f"clean: {clean_result.error}"
        return summary

    cleaned_data = clean_result.data

    # 6. Transform to Markdown
    transform_result = run_transform(cleaned_data, source, ticker)
    summary["stage_latencies"]["transform"] = transform_result.latency_ms

    if not transform_result.ok:
        summary["error"] = f"transform: {transform_result.error}"
        return summary

    content_md = transform_result.data

    # 7. Store Silver (clean_assets)
    try:
        upsert_clean_asset(
            conn,
            asset_id=asset_id,
            ticker=ticker,
            source_type=source,
            reference_date=date,
            content_md=content_md,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to store Silver asset %s: %s", asset_id, exc)
        summary["error"] = f"silver_storage: {exc}"
        return summary

    summary["ok"] = True
    return summary


async def process_request(
    ticker: str,
    date: str,
    sources: list[str],
    conn: sqlite3.Connection,
    fetch_fn: FetchFn,
    limiter: Any | None = None,
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
    conn : sqlite3.Connection
        Open database connection with schema initialised.
    fetch_fn : async callable
        ``async def fetch(ticker, endpoint, date) -> FetchResult``.
    limiter : optional
        Reserved for future rate-limiter integration.

    Returns
    -------
    list[dict]
        One summary dict per source with keys: source, ok, asset_id, error,
        stage_latencies.
    """
    results: list[dict] = []

    for source in sources:
        try:
            summary = await _process_source(
                ticker, date, source, conn, fetch_fn,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Unhandled error processing source %s: %s", source, exc)
            summary = {
                "source": source,
                "ok": False,
                "asset_id": None,
                "error": f"unhandled: {exc}",
                "stage_latencies": {},
            }
        results.append(summary)

    return results
