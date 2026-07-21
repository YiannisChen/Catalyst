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

from catalyst_data.config import HISTORICAL_START, TICKER_UNIVERSE
from catalyst_data.freshness import latest_local_ohlcv_date
from catalyst_data.trading_calendar import (
    CalendarCoverageError,
    calendar_trading_days,
    latest_closed_trading_day_for_date,
    trading_days_for_window,
)
from catalyst_data.storage.sqlite import FROZEN_PATHS



def _next_session(date_str: str) -> str:
    """Return the next calendar day after date_str (YYYY-MM-DD)."""
    from datetime import date as _date, timedelta
    dt = _date.fromisoformat(date_str)
    return (dt + timedelta(days=1)).isoformat()
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


class PlanDriftError(Exception):
    """Raised when the computed plan_hash differs from expected_plan_hash at execution start."""

    def __init__(self, expected: str, actual: str):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Plan drift detected: expected hash {expected[:16]}..., got {actual[:16]}..."
        )


def _check_plan_drift(plan: "UpdatePlan") -> None:
    """Raise PlanDriftError if plan.plan_hash != plan.expected_plan_hash.

    Called before execution writes or network calls.
    """
    if not plan.expected_plan_hash:
        return  # no expected hash set → first run, no drift check
    actual = plan.plan_hash or compute_plan_hash(plan)
    if actual != plan.expected_plan_hash:
        raise PlanDriftError(expected=plan.expected_plan_hash, actual=actual)

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
    expected_plan_hash: str = ""


# ---------------------------------------------------------------------------
# Canonical JSON helpers
# ---------------------------------------------------------------------------


def _canonical_json(plan: UpdatePlan) -> str:
    """Derive canonical JSON for hashing (excludes created_at and plan_hash)."""
    d = asdict(plan)
    d.pop("created_at", None)
    d.pop("plan_hash", None)
    d.pop("db_path", None)  # path varies; DB is identified by content SHA
    d.pop("expected_plan_hash", None)  # runtime drift-compare field; DB is identified by content SHA
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
    conn: sqlite3.Connection,
    tickers: list[str] | None,
    *,
    use_ohlcv_universe: bool = False,
) -> tuple[list[str], str, list[dict[str, str]]]:
    """Return (ticker_list, provenance_label, warnings).

    Resolution order:
    1. Explicit caller tickers → "explicit-config"
    2. Configured TICKER_UNIVERSE constant → "explicit-config" (default)
    3. OHLCV-derived with use_ohlcv_universe=True → "ohlcv-derived-fallback"
       + typed universe-fallback warning
    """
    warnings: list[dict[str, str]] = []
    if tickers:
        return sorted(tickers), "explicit-config", warnings

    if use_ohlcv_universe:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol"
        ).fetchall()
        derived = [r[0] for r in rows]
        warnings.append({
            "type": "universe-fallback",
            "message": (
                "Universe derived from ohlcv table.  This fallback is not "
                "acceptable for certified execution."
            ),
        })
        return derived, "ohlcv-derived-fallback", warnings

    return sorted(TICKER_UNIVERSE), "explicit-config", warnings


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
    use_ohlcv_universe: bool = False,
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
        universe_tickers, provenance, warns = _resolve_universe(conn, tickers, use_ohlcv_universe=use_ohlcv_universe)
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

        # ── Per-ticker date windows ──
        if from_date is None or to_date is None:
            # Resolve from_date per ticker: max(explicit from_date,
            #   that ticker's ohlcv watermark + 1, historical_start)
            wm_rows = conn.execute(
                f"SELECT symbol, MAX(date) FROM ohlcv "
                f"WHERE symbol IN ({','.join('?' for _ in universe_tickers)}) "
                f"GROUP BY symbol",
                universe_tickers,
            ).fetchall()
            wm_map: dict[str, str] = {r[0]: r[1] for r in wm_rows if r[1]}

            ticker_from: dict[str, str] = {}
            for t in universe_tickers:
                wm = wm_map.get(t)
                if wm:
                    ticker_from[t] = _next_session(wm)
                else:
                    ticker_from[t] = HISTORICAL_START

            # Global from_date = min(per-ticker), global to_date = latest session
            fd = from_date or min(ticker_from.values())
            td = to_date or latest
        else:
            fd = from_date
            td = to_date

        # ── Trading days ──
        trading_days = calendar_trading_days(fd, td)

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
                        if _should_plan_cell(
                            conn, ticker, tday, src,
                            reference_today=ref_today_str,
                        ):
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

