"""Context-managed SQLite trace writer with contextvar-based activation."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator
from uuid import uuid4

from catalyst_agents.trace.schema import init_trace_db
from catalyst_agents.runtime.assurance.checks import canonical_created_at, compute_source_flags, run_all_checks
from catalyst_agents.runtime.assurance.record import RunAssuranceRecord

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
        self.db_path = Path(db_path) if db_path is not None else Path(os.environ.get("CATALYST_TRACE_DB_PATH", ".catalyst/agent_trace.db"))
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
        self.persist_assurance(final_state)

    def persist_assurance(self, final_state: dict[str, Any]) -> None:
        evidence = final_state.get("retrieved_chunks") or []
        hypotheses = final_state.get("hypotheses") or []
        event_rows = self.conn.execute(
            "SELECT event_seq, trace_id, node, model_id, error_type FROM trace_events WHERE run_id = ? ORDER BY event_seq",
            (self.run_id,),
        ).fetchall()
        nodes = [row[2] for row in event_rows]
        event_sequences = [int(row[0]) for row in event_rows]
        trace_complete = bool(event_rows) and event_sequences == list(range(1, len(event_rows) + 1)) and all(
            row[1] == self.trace_id for row in event_rows
        )
        allowed_transitions = {
            "context_builder": {"miner"},
            "miner": {"critic", "baseline_prepare_evidence"},
            "critic": {"decision_router"},
            "decision_router": {"judge", "expand_macro", "insufficient_handler", "system_error_handler"},
            "expand_macro": {"miner"},
            "baseline_prepare_evidence": {"judge"},
            "judge": {"validator"},
            "validator": {"finalizer"},
            "insufficient_handler": {"finalizer"},
            "system_error_handler": {"finalizer"},
        }
        legal_path_ok = all(right in allowed_transitions.get(left, set()) for left, right in zip(nodes, nodes[1:]))
        output_status = _status_name(final_state.get("output_status")) or "SYSTEM_ERROR"
        if output_status == "SYSTEM_ERROR" and any(row[4] for row in event_rows):
            legal_path_ok = legal_path_ok and True
        else:
            legal_path_ok = legal_path_ok and bool(nodes) and nodes[-1] == "finalizer"

        retrieval_corpus_ids = {chunk.get("corpus_manifest_id") for chunk in evidence if chunk.get("corpus_manifest_id")}
        retrieval_index_ids = {chunk.get("index_manifest_id") for chunk in evidence if chunk.get("index_manifest_id")}
        metadata = final_state.get("retrieval_metadata")
        retrieval_corpus_id = next(iter(retrieval_corpus_ids), None) if len(retrieval_corpus_ids) <= 1 else "__multiple__"
        if retrieval_corpus_id is None and metadata is not None:
            retrieval_corpus_id = getattr(metadata, "requested_manifest_id", None)
        retrieval_index_id = next(iter(retrieval_index_ids), None) if len(retrieval_index_ids) <= 1 else "__multiple__"

        supporting_evidence: list[dict[str, Any]] = []
        for hypothesis in hypotheses:
            for item in hypothesis.get("supporting_evidence", []) or []:
                available_at = item.get("available_at")
                supporting_evidence.append({
                    **item,
                    "valid_support": bool(
                        float(item.get("relevance", 0.0) or 0.0) > 0.5
                        and item.get("temporal_match") is True
                        and available_at
                        and final_state.get("cutoff")
                        and available_at <= final_state["cutoff"]
                    ),
                })
        created_at = canonical_created_at()
        artifacts = {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "output_status": output_status,
            "cutoff": final_state.get("cutoff"),
            "cutoff_observations": tuple(
                final_state.get(name)
                for name in ("context_cutoff", "retrieval_cutoff", "validator_cutoff")
                if final_state.get(name)
            ),
            "citations": [
                evidence_id
                for hypothesis in hypotheses
                for evidence_id in hypothesis.get("supporting_evidence_ids", [])
            ],
            "judge_visible_ids": (
                list(final_state["judge_evidence"].keys())
                if final_state.get("judge_evidence") is not None else None
            ),
            "gate_results": [
                (h.get("cause_label"), h.get("prerequisite_gate_passed"), h.get("prerequisite_gate_reason"))
                for h in final_state.get("hypotheses", []) or []
            ] or [("not_applicable", True, "no hypotheses")],
            "legal_path_ok": legal_path_ok,
            "trace_complete": trace_complete,
            "corpus_manifest_id": final_state.get("corpus_manifest_id"),
            "index_manifest_id": final_state.get("index_manifest_id"),
            "retrieval_corpus_manifest_id": retrieval_corpus_id,
            "retrieval_index_manifest_id": retrieval_index_id,
            "model_ids": sorted({row[3] for row in event_rows if row[3]}),
            "prompt_versions": sorted({f"{row[2]}:b5" for row in event_rows if row[2] in {"critic", "judge", "validator"} and row[3]}),
            "retry_count": int(final_state.get("retry_count", 0) or 0),
            "repair_count": int(final_state.get("repair_count", final_state.get("validator_attempts", 0)) or 0),
            "budget_exhausted": bool(final_state.get("budget_exhausted", False)),
            "is_degraded": bool(final_state.get("is_degraded", False) or any(chunk.get("is_degraded") for chunk in evidence)),
            "context_artifact": final_state.get("context_artifact"),
            "checked_at": created_at,
            "evidence": [
                {"chunk_id": chunk.get("asset_id"), "source_class": chunk.get("source_class") or chunk.get("source_type")}
                for chunk in evidence
            ],
        }
        checks = run_all_checks(self.run_id, artifacts)
        record = RunAssuranceRecord(
            run_id=self.run_id,
            trace_id=self.trace_id,
            output_status=artifacts["output_status"] if artifacts["output_status"] in {"SUFFICIENT", "PARTIAL", "ABSTAIN", "SYSTEM_ERROR"} else "SYSTEM_ERROR",
            cutoff=artifacts["cutoff"] or "",
            corpus_manifest_id=artifacts["corpus_manifest_id"],
            index_manifest_id=artifacts["index_manifest_id"],
            model_ids=artifacts["model_ids"],
            prompt_versions=artifacts["prompt_versions"],
            checks=checks,
            source_support_flags=compute_source_flags(supporting_evidence),
            retry_count=artifacts["retry_count"],
            repair_count=artifacts["repair_count"],
            budget_exhausted=artifacts["budget_exhausted"],
            is_degraded=artifacts["is_degraded"],
            created_at=created_at,
        )
        persist_assurance_record(self.conn, record)

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
                total_cost_usd = ?,
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


class AssuranceConflictError(RuntimeError):
    pass


def _canonical_record_json(record: RunAssuranceRecord) -> str:
    return json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def persist_assurance_record(conn: sqlite3.Connection, record: RunAssuranceRecord) -> None:
    record_json = _canonical_record_json(record)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute("SELECT record_json FROM run_assurance WHERE run_id = ?", (record.run_id,)).fetchone()
        if existing is not None:
            if existing[0] != record_json:
                raise AssuranceConflictError(f"assurance record conflict for run_id={record.run_id}")
        else:
            conn.execute(
                "INSERT INTO run_assurance (run_id, schema_version, record_json, created_at) VALUES (?, ?, ?, ?)",
                (record.run_id, record.schema_version, record_json, record.created_at),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
