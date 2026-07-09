"""Context-managed SQLite trace writer with contextvar-based activation."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator
from uuid import uuid4

from catalyst_data.config import db_path as default_db_path

from catalyst_agents.trace.schema import init_trace_db

_CURRENT_WRITER: ContextVar["TraceWriter | None"] = ContextVar("catalyst_trace_writer", default=None)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_current_writer() -> "TraceWriter | None":
    return _CURRENT_WRITER.get()


@contextmanager
def activate_writer(writer: "TraceWriter") -> Iterator["TraceWriter"]:
    token = _CURRENT_WRITER.set(writer)
    try:
        yield writer
    finally:
        _CURRENT_WRITER.reset(token)


class TraceWriter:
    """Persist run-level and node-level trace records to SQLite."""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        trace_id: str | None = None,
        db_path: Path | str | None = None,
        ticker: str | None = None,
        trade_date: str | None = None,
        config: str = "mcj_full",
    ) -> None:
        self.run_id = run_id or uuid4().hex
        self.trace_id = trace_id or uuid4().hex
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.ticker = ticker
        self.trade_date = trade_date
        self.config = config
        self.started_at = _utc_now()
        self._run_started_monotonic = time.perf_counter()
        self._event_seq = 0
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> "TraceWriter":
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        init_trace_db(self._conn)
        existing = self._conn.execute(
            "SELECT status, trace_id FROM agent_runs WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()
        if existing is not None:
            existing_trace_id = existing[1]
            if existing_trace_id:
                self.trace_id = existing_trace_id
            self._conn.execute(
                """
                UPDATE agent_runs
                SET status = ?,
                    started_at = ?,
                    trace_id = COALESCE(trace_id, ?),
                    ticker = COALESCE(ticker, ?),
                    trade_date = COALESCE(trade_date, ?),
                    config = COALESCE(config, ?)
                WHERE run_id = ?
                """,
                ("RUNNING", self.started_at, self.trace_id, self.ticker, self.trade_date, self.config, self.run_id),
            )
        else:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO agent_runs
                    (run_id, trace_id, ticker, trade_date, status, started_at, config)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (self.run_id, self.trace_id, self.ticker, self.trade_date, "RUNNING", self.started_at, self.config),
            )
        self._conn.commit()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self._finalize_status(
                status="SYSTEM_ERROR",
                error_type="system_error",
                error_message=str(exc),
                total_cost_usd=None,
                model_id_per_role=None,
            )
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        return False

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("TraceWriter is not active")
        return self._conn

    def event(
        self,
        *,
        node: str,
        started_at: str,
        ended_at: str,
        latency_ms: int,
        model_id: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None,
        decision: str | None,
        error_type: str | None,
        error_message: str | None,
        status_before: str | None,
        status_after: str | None,
    ) -> int:
        self._event_seq += 1
        self.conn.execute(
            """
            INSERT INTO trace_events
                (run_id, trace_id, event_seq, node, started_at, ended_at, latency_ms,
                 model_id, input_tokens, output_tokens, cost_usd, decision, error_type,
                 error_message, status_before, status_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.run_id,
                self.trace_id,
                self._event_seq,
                node,
                started_at,
                ended_at,
                latency_ms,
                model_id,
                input_tokens,
                output_tokens,
                cost_usd,
                decision,
                error_type,
                error_message,
                status_before,
                status_after,
            ),
        )
        self.conn.commit()
        return self._event_seq

    def complete(self, final_state: dict[str, Any]) -> None:
        breakdown = final_state.get("cost_breakdown", []) or []
        model_ids = {
            entry.get("node"): entry.get("model_id")
            for entry in breakdown
            if entry.get("node") and entry.get("model_id")
        }
        self._finalize_status(
            status=_status_name(final_state.get("output_status")) or "UNKNOWN",
            error_type=final_state.get("error_type"),
            error_message=final_state.get("validation_error"),
            total_cost_usd=final_state.get("total_cost_usd"),
            model_id_per_role=json.dumps(model_ids, sort_keys=True) if model_ids else None,
        )

    def _finalize_status(
        self,
        *,
        status: str,
        error_type: str | None,
        error_message: str | None,
        total_cost_usd: float | None,
        model_id_per_role: str | None,
    ) -> None:
        ended_at = _utc_now()
        total_latency_ms = int((time.perf_counter() - self._run_started_monotonic) * 1000)
        self.conn.execute(
            """
            UPDATE agent_runs
            SET status = ?,
                ended_at = ?,
                total_latency_ms = ?,
                total_cost_usd = COALESCE(?, total_cost_usd),
                model_id_per_role = COALESCE(?, model_id_per_role),
                error_type = ?,
                error_message = ?
            WHERE run_id = ?
            """,
            (
                status,
                ended_at,
                total_latency_ms,
                total_cost_usd,
                model_id_per_role,
                error_type,
                error_message,
                self.run_id,
            ),
        )
        self.conn.commit()


def _status_name(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)
