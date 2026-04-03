from __future__ import annotations

import time
from typing import Callable, Awaitable, TYPE_CHECKING

import httpx

from catalyst_data.connectors.base import FetchResult

if TYPE_CHECKING:
    from catalyst_data.rate_limiter import TokenBucketLimiter

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

ENDPOINT_PATH_MAP = {
    "income_statement": "income-statement",
    "balance_sheet": "balance-sheet-statement",
    "cash_flow": "cash-flow-statement",
}


def create_fmp_fetcher(
    api_key: str,
    limiter: TokenBucketLimiter | None = None,
    client: httpx.AsyncClient | None = None,
) -> Callable[[str, str, str], Awaitable[FetchResult]]:
    """Return an async fetch function bound to the given api_key/limiter/client."""

    async def fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
        path = ENDPOINT_PATH_MAP.get(endpoint, endpoint)
        url = f"{FMP_BASE_URL}/{path}/{ticker}"
        params = {"apikey": api_key, "period": "annual"}

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
                    source_label=f"fmp:{endpoint}",
                )
            return FetchResult(
                status=resp.status_code,
                error=f"FMP {resp.status_code}",
                latency_ms=latency,
                source_label=f"fmp:{endpoint}",
            )
        except (httpx.TimeoutException, httpx.ReadTimeout) as e:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=f"timeout: {e}",
                latency_ms=latency,
                source_label=f"fmp:{endpoint}",
            )
        except httpx.HTTPError as e:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=str(e),
                latency_ms=latency,
                source_label=f"fmp:{endpoint}",
            )
        finally:
            if own_client:
                await c.aclose()

    return fetch
