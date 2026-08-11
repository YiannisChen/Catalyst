"""Cutoff computation with session-close times and early-close support.

Uses the production trading_calendar (no second holiday source).
Implements ExchangeCutoffPolicy per §0.1.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from catalyst_data.trading_calendar import (
    CALENDAR_YEARS, ET, is_trading_day, session_close_utc,
    EARLY_CLOSE_DATES, REGULAR_CLOSE, EARLY_CLOSE,
)


class CutoffPolicyError(ValueError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


def _session_day(session_date: str):
    from datetime import date

    try:
        day = date.fromisoformat(session_date)
    except (TypeError, ValueError):
        raise CutoffPolicyError("not_a_trading_session", str(session_date)) from None
    if not CALENDAR_YEARS[0] <= day.year <= CALENDAR_YEARS[1]:
        raise CutoffPolicyError("calendar_out_of_range", session_date)
    if not is_trading_day(session_date):
        raise CutoffPolicyError("not_a_trading_session", session_date)
    return day


class ExchangeCutoffPolicy:
    """US equities exchange cutoff policy — delegates to production calendar."""

    def close_to_close(self, ticker: str, session_date: str) -> str:
        """Return the official exchange-close cutoff for a given session."""
        _session_day(session_date)
        return session_close_utc(session_date)

    def intraday(self, ticker: str, session_date: str, as_of: str) -> str:
        """Validate and canonicalize an intraday as_of.

        The as_of must end with Z, be on the session date in ET,
        and not be after the official exchange close.
        Returns second-resolution canonical UTC.
        """
        return _validate_intraday(session_date, as_of)

    def compute_cutoff(
        self,
        ticker: str,
        session_date: str,
        mode: str = "close_to_close",
        as_of: str | None = None,
    ) -> str:
        """Policy-level cutoff computation (graph runtime contract).

        Mirrors the module-level ``compute_cutoff`` function so the
        attribution graph can call ``cutoff_policy.compute_cutoff(...)``:
        ``close_to_close`` and ``attribution`` resolve to the official
        exchange close; ``intraday`` canonicalizes an explicit UTC as_of.
        """
        if mode in {"close_to_close", "attribution"}:
            return self.close_to_close(ticker, session_date)
        if mode == "intraday":
            if as_of is None:
                raise CutoffPolicyError("invalid_as_of", "intraday requires as_of")
            return self.intraday(ticker, session_date, as_of)
        raise CutoffPolicyError("invalid_mode", str(mode))


def _validate_intraday(session_date: str, as_of: str) -> str:
    """Validate intraday as_of and return canonical second-resolution UTC."""
    if not as_of.endswith("Z"):
        raise CutoffPolicyError("invalid_as_of", "intraday requires UTC as_of ending with Z")

    # Parse as_of — handle optional fractional seconds
    as_of_clean = as_of.replace("Z", "+00:00")
    try:
        as_of_dt = datetime.fromisoformat(as_of_clean)
    except ValueError:
        raise CutoffPolicyError("invalid_as_of", f"cannot parse {as_of}") from None

    if as_of_dt.tzinfo is None:
        raise CutoffPolicyError("invalid_as_of", "as_of must be UTC")

    # Convert to ET for date validation
    as_of_et = as_of_dt.astimezone(ET)

    # Verify session_date is a trading day
    d = _session_day(session_date)

    # Verify as_of ET date matches session_date
    if as_of_et.date() != d:
        raise CutoffPolicyError("invalid_as_of", "as_of date does not match session_date in ET")

    # Verify as_of is not after exchange close
    close_time = EARLY_CLOSE if session_date in EARLY_CLOSE_DATES else REGULAR_CLOSE
    close_et = datetime.combine(d, close_time, tzinfo=ET)

    if as_of_et > close_et:
        raise CutoffPolicyError("invalid_as_of", "as_of after exchange close")

    # Return canonical second-resolution UTC
    return as_of_dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_cutoff(
    ticker: str,
    session_date: str,
    mode: str = "close_to_close",
    as_of: str | None = None,
) -> str:
    """Compute a retrieval cutoff.

    mode='close_to_close': use the official exchange close for the session.
    mode='intraday': validate and canonicalize the explicit UTC as_of.
    """
    policy = ExchangeCutoffPolicy()

    if mode == "close_to_close":
        return policy.close_to_close(ticker, session_date)

    if mode == "intraday":
        if as_of is None:
            raise CutoffPolicyError("invalid_as_of", "intraday requires as_of")
        return policy.intraday(ticker, session_date, as_of)

    raise CutoffPolicyError("invalid_mode", str(mode))


__all__ = ["CutoffPolicyError", "ExchangeCutoffPolicy", "compute_cutoff"]
