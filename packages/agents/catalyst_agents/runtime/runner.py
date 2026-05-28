from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable

from catalyst_agents.trace.schema import init_trace_db


DEFAULT_TIMEOUT_SECONDS = 300.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiveRunRunner:
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

    def run(self, run_id: str) -> dict[str, Any]:
        if self._is_cancelled(run_id):
            return {"run_id": run_id, "status": "CANCELLED", "sub_reason": "user_cancelled"}
        self._mark_running(run_id)
        state = self._load_state(run_id)
        timed_out = threading.Event()
        executor = ThreadPoolExecutor(max_workers=self.max_workers)
        future = executor.submit(self._invoke_graph, state, run_id, timed_out)
        try:
            result = future.result(timeout=self.timeout_seconds)
            if self._is_cancelled(run_id):
                return {"run_id": run_id, "status": "CANCELLED", "sub_reason": "user_cancelled"}
            if timed_out.is_set():
                return {"run_id": run_id, "status": "FAILED_SYSTEM", "sub_reason": "timeout"}
            return {"run_id": run_id, "status": "COMPLETED", "result": result}
        except TimeoutError:
            timed_out.set()
            future.cancel()
            self._mark_timeout(run_id)
            return {"run_id": run_id, "status": "FAILED_SYSTEM", "sub_reason": "timeout"}
        except Exception as exc:
            self._mark_system_error(run_id, str(exc))
            return {"run_id": run_id, "status": "FAILED_SYSTEM", "sub_reason": "system_error"}
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_trace_db(conn)
        return conn

    def _load_state(self, run_id: str) -> dict[str, Any]:
        conn = self._connect()
        row = conn.execute("SELECT ticker, trade_date, config FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        conn.close()
        if row is None:
            raise ValueError(f"run not found: {run_id}")
        config = json.loads(row["config"] or "{}")
        return {
            "ticker": row["ticker"],
            "trade_date": row["trade_date"],
            "query": config.get("query"),
            "price_move_pct": None,
            "query_ticker_raw": None,
            "ticker_consistent": None,
            "market_session_valid": True,
            "magnitude_plausible": None,
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "graded_evidence": [],
            "all_graded_chunks": [],
            "critic_reasoning": "",
            "critic_decision": None,
            "error_type": None,
            "causes": [],
            "summary_md": "",
            "grounding_rate": None,
            "output_status": None,
            "validation_error": None,
            "validator_attempts": 0,
            "phase": None,
            "router_edge": None,
            "router_reason": None,
            "expansions_used": 0,
            "max_expansions": 2,
            "current_layer": None,
            "retrieval_metadata": None,
            "cost_breakdown": [],
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "model_id": config.get("model") or "runtime-default",
            "config": config.get("config") or "mcj_full",
        }

    def _invoke_graph(self, state: dict[str, Any], run_id: str, timed_out: threading.Event) -> Any:
        previous_db_path = os.environ.get("CATALYST_DB_PATH")
        os.environ["CATALYST_DB_PATH"] = str(self.db_path)
        try:
            graph = self.graph_factory(model_id=state.get("model_id"))
            result = graph.invoke(state, run_id=run_id)
            # If timeout already happened in the parent thread, restore timeout terminal status.
            if timed_out.is_set():
                self._mark_timeout(run_id)
            return result
        finally:
            if previous_db_path is None:
                os.environ.pop("CATALYST_DB_PATH", None)
            else:
                os.environ["CATALYST_DB_PATH"] = previous_db_path

    def _is_cancelled(self, run_id: str) -> bool:
        conn = self._connect()
        row = conn.execute("SELECT status FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        conn.close()
        return row is not None and row["status"] == "CANCELLED"

    def _mark_running(self, run_id: str) -> None:
        conn = self._connect()
        conn.execute(
            "UPDATE agent_runs SET status = ?, started_at = ? WHERE run_id = ? AND status = 'QUEUED'",
            ("RUNNING", _utc_now(), run_id),
        )
        conn.commit()
        conn.close()

    def _mark_timeout(self, run_id: str) -> None:
        conn = self._connect()
        conn.execute(
            """
            UPDATE agent_runs
            SET status = ?, ended_at = ?, error_type = ?, error_message = ?
            WHERE run_id = ?
            """,
            ("FAILED_SYSTEM", _utc_now(), "timeout", "Run exceeded the configured timeout.", run_id),
        )
        conn.commit()
        conn.close()

    def _mark_system_error(self, run_id: str, message: str) -> None:
        conn = self._connect()
        conn.execute(
            """
            UPDATE agent_runs
            SET status = ?, ended_at = ?, error_type = ?, error_message = ?
            WHERE run_id = ?
            """,
            ("FAILED_SYSTEM", _utc_now(), "system_error", message, run_id),
        )
        conn.commit()
        conn.close()
