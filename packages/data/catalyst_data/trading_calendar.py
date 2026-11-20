"""Shared dependency-free US trading calendar helpers.

The local OHLCV table is useful evidence, but it must not be the ceiling for
news alignment or live gap-fill windows. These helpers union local OHLCV dates
with a small calendar oracle so post-OHLCV publications still map to dates.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

# ---- W1-B: Calendar coverage ----

CALENDAR_YEARS: tuple[int, int] = (2024, 2027)


class CalendarCoverageError(Exception):
    """Requested date range is outside the supported holiday calendar."""

    def __init__(self, requested: str, supported: tuple[int, int]):
        self.requested = requested
        self.supported = supported
        super().__init__(
            f"Date {requested} is outside supported calendar range "
            f"{supported[0]}-{supported[1]}"
        )

# Fixed closed sets, including observed dates. Not computed from a formula.
_HOLIDAY_PATH = Path(__file__).resolve().with_name("us_market_holidays.json")


def _load_holiday_sets() -> tuple[frozenset[str], frozenset[str]]:
    payload = json.loads(_HOLIDAY_PATH.read_text(encoding="utf-8"))
    expected = ("US_MARKET_HOLIDAYS", "EARLY_CLOSE_DATES")
    if not isinstance(payload, dict) or set(payload) != set(expected):
        raise RuntimeError(
            f"{_HOLIDAY_PATH.name} must contain only US_MARKET_HOLIDAYS and EARLY_CLOSE_DATES"
        )
    loaded: dict[str, frozenset[str]] = {}
    for key in expected:
        raw = payload[key]
        if not isinstance(raw, list):
            raise RuntimeError(f"{_HOLIDAY_PATH.name} key {key} must be a list of date strings")
        closed: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                raise RuntimeError(f"{_HOLIDAY_PATH.name} key {key} must be a list of date strings")
            try:
                parsed = date.fromisoformat(item)
            except ValueError as exc:
                raise RuntimeError(
                    f"{_HOLIDAY_PATH.name} key {key} has a non-ISO date {item!r}"
                ) from exc
            if parsed.isoformat() != item or item in closed:
                raise RuntimeError(f"{_HOLIDAY_PATH.name} key {key} has a bad date {item!r}")
            closed.add(item)
        loaded[key] = frozenset(closed)
    if loaded["US_MARKET_HOLIDAYS"] & loaded["EARLY_CLOSE_DATES"]:
        raise RuntimeError(
            f"{_HOLIDAY_PATH.name} lists a date as both a full close and an early close"
        )
    return loaded["US_MARKET_HOLIDAYS"], loaded["EARLY_CLOSE_DATES"]


US_MARKET_HOLIDAYS: frozenset[str]
EARLY_CLOSE_DATES: frozenset[str]
US_MARKET_HOLIDAYS, EARLY_CLOSE_DATES = _load_holiday_sets()


def calendar_trading_days(from_date: str, to_date: str) -> list[str]:
    """Generate weekday dates in [from_date, to_date] excluding market holidays."""
    if not from_date or not to_date:
        return []

    # W1-B: fail closed outside supported calendar years
    for ds in (from_date, to_date):
        yr = int(ds[:4])
        if yr < CALENDAR_YEARS[0] or yr > CALENDAR_YEARS[1]:
            raise CalendarCoverageError(ds, CALENDAR_YEARS)

    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    if start > end:
        return []

    days: list[str] = []
    current = start
    while current <= end:
        ds = current.isoformat()
        if current.weekday() < 5 and ds not in US_MARKET_HOLIDAYS:
            days.append(ds)
        current += timedelta(days=1)
    return days


def latest_closed_trading_day_for_date(ref_date: date | None = None) -> date:
    """Return the latest US trading day on or before ref_date."""
    if ref_date is None:
        ref_date = date.today()

    candidate = ref_date
    while candidate.weekday() >= 5 or candidate.isoformat() in US_MARKET_HOLIDAYS:
        candidate -= timedelta(days=1)
    return candidate


def ohlcv_trading_days(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT date FROM ohlcv ORDER BY date").fetchall()
    return [r[0] for r in rows]


def ohlcv_bounds(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT MIN(date), MAX(date) FROM ohlcv").fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def trading_days_for_window(
    conn: sqlite3.Connection, from_date: str, to_date: str
) -> list[str]:
    """Return sorted OHLCV dates union calendar oracle for a requested window."""
    rows = conn.execute(
        """SELECT DISTINCT date FROM ohlcv
           WHERE date >= ? AND date <= ?
           ORDER BY date""",
        (from_date, to_date),
    ).fetchall()
    ohlcv_days = [r[0] for r in rows]
    return sorted(set(ohlcv_days) | set(calendar_trading_days(from_date, to_date)))


def trading_days_through(
    conn: sqlite3.Connection, through_date: str | None = None
) -> list[str]:
    """Return OHLCV dates union calendar dates through the requested ceiling."""
    min_date, max_date = ohlcv_bounds(conn)
    if min_date is None:
        raise RuntimeError(
            "Trading calendar is empty - ohlcv table has no dates. "
            "Cannot align articles without a starting calendar."
        )
    end = max(filter(None, [max_date, through_date]))
    return sorted(set(ohlcv_trading_days(conn)) | set(calendar_trading_days(min_date, end)))


# ── B4: Session close times ──────────────────────────────────────────────────

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


def is_trading_day(session_date: str) -> bool:
    """Return True if session_date is a US equities trading day."""
    try:
        d = date.fromisoformat(session_date)
    except ValueError:
        return False

    year = d.year
    if year < CALENDAR_YEARS[0] or year > CALENDAR_YEARS[1]:
        return False

    if d.weekday() >= 5:
        return False

    if session_date in US_MARKET_HOLIDAYS:
        return False

    return True


def session_close_utc(session_date: str) -> str:
    """Return the canonical UTC close timestamp for a trading session.

    Uses the production holiday calendar and early-close schedule.
    Returns second-resolution UTC: YYYY-MM-DDTHH:MM:SSZ.
    """
    if not is_trading_day(session_date):
        raise ValueError(f"not_a_trading_session: {session_date}")

    d = date.fromisoformat(session_date)
    close_time = EARLY_CLOSE if session_date in EARLY_CLOSE_DATES else REGULAR_CLOSE

    close_et = datetime.combine(d, close_time, tzinfo=ET)
    close_utc = close_et.astimezone(UTC)
    return close_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def session_open_utc(session_date: str) -> str:
    """Return the canonical UTC session-open timestamp (09:30 America/New_York).

    The market session opens at 09:30 ET on every trading day, including
    early-close sessions; DST is handled by the America/New_York zone so the
    UTC instant differs between EST and EDT. Fails closed on non-sessions.
    """
    if not is_trading_day(session_date):
        raise ValueError(f"not_a_trading_session: {session_date}")

    d = date.fromisoformat(session_date)
    open_et = datetime.combine(d, time(9, 30), tzinfo=ET)
    open_utc = open_et.astimezone(UTC)
    return open_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
