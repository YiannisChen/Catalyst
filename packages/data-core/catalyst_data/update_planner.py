"""Pure read-only update planning.  Never writes to the database.

Opens SQLite immutable+read-only (mode=ro&immutable=1).  Computes expected cells
from trading calendar, existing checkpoints, and explicit configuration.
Returns a deterministic UpdatePlan with a plan_hash for re-plan checks.

W1-A: initial zero-write planner.  Per-ticker defaults and explicit
universe are W1-B ownership — this slice preserves compatibility defaults.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_data.freshness import latest_local_ohlcv_date
from catalyst_data.trading_calendar import (
    calendar_trading_days,
    latest_closed_trading_day_for_date,
    trading_days_for_window,
)
from catalyst_data.storage.sqlite import FROZEN_PATHS

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SchemaOutOfDate(Exception):
    """Required tables/columns are missing.  Run migrations before planning."""

    def __init__(self, missing: list[str], fix_command: str | None = None):
        self.missing = missing
        self.fix_command = fix_command or (
            ".venv/bin/python packages/data-core/scripts/migrate_ingestion_quality.py"
        )
        super().__init__(
            f"Schema out of date.  Missing: {missing}.  "
            f"Fix: {self.fix_command}"
        )


class FrozenDBError(Exception):
    """Attempted to open a frozen DB path for planning."""


class ActiveWALForPlanning(Exception):
    """Non-empty WAL file exists — immutable read would ignore uncheckpointed data.

    The planner must not silently open an immutable connection on a DB
    with pending WAL content because immutable=1 skips the WAL and
    returns stale (pre-checkpoint) data.
    """

    def __init__(self, db_path: str, wal_size: int):
        self.db_path = db_path
        self.wal_size = wal_size
        super().__init__(
            f"Non-empty WAL ({wal_size} bytes) at {db_path}-wal.  "
            f"Close all writers before planning."
        )

# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


@dataclass
class PlanWarning:
    type: str       # "universe-fallback", "schema-behind", "watermark-behind-news"
    message: str


# ---------------------------------------------------------------------------
# UpdatePlan
# ---------------------------------------------------------------------------


@dataclass
class UpdatePlan:
    plan_schema_version: int = 1
    created_at: str = ""           # ISO-8601 UTC — EXCLUDED from hash
    db_path: str = ""              # logical path — excluded from hash
    db_sha256: str = ""
    db_user_version: int = 0
    config: dict[str, Any] = field(default_factory=dict)
    universe: dict[str, Any] = field(default_factory=dict)
    reference_today: str = ""
    latest_closed_session: str = ""
    stages: dict[str, Any] = field(default_factory=dict)
    estimates: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, str]] = field(default_factory=list)
    plan_hash: str = ""


# ---------------------------------------------------------------------------
# Canonical JSON helpers
# ---------------------------------------------------------------------------


def _canonical_json(plan: UpdatePlan) -> str:
    """Derive canonical JSON for hashing (excludes created_at and plan_hash)."""
    d = asdict(plan)
    d.pop("created_at", None)
    d.pop("plan_hash", None)
    d.pop("db_path", None)  # path varies; DB is identified by content SHA
    return json.dumps(d, sort_keys=True, default=str)


def compute_plan_hash(plan: UpdatePlan) -> str:
    """SHA-256 over canonical plan JSON."""
    payload = _canonical_json(plan)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

_REQUIRED_TABLES = ("ohlcv", "source_checkpoints")
_REQUIRED_COLUMNS = {
    "ohlcv": ("symbol", "date", "open", "high", "low", "close", "volume"),
    "source_checkpoints": ("run_id", "source_type", "ticker", "date", "status"),
}


def _check_schema(conn: sqlite3.Connection) -> None:
    """Raise SchemaOutOfDate if required tables or columns are missing."""
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing_tables = [t for t in _REQUIRED_TABLES if t not in tables]
    missing = list(missing_tables)

    # Check required columns on tables that exist
    for table, req_cols in _REQUIRED_COLUMNS.items():
        if table in tables:
            cols = {
                r[1]
                for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            table_missing = [
                f"{table}.{c}" for c in req_cols if c not in cols
            ]
            missing.extend(table_missing)

    if missing:
        raise SchemaOutOfDate(missing)


# ---------------------------------------------------------------------------
# Universe resolution
# ---------------------------------------------------------------------------


def _resolve_universe(
    conn: sqlite3.Connection, tickers: list[str] | None
) -> tuple[list[str], str, list[dict[str, str]]]:
    """Return (ticker_list, provenance_label, warnings).

    If tickers is explicit → "explicit-config" provenance.
    Otherwise → OHLCV-derived with "ohlcv-derived-fallback" + typed warning.
    """
    warnings: list[dict[str, str]] = []
    if tickers:
        return sorted(tickers), "explicit-config", warnings

    rows = conn.execute(
        "SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol"
    ).fetchall()
    tickers = [r[0] for r in rows]
    warnings.append({
        "type": "universe-fallback",
        "message": (
            "Universe derived from ohlcv table.  This fallback is not "
            "acceptable for certified execution.  Use explicit tickers or "
            "configured universe."
        ),
    })
    return tickers, "ohlcv-derived-fallback", warnings


# ---------------------------------------------------------------------------
# Plan entrypoint
# ---------------------------------------------------------------------------


def plan_update(
    db_path: str | os.PathLike[str],
    *,
    tickers: list[str] | None = None,
    sources: list[str] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    reference_today: str | None = None,
) -> UpdatePlan:
    """Compute an UpdatePlan from DB state.  NEVER writes.

    Opens DB immutable+read-only via SQLite URI mode=ro&immutable=1.
    """
    # ── Resolve physical path ──
    logical_path = str(db_path)
    resolved = Path(db_path).expanduser().resolve(strict=True)
    real = os.path.realpath(str(resolved))

    # ── Frozen DB guard ──
    if real in FROZEN_PATHS:
        raise FrozenDBError(f"Refusing to open frozen DB: {logical_path}")

    # ── WAL safety check (before immutable open) ──
    wal_path = Path(str(resolved) + "-wal")
    if wal_path.exists() and wal_path.stat().st_size > 0:
        raise ActiveWALForPlanning(logical_path, wal_path.stat().st_size)

    # ── Immutable read-only connection (resolved absolute path → as_uri) ──
    ro_uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(ro_uri, uri=True)

    try:
        # ── Schema check ──
        _check_schema(conn)

        # ── DB identity ──
        db_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
        db_ver = conn.execute("PRAGMA user_version").fetchone()[0]

        # ── Reference date ──
        if reference_today:
            ref_date = date.fromisoformat(reference_today)
        else:
            ref_date = date.today()
        ref_today_str = ref_date.isoformat()
        latest = latest_closed_trading_day_for_date(ref_date).isoformat()

        # ── Sources ──
        srcs = sources or ["polygon_news"]

        # ── Universe ──
        universe_tickers, provenance, warns = _resolve_universe(conn, tickers)
        if not universe_tickers:
            conn.close()
            plan = UpdatePlan(
                created_at=datetime.now(timezone.utc).isoformat(),
                db_path=logical_path, db_sha256=db_sha, db_user_version=db_ver,
                config={"tickers": [], "sources": srcs, "from_date": from_date,
                        "to_date": to_date, "reference_today": ref_today_str},
                universe={"tickers": [], "provenance": provenance},
                reference_today=ref_today_str,
                latest_closed_session=latest,
                stages={"market": {"cells": [], "count": 0},
                        "evidence": {"cells": [], "count": 0, "provisional": True}},
                estimates={"requests": {}, "duration_range": None},
                warnings=warns,
            )
            plan.plan_hash = compute_plan_hash(plan)
            return plan

        # ── Date window ──
        if from_date is None or to_date is None:
            wm = latest_local_ohlcv_date(conn)
        fd = from_date or wm
        td = to_date or latest

        # ── Trading days ──
        trading_days = trading_days_for_window(conn, fd, td)

        # ── Missing cells ──
        success_rows = conn.execute(
            f"""SELECT ticker, date, source_type FROM source_checkpoints
                WHERE status = 'success'
                  AND ticker IN ({','.join('?' for _ in universe_tickers)})
                  AND source_type IN ({','.join('?' for _ in srcs)})
                  AND date >= ? AND date <= ?""",
            universe_tickers + srcs + [fd, td],
        ).fetchall()
        success_cells: set[tuple[str, str, str]] = {
            (r[0], r[1], r[2]) for r in success_rows
        }

        missing: list[tuple[str, str, str]] = []
        for ticker in universe_tickers:
            for tday in trading_days:
                for src in srcs:
                    cell = (ticker, tday, src)
                    if cell not in success_cells:
                        missing.append(cell)

        # ── Stage 1 (market): filter OHLCV sources ──
        market_cells = [c for c in missing if c[2] in ("polygon_ohlcv",)]
        evidence_cells = [c for c in missing if c[2] not in ("polygon_ohlcv",)]

        # ── Estimates ──
        per_source: dict[str, int] = {}
        for _, _, src in missing:
            per_source[src] = per_source.get(src, 0) + 1

        # ── Assemble plan ──
        plan = UpdatePlan(
            created_at=datetime.now(timezone.utc).isoformat(),
            db_path=logical_path,
            db_sha256=db_sha,
            db_user_version=db_ver,
            config={
                "tickers": universe_tickers,
                "sources": srcs,
                "from_date": fd,
                "to_date": td,
                "reference_today": ref_today_str,
            },
            universe={"tickers": universe_tickers, "provenance": provenance},
            reference_today=ref_today_str,
            latest_closed_session=latest,
            stages={
                "market": {"cells": market_cells, "count": len(market_cells)},
                "evidence": {"cells": evidence_cells, "count": len(evidence_cells),
                             "provisional": True},
            },
            estimates={"requests": per_source, "duration_range": None},
            warnings=warns,
        )
        plan.plan_hash = compute_plan_hash(plan)
        return plan
    finally:
        conn.close()
