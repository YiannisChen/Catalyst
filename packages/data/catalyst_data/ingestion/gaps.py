"""Category-level, data-only readiness and backfill ledger.

This ledger preserves source-level records and projects them into the six
contract categories. It does not decide whether a category is complete across
all required sources; callers must record the requested coverage and paging
state for each source task.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from catalyst_data.ingestion import source_categories
from catalyst_data.ingestion.time import timefields

GapStatus = Literal["OK", "EMPTY", "ERROR", "NOT_FETCHED"]
ProgressState = Literal["queued", "running", "retrying", "completed"]
ScopeKind = Literal["target", "peer", "supplementary", "reference"]
Reason = Literal["ERROR", "DISABLED", "deferred", "breaker"]

_STATUSES = {"OK", "EMPTY", "ERROR", "NOT_FETCHED"}
_PROGRESS = {"queued", "running", "retrying", "completed"}
_SCOPES = {"target", "peer", "supplementary", "reference"}
_REASONS = {"ERROR", "DISABLED", "deferred", "breaker"}
_TRANSITIONS = {
    "queued": {"queued", "running", "retrying", "completed"},
    "running": {"running", "retrying", "completed"},
    "retrying": {"running", "retrying", "completed"},
    "completed": {"completed"},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS category_gaps (
    id INTEGER PRIMARY KEY,
    session_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    source_id TEXT NOT NULL,
    category TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    mapping_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    requested_from TEXT,
    requested_until TEXT,
    coverage_from TEXT,
    coverage_until TEXT,
    cursor TEXT,
    pagination_complete INTEGER,
    logical_task_id TEXT,
    progress_state TEXT,
    target_count INTEGER,
    completed_count INTEGER,
    updated_at TEXT NOT NULL,
    UNIQUE (session_date, ticker, source_id, scope_kind, scope_id)
);
CREATE INDEX IF NOT EXISTS category_gaps_category_session
    ON category_gaps(category, session_date, ticker);
CREATE INDEX IF NOT EXISTS category_gaps_task
    ON category_gaps(logical_task_id);
"""


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT gap_ledger_schema")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO gap_ledger_schema")
        conn.execute("RELEASE gap_ledger_schema")
        raise
    else:
        conn.execute("RELEASE gap_ledger_schema")


