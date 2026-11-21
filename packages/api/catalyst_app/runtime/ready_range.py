"""Small app adapter for the data-core prepared-range snapshot."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class ReadyRangeUnavailable(RuntimeError):
    """The ready snapshot could not be imported, read, or trusted."""


@lru_cache(maxsize=8)
def _read_snapshot(path: str, mtime_ns: int, size: int) -> dict[str, Any]:
    del mtime_ns, size
    read_api = importlib.import_module("catalyst_data.read_api")
    return read_api.ready_snapshot(path)


@dataclass(frozen=True)
class ReadyRange:
    generated_at: str
    last_trading_day: str | None
    pairs: frozenset[tuple[str, str]]
    status: str
    data_identity: str | None = None


def most_recent_completed_trading_day(now: datetime | None = None) -> date:
    """Return the latest session whose NYSE close has passed.

    Uses ``catalyst_data.trading_calendar`` (the repo's US market holiday and
    early-close calendar), and checks its session close in UTC so a snapshot
    is not marked stale during an open session.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    calendar = importlib.import_module("catalyst_data.trading_calendar")
    eastern_today = current.astimezone(ZoneInfo("America/New_York")).date()
    candidate = calendar.latest_closed_trading_day_for_date(eastern_today)
    for _ in range(14):
        if calendar.is_trading_day(candidate.isoformat()):
            close = datetime.fromisoformat(
                calendar.session_close_utc(candidate.isoformat()).replace("Z", "+00:00")
            )
            if close <= current:
                return candidate
        candidate -= timedelta(days=1)
        candidate = calendar.latest_closed_trading_day_for_date(candidate)
    raise ValueError("no supported completed trading session in the prior 14 days")


def load_ready_range(*, now: datetime | None = None, path: str | None = None) -> ReadyRange:
    """Load a validated data-core snapshot, retaining pairs when its range is stale."""
    try:
        read_api = importlib.import_module("catalyst_data.read_api")
        store = getattr(read_api, "ready_snapshot_store", None)
        source = Path(path) if path is not None else (store.default_path() if store is not None else None)
        stat = None
        if source is not None:
            try:
                stat = source.stat()
            except OSError:
                stat = None
        if source is None or stat is None:
            snapshot: Any = read_api.ready_snapshot(path)
        else:
            # Cached by file identity: /api/options is hit on every page load.
            snapshot = _read_snapshot(str(source), stat.st_mtime_ns, stat.st_size)
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot is not an object")
        generated_at = snapshot["generated_at"]
        last_day = snapshot["last_completed_session_date"]
        pairs = snapshot["pairs"]
        if not isinstance(generated_at, str) or not generated_at.endswith("Z"):
            raise ValueError("snapshot generated_at is invalid")
        datetime.fromisoformat(generated_at[:-1] + "+00:00")
        if last_day is not None:
            date.fromisoformat(last_day)
        if not isinstance(pairs, list):
            raise ValueError("snapshot pairs are invalid")
        normalized: set[tuple[str, str]] = set()
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("snapshot pair is invalid")
            ticker = pair.get("ticker")
            session_date = pair.get("session_date")
            if not isinstance(ticker, str) or not ticker or not isinstance(session_date, str):
                raise ValueError("snapshot pair is invalid")
            parsed_date = date.fromisoformat(session_date)
            if parsed_date.isoformat() != session_date:
                raise ValueError("snapshot pair date is invalid")
            normalized.add((ticker.strip().upper(), session_date))
    except Exception as exc:
        raise ReadyRangeUnavailable("prepared_range_unavailable") from exc

    try:
        latest_completed = most_recent_completed_trading_day(now)
        stale = last_day is None or date.fromisoformat(last_day) < latest_completed
    except Exception as exc:
        raise ReadyRangeUnavailable("prepared_range_unavailable") from exc
    return ReadyRange(
        generated_at=generated_at,
        last_trading_day=last_day,
        pairs=frozenset(normalized),
        status="stale" if stale else "ready",
        data_identity=generated_at.strip() or None,
    )


__all__ = [
    "ReadyRange",
    "ReadyRangeUnavailable",
    "load_ready_range",
    "most_recent_completed_trading_day",
]
