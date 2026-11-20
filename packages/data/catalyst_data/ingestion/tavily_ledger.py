"""Atomic UTC-day reservations for real Tavily API calls.

The app owns the P-2 runtime ledger; data-core owns the transport hooks that
make Tavily calls.  This module keeps the small shared ledger contract between
them without importing app code from data-core.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from catalyst_data.read.tavily_usage import used_for_day as used_for_day

DEFAULT_DAILY_TAVILY_CALL_LIMIT = 50
PUBLIC_TAVILY_ENABLED_ENV = "CATALYST_PUBLIC_TAVILY_ENABLED"
_PUBLIC_TIERS = frozenset({"anonymous", "invite"})
_TIERS = frozenset({"anonymous", "invite", "admin", "local"})

TAVILY_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS tavily_call_reservations (
    call_id      TEXT PRIMARY KEY,
    utc_day      TEXT NOT NULL,
    tier         TEXT NOT NULL CHECK (tier IN ('anonymous','invite','admin','local')),
    run_id       TEXT,
    state        TEXT NOT NULL CHECK (state IN ('reserved','settled','released')),
    reserved_at  TEXT NOT NULL,
    settled_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tavily_reservations_day_state
    ON tavily_call_reservations(utc_day, state);
"""


def _utc_now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("tavily ledger time must be timezone-aware")
    return result.astimezone(timezone.utc)


def _parse_switch(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"invalid boolean Tavily configuration: {PUBLIC_TAVILY_ENABLED_ENV}")


def daily_limit_from_environment() -> int:
    raw = os.environ.get("CATALYST_DAILY_TAVILY_CALL_LIMIT")
    try:
        value = DEFAULT_DAILY_TAVILY_CALL_LIMIT if raw is None else int(raw)
    except ValueError as exc:
        raise RuntimeError(
            "invalid integer budget configuration: CATALYST_DAILY_TAVILY_CALL_LIMIT"
        ) from exc
    if value < 1:
        raise RuntimeError("CATALYST_DAILY_TAVILY_CALL_LIMIT must be positive")
    return value


def public_enabled_from_environment() -> bool:
    return _parse_switch(os.environ.get(PUBLIC_TAVILY_ENABLED_ENV))


def _refuse(code: str) -> None:
    # Imported lazily: ``web_search`` imports this module for its transport hooks.
    from catalyst_data.ingestion.collectors.web_search import WebSearchError

    raise WebSearchError(code)


@dataclass(frozen=True)
class TavilyCallContext:
    """Per-run (or per-request) identity for the global Tavily call counter."""

    db_path: Path
    tier: str
    run_id: str | None = None
    daily_limit: int = DEFAULT_DAILY_TAVILY_CALL_LIMIT
    public_enabled: bool = False

    def __post_init__(self) -> None:
        if self.tier not in _TIERS:
            raise ValueError("unknown access tier")
        if self.daily_limit < 1:
            raise ValueError("tavily call limit must be positive")

    @classmethod
    def from_environment(
        cls,
        *,
        db_path: str | Path,
        tier: str,
        run_id: str | None = None,
    ) -> "TavilyCallContext":
        return cls(
            db_path=Path(db_path),
            tier=tier,
            run_id=run_id,
            daily_limit=daily_limit_from_environment(),
            public_enabled=public_enabled_from_environment(),
        )

    @property
    def public(self) -> bool:
        return self.tier in _PUBLIC_TIERS


@dataclass(frozen=True)
class TavilyCallReservation:
    context: TavilyCallContext
    call_id: str


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(TAVILY_LEDGER_DDL)
    conn.commit()


def _used_on_own_connection(path: str | Path, utc_day: str) -> int:
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        return used_for_day(conn, utc_day)
    finally:
        conn.close()


def preflight(context: TavilyCallContext | None, *, now: datetime | None = None) -> None:
    """Refuse public Tavily work before touching key pools or transports.

    This is a cheap, non-authoritative check used only to preserve an explicit
    reason when no key is configured.  ``reserve`` repeats the check atomically.
    """
    if context is None or not context.public:
        return
    if not context.public_enabled:
        _refuse("web_search_public_disabled")
    utc_day = _utc_now(now).date().isoformat()
    if _used_on_own_connection(context.db_path, utc_day) >= context.daily_limit:
        _refuse("web_search_budget_exhausted")


def reserve(
    context: TavilyCallContext | None,
    *,
    now: datetime | None = None,
) -> TavilyCallReservation | None:
    """Reserve one real call before dispatch; public calls fail closed at the limit."""
    if context is None:
        return None
    current = _utc_now(now)
    utc_day = current.date().isoformat()
    if context.public and not context.public_enabled:
        _refuse("web_search_public_disabled")

    conn = _connect(context.db_path)
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            used = used_for_day(conn, utc_day)
            if context.public and used >= context.daily_limit:
                conn.rollback()
                _refuse("web_search_budget_exhausted")
            call_id = uuid4().hex
            conn.execute(
                "INSERT INTO tavily_call_reservations "
                "(call_id,utc_day,tier,run_id,state,reserved_at,settled_at) "
                "VALUES (?,?,?,?,'reserved',?,NULL)",
                (call_id, utc_day, context.tier, context.run_id, current.isoformat()),
            )
            conn.commit()
            return TavilyCallReservation(context=context, call_id=call_id)
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    finally:
        conn.close()


def settle(reservation: TavilyCallReservation | None, *, now: datetime | None = None) -> None:
    if reservation is None:
        return
    current = _utc_now(now)
    conn = _connect(reservation.context.db_path)
    try:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE tavily_call_reservations SET state='settled', settled_at=? "
            "WHERE call_id=? AND state='reserved'",
            (current.isoformat(), reservation.call_id),
        )
        conn.commit()
    finally:
        conn.close()


def release(reservation: TavilyCallReservation | None, *, now: datetime | None = None) -> None:
    """Release a failed call so it does not consume the day's limit."""
    if reservation is None:
        return
    current = _utc_now(now)
    conn = _connect(reservation.context.db_path)
    try:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE tavily_call_reservations SET state='released', settled_at=? "
            "WHERE call_id=? AND state='reserved'",
            (current.isoformat(), reservation.call_id),
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def guard(
    context: TavilyCallContext | None,
    *,
    now: datetime | None = None,
) -> Iterator[None]:
    """Reserve before a Tavily transport call, settle success, release failure."""
    reservation = reserve(context, now=now)
    try:
        yield
    except BaseException:
        release(reservation, now=now)
        raise
    else:
        settle(reservation, now=now)


__all__ = [
    "DEFAULT_DAILY_TAVILY_CALL_LIMIT",
    "PUBLIC_TAVILY_ENABLED_ENV",
    "TAVILY_LEDGER_DDL",
    "TavilyCallContext",
    "TavilyCallReservation",
    "daily_limit_from_environment",
    "guard",
    "preflight",
    "public_enabled_from_environment",
    "release",
    "reserve",
    "settle",
    "used_for_day",
]