# ---- W1-C: Checkpoint lifecycle constants (architect-approved) ----
SUCCESS_EMPTY_RECHECK_MAX = 1
SUCCESS_EMPTY_RECHECK_DELAY_DAYS = 1
SUCCESS_EMPTY_WINDOW_DAYS = 5
PERMANENT_FAILURE_THRESHOLD = 3
CHRONIC_TRANSIENT_THRESHOLD = 5


def _should_plan_cell(
    conn: sqlite3.Connection,
    ticker: str,
    date_str: str,
    source: str,
    *,
    reference_today: str,
) -> bool:
    """Return True if cell should be planned (not excluded by lifecycle).

    Filters: success_empty recheck limits, permanent-failure exclusion,
    chronic-transient warning, superseded-by-success detection.
    """
    from datetime import date as _date
    from catalyst_data.error_taxonomy import ErrorClass

    PERMANENT_CLASSES = {
        ErrorClass.AUTH.value, ErrorClass.PERMISSION_PAID.value,
        ErrorClass.MALFORMED_RESPONSE.value, ErrorClass.PARSE_FAILURE.value,
        ErrorClass.BUDGET_EXHAUSTED.value,
    }

    # Check for existing success checkpoint (fast path)
    success = conn.execute(
        "SELECT 1 FROM source_checkpoints "
        "WHERE ticker=? AND date=? AND source_type=? AND status='success'",
        (ticker, date_str, source),
    ).fetchone()
    if success:
        return False

    # success_empty: at most one recheck, delay, window
    empty_rows = conn.execute(
        "SELECT run_id, date FROM source_checkpoints "
        "WHERE ticker=? AND date=? AND source_type=? AND status='success_empty'",
        (ticker, date_str, source),
    ).fetchall()
    # Terminal after max rechecks: 0=first attempt, 1=recheck, 2=terminal
    if len(empty_rows) > SUCCESS_EMPTY_RECHECK_MAX:
        return False
    if empty_rows:
        ref = _date.fromisoformat(reference_today)
        chk = _date.fromisoformat(empty_rows[0][1])
        days_since = (ref - chk).days
        if days_since < SUCCESS_EMPTY_RECHECK_DELAY_DAYS:
            return False
        if days_since > SUCCESS_EMPTY_WINDOW_DAYS:
            return False

    # Permanent-class failures: exclude at threshold 3
    failed_rows = conn.execute(
        "SELECT error_class, run_id FROM source_checkpoints "
        "WHERE ticker=? AND date=? AND source_type=? AND status='failed'",
        (ticker, date_str, source),
    ).fetchall()
    permanent_count = 0
    transient_count = 0
    for ec_val, _rid in failed_rows:
        if ec_val in PERMANENT_CLASSES:
            permanent_count += 1
        else:
            transient_count += 1
    if permanent_count >= PERMANENT_FAILURE_THRESHOLD:
        return False

    # Chronic-transient: warn at threshold but do NOT exclude
    if transient_count >= CHRONIC_TRANSIENT_THRESHOLD:
        import logging
        _logger = logging.getLogger(__name__)
        _logger.warning(
            "Chronic transient failure: %s/%s/%s has %d transient failures",
            ticker, date_str, source, transient_count,
        )

    return True
