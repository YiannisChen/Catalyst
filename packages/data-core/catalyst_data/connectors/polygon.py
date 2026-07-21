"""Polygon.io connector for OHLCV daily bars and ticker news.

Endpoints:
- ohlcv: /v2/aggs/ticker/{ticker}/range/1/day/{date}/{date}
- news:  /v2/reference/news?ticker={ticker}&published_utc filters

Free tier: 5 req/min. Use TokenBucketLimiter for rate control.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable

import httpx

from catalyst_data.connectors.base import FetchResult
from catalyst_data.retry import with_retry

POLYGON_BASE_URL = "https://api.polygon.io"


def _parse_retry_after(resp: httpx.Response) -> float | None:
    """Extract Retry-After header value as seconds, or None if absent/unparseable."""
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _build_ohlcv_url(ticker: str, date: str) -> tuple[str, dict[str, str]]:
    """Build URL and params for a single-day OHLCV aggregate request."""
    url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/{date}/{date}"
    return url, {"adjusted": "true"}


def _build_news_url(ticker: str, date: str) -> tuple[str, dict[str, str]]:
    """Build URL and params for news within a single calendar day."""
    dt = datetime.strptime(date, "%Y-%m-%d")
    next_day = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"{POLYGON_BASE_URL}/v2/reference/news"
    params = {
        "ticker": ticker,
        "published_utc.gte": f"{date}T00:00:00Z",
        "published_utc.lt": f"{next_day}T00:00:00Z",
        "limit": "50",
    }
    return url, params


_ENDPOINT_BUILDERS = {
    "ohlcv": _build_ohlcv_url,
    "news": _build_news_url,
}


def create_polygon_fetcher(
    api_key: str,
    limiter=None,
    client: httpx.AsyncClient | None = None,
) -> Callable[[str, str, str], Awaitable[FetchResult]]:
    """Return an async fetch function bound to the given api_key and optional limiter.

    Args:
        api_key: Polygon.io API key.
        limiter: Optional TokenBucketLimiter; ``await limiter.acquire()`` is
            called before each request when provided.
        client: Optional httpx.AsyncClient for dependency injection (tests).
    """

    async def fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
        builder = _ENDPOINT_BUILDERS.get(endpoint)
        if builder is None:
            return FetchResult(
                status=0,
                error=f"Unknown Polygon endpoint: {endpoint}",
                source_label=f"polygon:{endpoint}",
            )

        url, params = builder(ticker, date)
        params["apiKey"] = api_key

        start = time.monotonic()
        own_client = client is None
        c = client if not own_client else httpx.AsyncClient(timeout=30.0)
        try:
            if limiter is None:
                resp = await c.get(url, params=params)
            else:
                async with limiter.acquire():
                    resp = await c.get(url, params=params)
            latency = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                return FetchResult(
                    status=200,
                    data=resp.json(),
                    latency_ms=latency,
                    source_label=f"polygon:{endpoint}",
                )
            error_text = resp.text.strip() if getattr(resp, "text", None) else ""
            error_message = f"Polygon {resp.status_code}"
            if error_text:
                error_message = f"{error_message}: {error_text}"

            retry_after = _parse_retry_after(resp)
            return FetchResult(
                status=resp.status_code,
                error=error_message,
                latency_ms=latency,
                source_label=f"polygon:{endpoint}",
                retry_after_seconds=retry_after,
            )
        except (httpx.TimeoutException, httpx.ReadTimeout) as exc:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=f"Timeout: {exc}",
                latency_ms=latency,
                source_label=f"polygon:{endpoint}",
            )
        except httpx.HTTPError as exc:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=str(exc),
                latency_ms=latency,
                source_label=f"polygon:{endpoint}",
            )
        finally:
            if own_client:
                await c.aclose()

    return with_retry(fetch, provider="polygon")


# ============================================================================
# B2 — Polygon pagination with request ledger, raw store, and provenance
# ============================================================================

import asyncio
import hashlib
import json as _json
from urllib.parse import urlparse, parse_qs

from catalyst_data.ingestion.redaction import (
    compute_cursor_fingerprint,
    compute_request_fingerprint,
    redact_request,
)
from catalyst_data.ingestion.request_ledger import insert_attempt, transition_attempt, compute_logical_fetch_id
from catalyst_data.ingestion.raw_store import store_raw_response
from catalyst_data.ingestion.provenance import record_provenance


class PolygonPaginationError(Exception):
    """Raised on pagination loop, duplicate cursor, or unrecoverable page error."""


def _table_columns(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _upsert_polygon_article(
    db,
    *,
    article_pk: str,
    item: dict,
    item_tickers: list[str],
    ticker: str,
    date: str,
    raw_asset_id: str,
) -> None:
    article_cols = _table_columns(db, "articles")
    publisher = item.get("publisher") or {}
    publisher_name = publisher.get("name") if isinstance(publisher, dict) else None

    if "raw_asset_id" in article_cols:
        db.execute(
            """INSERT OR REPLACE INTO articles
               (article_id, raw_asset_id, provider, source_type, ticker,
                reference_date, published_utc, title, description, article_url,
                image_url, publisher_name, tickers_json)
               VALUES (?, ?, 'polygon', 'polygon_news', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                article_pk,
                raw_asset_id,
                ticker,
                date,
                item.get("published_utc") or f"{date}T00:00:00Z",
                item.get("title") or "",
                item.get("description"),
                item.get("article_url"),
                item.get("image_url"),
                publisher_name,
                _json.dumps(sorted(item_tickers)),
            ),
        )
    else:
        db.execute(
            """INSERT OR REPLACE INTO articles
               (article_id, title, description, published_utc, source,
                publisher, article_url, image_url)
               VALUES (?, ?, ?, ?, 'polygon', ?, ?, ?)""",
            (
                article_pk,
                item.get("title"),
                item.get("description"),
                item.get("published_utc"),
                publisher_name,
                item.get("article_url"),
                item.get("image_url"),
            ),
        )

    ticker_cols = _table_columns(db, "article_tickers")
    for article_ticker in item_tickers:
        if "raw_asset_id" in ticker_cols:
            db.execute(
                """INSERT OR REPLACE INTO article_tickers
                   (article_id, ticker, raw_asset_id, reference_date)
                   VALUES (?, ?, ?, ?)""",
                (article_pk, article_ticker, raw_asset_id, date),
            )
        else:
            db.execute(
                """INSERT OR IGNORE INTO article_tickers
                   (article_id, ticker, reference_date)
                   VALUES (?, ?, ?)""",
                (article_pk, article_ticker, date),
            )


