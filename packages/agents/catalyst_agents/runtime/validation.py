from __future__ import annotations

from datetime import date
import sqlite3
from typing import Any


DEFAULT_MAX_QUERY_CHARS = 500


def _failure(sub_reason: str, message: str, field: str) -> dict[str, Any]:
    return {
        "status": "FAILED_REQUEST",
        "sub_reason": sub_reason,
        "message": message,
        "field": field,
        "retryable": False,
    }


def _result_ok(*, ticker: str, trade_date: str, query: str | None) -> dict[str, Any]:
    return {
        "ok": True,
        "ticker": ticker,
        "trade_date": trade_date,
        "query": query,
        "failure": None,
    }


def _result_failure(sub_reason: str, message: str, field: str) -> dict[str, Any]:
    return {
        "ok": False,
        "ticker": None,
        "trade_date": None,
        "query": None,
        "failure": _failure(sub_reason, message, field),
    }


def _normalize_query(query: Any, max_query_chars: int) -> tuple[bool, str | None, dict[str, Any] | None]:
    if query is None:
        return True, None, None
    if not isinstance(query, str):
        return False, None, _failure("invalid_query_type", "Query must be a string or null.", "query")
    stripped = query.strip()
    if not stripped:
        return False, None, _failure("empty_query", "Query must not be blank when provided.", "query")
    if len(stripped) > max_query_chars:
        return False, None, _failure("query_too_long", "Query exceeds the maximum supported length.", "query")
    return True, stripped, None


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _ticker_universe(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]).upper()
        for row in conn.execute("SELECT DISTINCT symbol FROM ohlcv WHERE symbol IS NOT NULL")
    }


def validate_live_run_request(
    conn: sqlite3.Connection,
    *,
    ticker: Any,
    trade_date: Any,
    query: Any = None,
    max_query_chars: int = DEFAULT_MAX_QUERY_CHARS,
) -> dict[str, Any]:
    if not isinstance(ticker, str) or not ticker.strip():
        return _result_failure("unsupported_ticker", "Ticker is not available in the runtime dataset.", "ticker")
    normalized_ticker = ticker.strip().upper()
    supported_tickers = _ticker_universe(conn)
    if normalized_ticker not in supported_tickers:
        return _result_failure("unsupported_ticker", "Ticker is not available in the runtime dataset.", "ticker")

    if not isinstance(trade_date, str) or not _is_iso_date(trade_date):
        return _result_failure("invalid_trade_date", "Trade date must use ISO date format.", "trade_date")

    bounds = conn.execute(
        "SELECT MIN(date), MAX(date) FROM ohlcv WHERE symbol = ?",
        (normalized_ticker,),
    ).fetchone()
    min_date, max_date = bounds if bounds else (None, None)
    if min_date is None or max_date is None or trade_date < min_date or trade_date > max_date:
        return _result_failure("date_out_of_range", "Trade date is outside available OHLCV coverage.", "trade_date")

    row = conn.execute(
        "SELECT 1 FROM ohlcv WHERE symbol = ? AND date = ? LIMIT 1",
        (normalized_ticker, trade_date),
    ).fetchone()
    if row is None:
        return _result_failure(
            "missing_trading_day_context",
            "No OHLCV row exists for the requested ticker and trade date.",
            "trade_date",
        )

    query_ok, normalized_query, query_failure = _normalize_query(query, max_query_chars)
    if not query_ok:
        return {
            "ok": False,
            "ticker": None,
            "trade_date": None,
            "query": None,
            "failure": query_failure,
        }

    return _result_ok(ticker=normalized_ticker, trade_date=trade_date, query=normalized_query)
