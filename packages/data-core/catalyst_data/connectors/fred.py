"""FRED (Federal Reserve Economic Data) connector for macro indicators.

Supported series IDs: DFF, DGS10, VIXCLS, UNRATE, CPIAUCSL, etc.

URL pattern:
  https://api.stlouisfed.org/fred/series/observations
    ?series_id={endpoint}&api_key={key}&file_type=json
    &observation_start={date-30d}&observation_end={date}

FRED uses "." for missing data; these observations are filtered out.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable, Awaitable

import httpx

from catalyst_data.connectors.base import FetchResult
from catalyst_data.retry import with_retry

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


def create_fred_fetcher(
    api_key: str,
    limiter=None,
    client: httpx.AsyncClient | None = None,
) -> Callable[[str, str, str], Awaitable[FetchResult]]:
    """Return an async fetch function bound to the given api_key and optional limiter.

    Args:
        api_key: FRED API key.
        limiter: Optional rate limiter; ``await limiter.acquire()`` is
            called before each request when provided.
        client: Optional httpx.AsyncClient for dependency injection (tests).
    """

    async def fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
        dt = datetime.strptime(date, "%Y-%m-%d")
        start_date = (dt - timedelta(days=30)).strftime("%Y-%m-%d")

        params = {
            "series_id": endpoint,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start_date,
            "observation_end": date,
        }

        start = time.monotonic()
        own_client = client is None
        c = client if not own_client else httpx.AsyncClient(timeout=30.0)
        try:
            if limiter is None:
                resp = await c.get(FRED_BASE_URL, params=params)
            else:
                async with limiter.acquire():
                    resp = await c.get(FRED_BASE_URL, params=params)
            latency = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                body = resp.json()
                # Filter out FRED missing-data markers (".")
                observations = [
                    obs for obs in body.get("observations", [])
                    if obs.get("value") != "."
                ]
                body["observations"] = observations
                return FetchResult(
                    status=200,
                    data=body,
                    latency_ms=latency,
                    source_label=f"fred:{endpoint}",
                )
            return FetchResult(
                status=resp.status_code,
                error=f"FRED {resp.status_code}: {resp.text}",
                latency_ms=latency,
                source_label=f"fred:{endpoint}",
            )
        except (httpx.TimeoutException, httpx.ReadTimeout) as exc:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=f"Timeout: {exc}",
                latency_ms=latency,
                source_label=f"fred:{endpoint}",
            )
        except httpx.HTTPError as exc:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=str(exc),
                latency_ms=latency,
                source_label=f"fred:{endpoint}",
            )
        finally:
            if own_client:
                await c.aclose()

    return with_retry(fetch, provider="fred")
