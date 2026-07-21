"""Shared dependency-free US trading calendar helpers.

The local OHLCV table is useful evidence, but it must not be the ceiling for
news alignment or live gap-fill windows. These helpers union local OHLCV dates
with a small calendar oracle so post-OHLCV publications still map to dates.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta



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

US_MARKET_HOLIDAYS: frozenset[str] = frozenset({
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
    "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
    "2027-11-25", "2027-12-24",
    # 2024 (added W1-B)
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29",
    "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02",
    "2024-11-28", "2024-12-25",
})


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