async def fetch_paginated_news(
    *,
    db,
    run_id: str,
    ticker: str,
    date: str,
    transport,
    page_limit: int = 20,
    item_limit: int = 1000,
) -> dict:
    """Fetch Polygon news with pagination, request ledger, raw store, and provenance.

    Uses an injected ``transport(provider, method, url, **kwargs)`` callable.
    Each page creates one provider_request_attempts row and one raw_assets row.
    Returns a summary dict with article upsert counts and checkpoint status.
    """
    cell_id = hashlib.sha256(
        _json.dumps({
            "source_type": "news", "endpoint_name": "polygon_news",
            "ticker_or_series": ticker, "window_start": date, "window_end": date,
            "stage": "evidence", "provider_profile_version": "v1",
        }, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    logical_fetch_id = compute_logical_fetch_id(run_id, cell_id)

    seen_article_ids: set[str] = set()
    seen_cursor_fingerprints: set[str] = set()
    total_items = 0
    page_no = 0
    parent_request_id = None
    checkpoint_status = "success"
    is_complete = 1

    # Ensure ingestion_runs parent row exists (FK requirement)
    db.execute(
        "INSERT OR IGNORE INTO ingestion_runs (run_id, status, started_at) VALUES (?, 'pending', strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        (run_id,),
    )
    db.commit()

    next_url = f"https://api.polygon.io/v2/reference/news?ticker={ticker}&published_utc.gte={date}T00:00:00Z&published_utc.lt={date}T23:59:59Z&limit=50"

    while next_url and page_no < page_limit and total_items < item_limit:
        page_no += 1

        # Compute cursor fingerprint for loop detection
        parsed = urlparse(next_url)
        cursor_fp = None
        if "cursor" in (parsed.query or ""):
            cursor_fp = compute_cursor_fingerprint(next_url)
            if cursor_fp in seen_cursor_fingerprints:
                raise PolygonPaginationError(
                    f"Cursor loop detected at page {page_no}: cursor fingerprint repeated"
                )
            seen_cursor_fingerprints.add(cursor_fp)

        # Build request identity
        request_id = hashlib.sha256(
            f"polygon_news:{ticker}:{date}:page_{page_no}:{run_id}".encode()
        ).hexdigest()

        redacted_request = redact_request({
            "method": "GET",
            "url": next_url,
            "params": {"page_no": page_no},
            "provider_profile_version": "v1",
        })
        redacted_params = _json.dumps(
            redacted_request["sorted_redacted_params"],
            sort_keys=True,
            separators=(",", ":"),
        )
        request_fingerprint = compute_request_fingerprint(redacted_request)

        # Insert STARTED attempt
        insert_attempt(db, {
            "request_id": request_id,
            "run_id": run_id,
            "logical_fetch_id": logical_fetch_id,
            "source_type": "news",
            "provider": "polygon",
            "endpoint_name": "polygon_news",
            "ticker_or_series": ticker,
            "window_start": date,
            "window_end": date,
            "attempt_no": 1,
            "page_no": page_no,
            "parent_request_id": parent_request_id,
            "request_fingerprint": request_fingerprint,
            "request_params_redacted": redacted_params,
            "cursor_fingerprint": cursor_fp,
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "completed_at": None,
            "status": "STARTED",
        })

        try:
            response = await transport(
                "polygon", "GET", next_url,
                source="polygon_news", endpoint="polygon_news",
                ticker=ticker, date=date,
            )
        except Exception as exc:
            transition_attempt(db, request_id, "TRANSPORT_ERROR",
                               error_class="transport_error",
                               error_message_redacted=str(exc)[:200])
            checkpoint_status = "failed"
            is_complete = 0
            break

        if isinstance(response, FetchResult):
            resp_data = response.data if isinstance(response.data, dict) else {}
        else:
            resp_data = response.json() if hasattr(response, 'json') else {}

        # Store raw response
        raw_body = _json.dumps(resp_data).encode()
        raw_asset_id = store_raw_response(
            db, request_id=request_id, response_bytes=raw_body,
            content_encoding="identity", page_no=page_no,
            ticker=ticker, reference_date=date, source_type="polygon_news",
        )

        items = resp_data.get("results", [])
        items_count = len(items)

        # Transition to terminal
        if items_count == 0 and page_no == 1:
            transition_attempt(db, request_id, "SUCCEEDED",
                               http_status=200, items_count=0,
                               raw_asset_id=raw_asset_id)
            checkpoint_status = "success_empty"
            next_url = None
            break

        # Deduplicate and record provenance
        new_count = 0
        for item in items:
            article_id = item.get("id", "")
            if article_id in seen_article_ids:
                continue
            seen_article_ids.add(article_id)
            new_count += 1
            total_items += 1

            publisher = item.get("publisher") or {}
            article_pk = f"poly:{article_id}"
            item_tickers = item.get("tickers") or [ticker]
            _upsert_polygon_article(
                db, article_pk=article_pk, item=item,
                item_tickers=item_tickers, ticker=ticker, date=date,
                raw_asset_id=raw_asset_id,
            )

            entity_version = hashlib.sha256(
                _json.dumps({
                    "article_id": article_pk,
                    "title": item.get("title"),
                    "description": item.get("description"),
                    "published_utc": item.get("published_utc"),
                    "publisher": publisher.get("name") if isinstance(publisher, dict) else None,
                    "article_url": item.get("article_url"),
                    "tickers": sorted(item_tickers),
                    "source": "polygon",
                }, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()
            record_provenance(
                db, entity_type="article", entity_id=article_pk,
                entity_version=entity_version, raw_asset_id=raw_asset_id,
            )

        transition_attempt(db, request_id, "SUCCEEDED",
                           http_status=200, items_count=items_count,
                           raw_asset_id=raw_asset_id)

        # Follow next_url
        next_url_str = resp_data.get("next_url") if isinstance(resp_data, dict) else None
        if next_url_str:
            next_url = next_url_str
            parent_request_id = request_id
        else:
            next_url = None

    # Update checkpoint
    limit_hit = page_no >= page_limit or total_items >= item_limit
    if limit_hit and next_url is not None:
        checkpoint_status = "partial"
        is_complete = 0

    # Upsert checkpoint
    db.execute(
        """INSERT OR REPLACE INTO source_checkpoints
           (run_id, source_type, ticker, date, status, logical_fetch_id,
            request_count, pages_received, items_received, is_complete)
           VALUES (?, 'polygon_news', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, ticker, date, checkpoint_status, logical_fetch_id,
         page_no, page_no, total_items, is_complete),
    )
    db.commit()

    return {
        "run_id": run_id,
        "ticker": ticker,
        "date": date,
        "status": checkpoint_status,
        "pages": page_no,
        "articles_upserted": total_items,
        "is_complete": bool(is_complete),
    }
