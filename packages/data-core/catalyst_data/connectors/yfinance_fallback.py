from __future__ import annotations

import asyncio
import time
from typing import Callable, Awaitable, TYPE_CHECKING

from catalyst_data.connectors.base import FetchResult

if TYPE_CHECKING:
    from catalyst_data.rate_limiter import TokenBucketLimiter

YF_STATEMENT_MAP = {
    "income_statement": "income_stmt",
    "balance_sheet": "balance_sheet",
    "cash_flow": "cashflow",
}


def _fetch_yf_sync(ticker: str, attr_name: str) -> dict | None:
    """Synchronous yfinance fetch — isolated for easy mocking."""
    import yfinance as yf

    stock = yf.Ticker(ticker)
    df = getattr(stock, attr_name, None)
    if df is None or (hasattr(df, "empty") and df.empty):
        return None
    return df.to_dict()


def create_yfinance_fetcher(
    limiter: TokenBucketLimiter | None = None,
) -> Callable[[str, str, str], Awaitable[FetchResult]]:
    """Return an async fetch function wrapping synchronous yfinance calls."""

    async def fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
        attr_name = YF_STATEMENT_MAP.get(endpoint)
        if not attr_name:
            return FetchResult(
                status=0,
                error=f"Unsupported endpoint for yfinance: {endpoint}",
                source_label=f"yfinance:{endpoint}",
            )

        start = time.monotonic()
        try:
            if limiter is None:
                data = await asyncio.to_thread(_fetch_yf_sync, ticker, attr_name)
            else:
                async with limiter.acquire():
                    data = await asyncio.to_thread(_fetch_yf_sync, ticker, attr_name)
            latency = (time.monotonic() - start) * 1000
            if data is None:
                return FetchResult(
                    status=404,
                    error="No data returned from yfinance",
                    latency_ms=latency,
                    source_label=f"yfinance:{endpoint}",
                )
            return FetchResult(
                status=200,
                data=data,
                latency_ms=latency,
                source_label=f"yfinance:{endpoint}",
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return FetchResult(
                status=0,
                error=str(e),
                latency_ms=latency,
                source_label=f"yfinance:{endpoint}",
            )

    return fetch
