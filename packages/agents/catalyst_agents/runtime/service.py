from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable
from uuid import uuid4

from catalyst_agents.runtime.runner import DEFAULT_TIMEOUT_SECONDS, LiveRunRunner
from catalyst_agents.runtime.status import failure_payload_from_rows, normalize_run_status
from catalyst_agents.runtime.validation import validate_live_run_request
from catalyst_agents.trace.artifacts import read_node_artifacts
from catalyst_agents.trace.schema import init_trace_db


_TERMINAL_STATUSES = {
    "SUFFICIENT",
    "SUCCEEDED",
    "PARTIAL",
    "INSUFFICIENT",
    "SYSTEM_ERROR",
    "FAILED_SYSTEM",
    "FAILED_REQUEST",
}
_NEXT_NODE = {
    "miner": "critic",
    "critic": "decision_router",
    "decision_router": "judge",
    "judge": "validator",
    "validator": "finalizer",
    "baseline_prepare_evidence": "judge",
    "expand_macro": "miner",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiveRunService:
    def __init__(
        self,
        *,
        db_path: Path | str,
        graph_factory: Callable[..., Any],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_workers: int = 1,
    ) -> None:
        self.db_path = Path(db_path)
        self.graph_factory = graph_factory
        self.timeout_seconds = timeout_seconds
        self.max_workers = max_workers

    def create_run(
        self,
        *,
        ticker: str,
        trade_date: str,
        query: Any = None,
        model: str | None = None,
        config: str = "mcj_full",
    ) -> dict[str, Any]:
        conn = self._connect()
        validation = validate_live_run_request(conn, ticker=ticker, trade_date=trade_date, query=query)
        run_id = uuid4().hex
        trace_id = uuid4().hex
        queued_at = _utc_now()
        if not validation["ok"]:
            failure = validation["failure"]
            conn.execute(
                """
                INSERT INTO agent_runs
                    (run_id, trace_id, ticker, trade_date, status, queued_at, ended_at, config, error_type, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    trace_id,
                    str(ticker).upper() if isinstance(ticker, str) else None,
                    trade_date if isinstance(trade_date, str) else None,
                    "FAILED_REQUEST",
                    queued_at,
                    queued_at,
                    json.dumps({"query": query, "model": model, "config": config}, sort_keys=True),
                    failure["sub_reason"],
                    failure["message"],
                ),
            )
            conn.commit()
            conn.close()
            return {"run_id": run_id, "status": "FAILED_REQUEST", "failure": failure}

        payload = {
            "query": validation["query"],
            "model": model,
            "config": config,
        }
        conn.execute(
            """
            INSERT INTO agent_runs
                (run_id, trace_id, ticker, trade_date, status, queued_at, started_at, config)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                run_id,
                trace_id,
                validation["ticker"],
                validation["trade_date"],
                "QUEUED",
                queued_at,
                json.dumps(payload, sort_keys=True),
            ),
        )
        conn.commit()
        conn.close()
        return {"run_id": run_id, "status": "QUEUED", "failure": None}

    def run_next(self) -> dict[str, Any] | None:
        conn = self._connect()
        # P2 assumption: single runner process polls queue; this selection is not an atomic multi-worker claim.
        row = conn.execute(
            "SELECT run_id FROM agent_runs WHERE status = 'QUEUED' ORDER BY queued_at ASC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is None:
            return None
        runner = LiveRunRunner(
            db_path=self.db_path,
            graph_factory=self.graph_factory,
            timeout_seconds=self.timeout_seconds,
            max_workers=self.max_workers,
        )
        return runner.run(row["run_id"])

    def run_one(self, run_id: str) -> dict[str, Any]:
        conn = self._connect()
        row = conn.execute("SELECT run_id, status FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        conn.close()
        if row is None:
            return {
                "ok": False,
                "run_id": run_id,
                "status": "NOT_FOUND",
                "failure": {"sub_reason": "run_not_found", "message": "Run not found."},
            }
        if row["status"] != "QUEUED":
            return {
                "ok": False,
                "run_id": run_id,
                "status": row["status"],
                "failure": {"sub_reason": "run_not_queued", "message": "Run is not in QUEUED state."},
            }
        runner = LiveRunRunner(
            db_path=self.db_path,
            graph_factory=self.graph_factory,
            timeout_seconds=self.timeout_seconds,
            max_workers=self.max_workers,
        )
        result = runner.run(run_id)
        return {"ok": True, **result}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        run_row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        if run_row is None:
            conn.close()
            return None
        event_row = conn.execute(
            "SELECT * FROM trace_events WHERE run_id = ? AND error_type IS NOT NULL ORDER BY event_seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        last_event = conn.execute(
            "SELECT event_seq, node FROM trace_events WHERE run_id = ? ORDER BY event_seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        conn.close()
        normalized = normalize_run_status(run_row["status"], sub_reason=run_row["error_type"])
        last_node = last_event["node"] if last_event is not None else None
        failure = failure_payload_from_rows(run_row=run_row, trace_event_row=event_row)
        return {
            "run_id": run_id,
            "status": normalized["status"],
            "sub_reason": normalized["sub_reason"],
            "failure": failure,
            "last_completed_node": last_node,
            "predicted_next_node": None if normalized["status"] in _TERMINAL_STATUSES else _NEXT_NODE.get(last_node, "miner"),
        }

    def get_events(self, run_id: str, *, after_seq: int | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        query = """
            SELECT run_id, trace_id, event_seq, node, started_at, ended_at, latency_ms,
                   model_id, input_tokens, output_tokens, cost_usd, decision, error_type,
                   error_message, status_before, status_after
            FROM trace_events
            WHERE run_id = ?
        """
        params: list[Any] = [run_id]
        if after_seq is not None:
            query += " AND event_seq > ?"
            params.append(after_seq)
        query += " ORDER BY event_seq ASC"
        rows = conn.execute(query, tuple(params)).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_artifacts(
        self,
        run_id: str,
        *,
        event_seq: int | None = None,
        artifact_type: str | None = None,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = read_node_artifacts(conn, run_id=run_id, event_seq=event_seq, artifact_type=artifact_type)
        conn.close()
        return rows

    def retry_run(self, run_id: str, *, model: str | None = None) -> dict[str, Any]:
        conn = self._connect()
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            conn.close()
            return {"ok": False, "failure": {"sub_reason": "run_not_found", "message": "Run not found."}}
        if row["status"] in {"QUEUED", "RUNNING"}:
            conn.close()
            return {"ok": False, "failure": {"sub_reason": "run_not_terminal", "message": "Only terminal runs can be retried."}}
        config = json.loads(row["config"] or "{}")
        conn.close()
        created = self.create_run(
            ticker=row["ticker"],
            trade_date=row["trade_date"],
            query=config.get("query"),
            model=model if model is not None else config.get("model"),
            config=config.get("config") or "mcj_full",
        )
        if created.get("status") == "QUEUED":
            conn = self._connect()
            conn.execute(
                "INSERT INTO run_links (run_id, parent_run_id, link_type) VALUES (?, ?, ?)",
                (created["run_id"], run_id, "retry"),
            )
            conn.commit()
            conn.close()
            return {"ok": True, "run_id": created["run_id"]}
        return {"ok": False, "failure": created.get("failure")}

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_trace_db(conn)
        return conn