@dataclass(frozen=True)
class GapRecord:
    id: int
    session_date: str
    ticker: str
    source_id: str
    category: str
    scope_kind: str
    scope_id: str
    mapping_version: int
    status: str
    reason: str | None
    requested_from: str | None
    requested_until: str | None
    coverage_from: str | None
    coverage_until: str | None
    cursor: str | None
    pagination_complete: bool | None
    logical_task_id: str | None
    progress_state: str | None
    target_count: int | None
    completed_count: int | None
    updated_at: str

    def to_public_dict(self) -> dict:
        """Return the category projection without internal source identities."""
        row = asdict(self)
        row.pop("source_id")
        row.pop("mapping_version")
        return row


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_time(name: str, value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timezone-aware ISO timestamp")
    try:
        timefields.stored(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a timezone-aware ISO timestamp") from None


def _time_value(value: str) -> datetime:
    return timefields.stored(value)


def _row_to_record(row: sqlite3.Row) -> GapRecord:
    values = dict(row)
    values["pagination_complete"] = (
        None if values["pagination_complete"] is None else bool(values["pagination_complete"])
    )
    return GapRecord(**values)


class GapLedger:
    """SQLite ledger for source-scoped records projected to readiness categories."""

    def __init__(
        self,
        database: str | Path | sqlite3.Connection,
        *,
        mapping: dict | None = None,
    ) -> None:
        if isinstance(database, sqlite3.Connection):
            self._conn = database
            self._owns_connection = False
        else:
            self._conn = sqlite3.connect(str(database))
            self._owns_connection = True
        self._conn.row_factory = sqlite3.Row
        # Loading validates the complete mapping against the source registry;
        # a caller-supplied mapping is held to the same validation path.
        self._mapping = source_categories.load() if mapping is None else mapping
        if mapping is not None:
            source_categories.validate(mapping, source_categories.source_registry.load())
        self._mapping_version = int(self._mapping["schema_version"])
        with _atomic(self._conn):
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    self._conn.execute(statement)
        if self._owns_connection:
            self._conn.commit()

    def record(
        self,
        *,
        session_date: str,
        ticker: str,
        source_id: str,
        scope_kind: ScopeKind,
        scope_id: str,
        status: GapStatus,
        category: str | None = None,
        reason: Reason | None = None,
        requested_from: str | None = None,
        requested_until: str | None = None,
        coverage_from: str | None = None,
        coverage_until: str | None = None,
        cursor: str | None = None,
        pagination_complete: bool | None = None,
        logical_task_id: str | None = None,
        progress_state: ProgressState | None = None,
        target_count: int | None = None,
        completed_count: int | None = None,
    ) -> GapRecord:
        """Insert or update one source/scope record and its logical task progress."""
        if not all(isinstance(value, str) and value.strip() for value in (session_date, ticker, source_id, scope_id)):
            raise ValueError("session_date, ticker, source_id, and scope_id are required")
        if scope_kind not in _SCOPES:
            raise ValueError("unknown scope kind")
        mapped_scope = None if scope_kind == "reference" else scope_kind
        resolved_category = source_categories.category_for(source_id, scope=mapped_scope, mapping=self._mapping)
        if category is not None and category != resolved_category:
            raise ValueError("category conflicts with source-category mapping")
        category = resolved_category

        if status not in _STATUSES:
            raise ValueError("unknown gap status")
        if reason is not None and reason not in _REASONS:
            raise ValueError("reason must be a contract reason class")
        if status == "ERROR" and reason != "ERROR":
            raise ValueError("ERROR status requires reason class ERROR")
        if status != "ERROR" and reason == "ERROR":
            raise ValueError("reason ERROR is only valid with ERROR status")
        if reason in {"DISABLED", "deferred", "breaker"} and status != "NOT_FETCHED":
            raise ValueError("DISABLED, deferred, and breaker reasons require NOT_FETCHED status")

        for name, value in (
            ("requested_from", requested_from),
            ("requested_until", requested_until),
            ("coverage_from", coverage_from),
            ("coverage_until", coverage_until),
        ):
            _validate_time(name, value)
        if (requested_from is None) != (requested_until is None):
            raise ValueError("requested coverage requires both bounds")
        if requested_from and requested_until and _time_value(requested_from) > _time_value(requested_until):
            raise ValueError("requested coverage bounds are reversed")
        if (coverage_from is None) != (coverage_until is None):
            raise ValueError("covered coverage requires both bounds")
        if coverage_from and coverage_until and _time_value(coverage_from) > _time_value(coverage_until):
            raise ValueError("coverage bounds are reversed")
        if coverage_from and requested_from and _time_value(coverage_from) < _time_value(requested_from):
            raise ValueError("coverage begins before requested coverage")
        if coverage_until and requested_until and _time_value(coverage_until) > _time_value(requested_until):
            raise ValueError("coverage ends after requested coverage")
        if pagination_complete is not None and type(pagination_complete) is not bool:
            raise ValueError("pagination_complete must be boolean or null")

        progress_values = (progress_state, target_count, completed_count)
        if logical_task_id is None:
            if any(value is not None for value in progress_values):
                raise ValueError("backfill progress requires a logical_task_id")
        else:
            if not isinstance(logical_task_id, str) or not logical_task_id.strip():
                raise ValueError("logical_task_id must be non-empty")
            if progress_state not in _PROGRESS:
                raise ValueError("unknown progress state")
            if type(target_count) is not int or type(completed_count) is not int:
                raise ValueError("backfill progress requires integer target_count and completed_count")
            if target_count < 0 or completed_count < 0 or completed_count > target_count:
                raise ValueError("completed_count must be between zero and target_count")

        identity = (session_date, ticker, source_id, scope_kind, scope_id)
        existing = self._conn.execute(
            "SELECT * FROM category_gaps WHERE session_date=? AND ticker=? AND source_id=? AND scope_kind=? AND scope_id=?",
            identity,
        ).fetchone()
        if existing is not None and logical_task_id is not None and existing["logical_task_id"] == logical_task_id:
            old_state = existing["progress_state"]
            if old_state and progress_state not in _TRANSITIONS[old_state]:
                raise ValueError("invalid progress state transition")
            old_target = existing["target_count"]
            if old_target is not None and target_count != old_target:
                raise ValueError("target_count cannot change for an existing logical task")
            if existing["completed_count"] is not None and completed_count < existing["completed_count"]:
                raise ValueError("completed_count cannot decrease for an existing logical task")

        updated_at = _utc_now()
        self._conn.execute(
            """INSERT INTO category_gaps (
                session_date, ticker, source_id, category, scope_kind, scope_id,
                mapping_version, status, reason, requested_from, requested_until,
                coverage_from, coverage_until, cursor, pagination_complete,
                logical_task_id, progress_state, target_count, completed_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_date, ticker, source_id, scope_kind, scope_id) DO UPDATE SET
                category=excluded.category,
                mapping_version=excluded.mapping_version,
                status=excluded.status,
                reason=excluded.reason,
                requested_from=excluded.requested_from,
                requested_until=excluded.requested_until,
                coverage_from=excluded.coverage_from,
                coverage_until=excluded.coverage_until,
                cursor=excluded.cursor,
                pagination_complete=excluded.pagination_complete,
                logical_task_id=excluded.logical_task_id,
                progress_state=excluded.progress_state,
                target_count=excluded.target_count,
                completed_count=excluded.completed_count,
                updated_at=excluded.updated_at""",
            (
                session_date,
                ticker,
                source_id,
                category,
                scope_kind,
                scope_id,
                self._mapping_version,
                status,
                reason,
                requested_from,
                requested_until,
                coverage_from,
                coverage_until,
                cursor,
                None if pagination_complete is None else int(pagination_complete),
                logical_task_id,
                progress_state,
                target_count,
                completed_count,
                updated_at,
            ),
        )
        if self._owns_connection:
            self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM category_gaps WHERE session_date=? AND ticker=? AND source_id=? AND scope_kind=? AND scope_id=?",
            identity,
        ).fetchone()
        return _row_to_record(row)

    def list_rows(
        self,
        *,
        category: str | None = None,
        session_date: str | None = None,
        ticker: str | None = None,
    ) -> list[GapRecord]:
        """Return internal records, optionally filtered by public category/session/ticker."""
        filters = []
        params: list[str] = []
        for field, value in (("category", category), ("session_date", session_date), ("ticker", ticker)):
            if value is not None:
                filters.append(field + "=?")
                params.append(value)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        rows = self._conn.execute(
            "SELECT * FROM category_gaps" + where + " ORDER BY session_date, ticker, category, source_id, scope_kind, scope_id",
            params,
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def public_rows(
        self,
        *,
        category: str | None = None,
        session_date: str | None = None,
        ticker: str | None = None,
    ) -> list[dict]:
        """Return category-level projections without provider/source identifiers."""
        return [
            row.to_public_dict()
            for row in self.list_rows(category=category, session_date=session_date, ticker=ticker)
        ]
