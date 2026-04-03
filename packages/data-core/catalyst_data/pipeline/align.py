"""News-to-trading-day alignment.

Maps article publication timestamps to the trading day they affect,
and computes forward returns from a given trade date.

Adapted from PokieTicker's alignment.py pattern.
"""

from __future__ import annotations

import bisect
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKET_CLOSE = time(16, 0)


def map_to_trade_date(
    published_utc: str,
    trading_days: list[str],
) -> str | None:
    """Map a news publication timestamp to the relevant trading day.

    Rules:
    - Convert *published_utc* to US/Eastern time.
    - If published before 16:00 ET, the candidate date is that calendar day.
    - If published at or after 16:00 ET, the candidate date is the next
      calendar day.
    - Find the candidate (or the first trading day on or after it) in
      *trading_days*.  Return ``None`` when no such day exists.
    """
    # Handle "Z" suffix that fromisoformat doesn't accept in older Pythons
    ts_str = published_utc.replace("Z", "+00:00")
    dt_utc = datetime.fromisoformat(ts_str)

    # Ensure aware, then convert to Eastern
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    dt_et = dt_utc.astimezone(ET)

    # Determine candidate calendar date
    if dt_et.time() >= MARKET_CLOSE:
        # After-hours: candidate is next calendar day
        from datetime import timedelta

        candidate = (dt_et.date() + timedelta(days=1)).isoformat()
    else:
        candidate = dt_et.date().isoformat()

    # Find the first trading day >= candidate via binary search
    idx = bisect.bisect_left(trading_days, candidate)
    if idx < len(trading_days):
        return trading_days[idx]
    return None


def compute_forward_returns(
    trade_date: str,
    closes: dict[str, float],
    dates_sorted: list[str],
) -> dict[str, float | None]:
    """Compute forward returns relative to *trade_date*.

    Returns a dict with keys ``ret_t0``, ``ret_t1``, ``ret_t3``, ``ret_t5``:
    - ret_t0 = (close[T] - close[T-1]) / close[T-1]
    - ret_tN = (close[T+N] - close[T]) / close[T]  for N in {1, 3, 5}

    Any return that cannot be computed (missing dates / data) is ``None``.
    """
    result: dict[str, float | None] = {
        "ret_t0": None,
        "ret_t1": None,
        "ret_t3": None,
        "ret_t5": None,
    }

    if trade_date not in closes:
        return result

    idx = bisect.bisect_left(dates_sorted, trade_date)
    if idx >= len(dates_sorted) or dates_sorted[idx] != trade_date:
        return result

    close_t = closes[trade_date]

    # ret_t0: requires previous trading day
    if idx > 0:
        prev_date = dates_sorted[idx - 1]
        if prev_date in closes:
            close_prev = closes[prev_date]
            result["ret_t0"] = (close_t - close_prev) / close_prev

    # Forward returns
    for offset, key in [(1, "ret_t1"), (3, "ret_t3"), (5, "ret_t5")]:
        fwd_idx = idx + offset
        if fwd_idx < len(dates_sorted):
            fwd_date = dates_sorted[fwd_idx]
            if fwd_date in closes:
                result[key] = (closes[fwd_date] - close_t) / close_t

    return result
