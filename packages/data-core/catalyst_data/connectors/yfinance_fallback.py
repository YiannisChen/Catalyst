from __future__ import annotations

import asyncio
import time
from typing import Callable, Awaitable, TYPE_CHECKING

from catalyst_data.connectors.base import FetchResult
from catalyst_data.retry import with_retry

if TYPE_CHECKING:
    from catalyst_data.rate_limiter import TokenBucketLimiter

YF_OHLCV_HISTORY_KEY = "ohlcv"

YF_STATEMENT_MAP = {
    "income_statement": "income_stmt",
    "balance_sheet": "balance_sheet",
    "cash_flow": "cashflow",
}


def _status_for_yfinance_exception(exc: Exception) -> int:
    message = str(exc).lower()
    if "429" in message or "too many requests" in message or "rate limit" in message:
        return 429
    if "timeout" in message:
        return 0
    return 0


def _fetch_yf_sync(ticker: str, attr_name: str) -> dict | None:
    """Synchronous yfinance fetch — isolated for easy mocking."""
    import yfinance as yf

    stock = yf.Ticker(ticker)
    df = getattr(stock, attr_name, None)
    if df is None or (hasattr(df, "empty") and df.empty):
        return None
    return df.to_dict()




def _fetch_yf_ohlcv_sync(ticker: str, date: str) -> dict | None:
    """Synchronous yfinance OHLCV fetch — returns bar dict or None."""
    import yfinance as yf
    from datetime import datetime, timedelta

    stock = yf.Ticker(ticker)
    dt = datetime.strptime(date, "%Y-%m-%d")
    next_day = dt + timedelta(days=1)
    df = stock.history(start=date, end=next_day.strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return None
    row = df.iloc[-1]
    return {
        "open": float(row["Open"]), "high": float(row["High"]),
        "low": float(row["Low"]), "close": float(row["Close"]),
        "volume": float(row["Volume"]),
    }


async def _fetch_ohlcv(ticker: str, date: str) -> FetchResult:
    """Async yfinance OHLCV fetcher."""
    start = time.monotonic()
    try:
        data = await asyncio.to_thread(_fetch_yf_ohlcv_sync, ticker, date)
        latency = (time.monotonic() - start) * 1000
        if data is None:
            return FetchResult(
                status=200, data=None, latency_ms=latency,
                source_label="yfinance:ohlcv",
            )
        return FetchResult(
            status=200, data=data, latency_ms=latency,
            source_label="yfinance:ohlcv",
        )
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return FetchResult(
            status=_status_for_yfinance_exception(e),
            error=str(e), latency_ms=latency,
            source_label="yfinance:ohlcv",
        )


def create_yfinance_fetcher(
    limiter: TokenBucketLimiter | None = None,
) -> Callable[[str, str, str], Awaitable[FetchResult]]:
    """Return an async fetch function wrapping synchronous yfinance calls."""

    async def fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
        if endpoint == YF_OHLCV_HISTORY_KEY:
            return await _fetch_ohlcv(ticker, date)
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
                status=_status_for_yfinance_exception(e),
                error=str(e),
                latency_ms=latency,
                source_label=f"yfinance:{endpoint}",
            )

    return with_retry(fetch, provider="yfinance")
