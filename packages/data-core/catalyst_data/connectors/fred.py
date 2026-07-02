"""FRED (Federal Reserve Economic Data) connector for macro indicators.

Supported series IDs: DFF, DGS10, DGS2, VIXCLS, UNRATE, CPIAUCSL, PCEPI, PAYEMS,
GDP, DAAA, DBAA, plus derived T10Y2Y.

URL pattern:
  https://api.stlouisfed.org/fred/series/observations
    ?series_id={endpoint}&api_key={key}&file_type=json
    &observation_start={date-30d}&observation_end={date}
    [&output_type=4&realtime_start=...&realtime_end=...]  # for first-release

FRED uses "." for missing data; these observations are filtered out.
output_type=4 returns initial release values with per-observation realtime_start
(first-release date) — required for no-look-ahead integrity.
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

    async def fetch(ticker: str, endpoint: str, date: str,
                    start_date: str | None = None,
                    output_type: int | None = None,
                    realtime_start: str | None = None,
                    realtime_end: str | None = None) -> FetchResult:
        dt = datetime.strptime(date, "%Y-%m-%d")
        obs_start = start_date or (dt - timedelta(days=30)).strftime("%Y-%m-%d")

        params = {
            "series_id": endpoint,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": obs_start,
            "observation_end": date,
        }
        if output_type is not None:
            params["output_type"] = str(output_type)
        if realtime_start is not None:
            params["realtime_start"] = realtime_start
        if realtime_end is not None:
            params["realtime_end"] = realtime_end

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
            safe_text = (resp.text or "").replace(api_key, "[REDACTED]") if resp.text else ""
            return FetchResult(
                status=resp.status_code,
                error=f"FRED {resp.status_code}: {safe_text}",
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
