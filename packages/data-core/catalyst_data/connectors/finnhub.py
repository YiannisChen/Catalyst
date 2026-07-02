"""Finnhub connector for company news.

Endpoint: /api/v1/company-news?symbol=&from=&to=&token=

Free tier: 60 req/min. Uses TokenBucketLimiter for rate control.
Token is passed as query param — NEVER logged. Sanitize in error strings.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Callable, Awaitable

import httpx

from catalyst_data.connectors.base import FetchResult
from catalyst_data.retry import with_retry

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"


def _sanitize_url(url: str, token: str) -> str:
    """Replace token value in a URL string with [REDACTED]."""
    if not token:
        return url
    return url.replace(f"token={token}", "token=[REDACTED]")


def _build_company_news_url(ticker: str, date: str) -> tuple[str, dict[str, str]]:
    """Build URL and params for company-news on a single calendar day."""
    dt = datetime.strptime(date, "%Y-%m-%d")
    next_day = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"{FINNHUB_BASE_URL}/company-news"
    params = {
        "symbol": ticker,
        "from": date,
        "to": date,
    }
    return url, params


_ENDPOINT_BUILDERS = {
    "company-news": _build_company_news_url,
}


def create_finnhub_fetcher(
    api_key: str,
    limiter=None,
    client: httpx.AsyncClient | None = None,
):
    """Return a namespace with fetch() for Finnhub company-news.

    fetch(ticker, "company-news", date) -> FetchResult

    The api_key is passed as a 'token' query parameter on every request.
    Token is NEVER logged — sanitized from all error strings.
    """
    if not api_key:
        raise ValueError("FINNHUB_API_KEY is required but empty")

    token = api_key

    async def fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
        builder = _ENDPOINT_BUILDERS.get(endpoint)
        if builder is None:
            return FetchResult(
                status=0,
                error=f"Unknown Finnhub endpoint: {endpoint}",
                source_label=f"finnhub:{endpoint}",
            )

        url, params = builder(ticker, date)
        params["token"] = token

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
                data = resp.json()
                if not isinstance(data, list):
                    return FetchResult(
                        status=200,
                        data=[],
                        latency_ms=latency,
                        source_label="finnhub:company-news",
                    )
                return FetchResult(
                    status=200,
                    data=data,
                    latency_ms=latency,
                    source_label="finnhub:company-news",
                )

            retry_after = None
            if resp.status_code == 429:
                retry_after = 1.0  # Finnhub doesn't return Retry-After header

            safe_url = _sanitize_url(str(resp.url) if hasattr(resp, 'url') else url, token)
            error_body = resp.text[:200] if hasattr(resp, 'text') else ""
            return FetchResult(
                status=resp.status_code,
                error=f"Finnhub {resp.status_code}: {error_body} [{safe_url}]",
                latency_ms=latency,
                source_label="finnhub:company-news",
                retry_after_seconds=retry_after,
            )
        except (httpx.TimeoutException, httpx.ReadTimeout) as exc:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=f"Timeout: {exc}",
                latency_ms=latency,
                source_label="finnhub:company-news",
            )
        except httpx.HTTPError as exc:
            latency = (time.monotonic() - start) * 1000
            safe_error = _sanitize_url(str(exc), token)
            return FetchResult(
                status=0,
                error=safe_error,
                latency_ms=latency,
                source_label="finnhub:company-news",
            )
        finally:
            if own_client:
                await c.aclose()

    fetch_wrapped = with_retry(fetch, provider="finnhub")
    return SimpleNamespace(fetch=fetch_wrapped)
