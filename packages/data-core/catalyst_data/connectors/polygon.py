"""Polygon.io connector for OHLCV daily bars and ticker news.

Endpoints:
- ohlcv: /v2/aggs/ticker/{ticker}/range/1/day/{date}/{date}
- news:  /v2/reference/news?ticker={ticker}&published_utc filters

Free tier: 5 req/min. Use TokenBucketLimiter for rate control.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
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
    return url, {}


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

    return with_retry(fetch)
