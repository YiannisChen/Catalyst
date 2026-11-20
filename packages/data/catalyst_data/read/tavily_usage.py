"""Read-only Tavily reservation counting."""
from __future__ import annotations

import sqlite3


def used_for_day(conn: sqlite3.Connection, utc_day: str) -> int:
    """Count reserved and settled calls for a UTC day; released calls do not count."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM tavily_call_reservations "
            "WHERE utc_day=? AND state!='released'",
            (utc_day,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return 0
        raise
    return int(row[0])
