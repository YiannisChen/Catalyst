"""Sealed legacy MCJ comparison adapter (M1-5).

Reads ``agent_runs``, ``trace_events``, ``node_artifacts``, and
``run_assurance`` rows into one versioned dict with semantic redaction:

- reasoning / chain-of-thought keys are always redacted;
- ``raw_llm_response`` and unknown raw/internal artifacts are always redacted
  regardless of payload length;
- only explicitly allowlisted public/display fields are retained, bounded to
  4,000 characters;
- redacted field names are recorded without their values.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

ADAPTER_SCHEMA_VERSION = "legacy_mcj_v1"
MAX_DISPLAY_LENGTH = 4000

_REASONING = re.compile(r"(?i)reasoning|chain[_-]?of[_-]?thought")
_SECRET = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|credential|authorization|_key\b)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(\bsk-[a-z0-9_-]+|\b(?:api[_-]?key|token|secret|password|"
    r"credential|authorization)\s*[:=]\s*\S+)"
)

_AGENT_RUN_FIELDS = frozenset(
    {
        "run_id", "trace_id", "ticker", "trade_date", "status", "queued_at",
        "started_at", "ended_at", "total_latency_ms", "total_cost_usd",
        "model_id_per_role", "error_type", "error_message",
    }
)
_TRACE_EVENT_FIELDS = frozenset(
    {
        "run_id", "trace_id", "event_seq", "node", "started_at", "ended_at",
        "latency_ms", "model_id", "input_tokens", "output_tokens", "cost_usd",
        "decision", "error_type", "error_message", "status_before", "status_after",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {"run_id", "event_seq", "node", "artifact_type", "created_at"}
)
_ASSURANCE_FIELDS = frozenset({"run_id", "schema_version", "created_at"})

# Artifacts whose payloads are always redacted as raw/internal model state.
_RAW_ARTIFACT_TYPES = frozenset(
    {"raw_llm_response", "state_snapshot", "error_snapshot"}
)
# Artifacts whose payloads may carry bounded public/display fields.
_DISPLAY_ARTIFACT_TYPES = frozenset(
    {
        "graded_evidence",
        "all_graded_chunks",
        "retrieved_chunks",
        "reranked_chunks",
        "critic_decision",
    }
)
# Explicit allowlist of public/display field names retained from display payloads.
_ALLOWED_PUBLIC_FIELDS = frozenset(
    {
        "asset_id",
        "headline",
        "rank",
        "score",
        "rerank_score",
        "reference_date",
        "source_type",
        "ticker",
        "snippet",
        "text",
        "title",
        "publisher_name",
        "available_at",
        "chunk_id",
        "document_id",
        "sufficiency",
        "next_action",
        "magnitude_coverage",
        "stop_reason",
        "retrieved_chunk_count",
        "graded_evidence_count",
        "all_graded_chunks_count",
        "causes_count",
        "total_cost_usd",
        "total_tokens",
        "current_layer",
        "expansions_used",
        "max_expansions",
        "layers_attempted",
        "validator_attempts",
        "error_type",
        "phase",
        "router_edge",
        "items",
        "chunks",
        "decision",
        "summary_md",
        "status",
        "schema_version",
        "run_id",
        "trace_id",
        "output_status",
        "cutoff",
        "corpus_manifest_id",
        "index_manifest_id",
        "model_ids",
        "prompt_versions",
        "checks",
        "source_support_flags",
        "retry_count",
        "repair_count",
        "budget_exhausted",
        "is_degraded",
        "created_at",
        "check_name",
        "detail",
        "checked_at",
    }
)


def _redact_node(
    node: Any,
    redacted_fields: list[str],
    truncated_fields: list[str],
    path: str = "$",
) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            child_path = f"{path}.{key}"
            if _REASONING.search(str(key)) or _SECRET.search(str(key)):
                redacted_fields.append(key)
                continue
            if key not in _ALLOWED_PUBLIC_FIELDS:
                redacted_fields.append(key)
                continue
            out[key] = _redact_node(value, redacted_fields, truncated_fields, child_path)
        return out
    if isinstance(node, list):
        return [
            _redact_node(item, redacted_fields, truncated_fields, f"{path}[{index}]")
            for index, item in enumerate(node)
        ]
    if isinstance(node, str):
        if _SECRET_VALUE.search(node):
            redacted_fields.append(path)
            return None
        if len(node) > MAX_DISPLAY_LENGTH:
            truncated_fields.append(path)
            return node[:MAX_DISPLAY_LENGTH]
    return node


def _redact_payload(
    payload: Any,
    artifact_type: str,
    redacted_fields: list[str],
    truncated_fields: list[str],
) -> Any:
    if artifact_type in _RAW_ARTIFACT_TYPES or artifact_type not in _DISPLAY_ARTIFACT_TYPES:
        redacted_fields.append(f"{artifact_type}.payload")
        return None
    return _redact_node(payload, redacted_fields, truncated_fields)


def _redact_unstructured(
    node: Any,
    redacted_fields: list[str],
    truncated_fields: list[str],
    path: str,
) -> Any:
    """Preserve public unstructured metadata while removing semantic secrets."""
    if isinstance(node, dict):
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            child_path = f"{path}.{key}"
            if _REASONING.search(str(key)) or _SECRET.search(str(key)):
                redacted_fields.append(child_path)
                continue
            cleaned[key] = _redact_unstructured(
                value, redacted_fields, truncated_fields, child_path
            )
        return cleaned
    if isinstance(node, list):
        return [
            _redact_unstructured(
                value, redacted_fields, truncated_fields, f"{path}[{index}]"
            )
            for index, value in enumerate(node)
        ]
    if isinstance(node, str):
        if _SECRET_VALUE.search(node):
            redacted_fields.append(path)
            return None
        if len(node) > MAX_DISPLAY_LENGTH:
            truncated_fields.append(path)
            return node[:MAX_DISPLAY_LENGTH]
    return node


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _read_rows(conn: sqlite3.Connection, table: str, run_id: str) -> list[tuple]:
    try:
        return conn.execute(
            f"SELECT * FROM {table} WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _row_to_dict(row: tuple, columns: list[str]) -> dict:
    return {name: value for name, value in zip(columns, row)}


def _public_row(
    row: dict[str, Any], allowed: frozenset[str], prefix: str, redacted_fields: list[str]
) -> dict[str, Any]:
    public: dict[str, Any] = {}
    for key, value in row.items():
        if key not in allowed:
            redacted_fields.append(f"{prefix}.{key}")
            continue
        if isinstance(value, str) and _SECRET_VALUE.search(value):
            redacted_fields.append(f"{prefix}.{key}")
            public[key] = None
            continue
        public[key] = value
    return public


def read_legacy_run_artifacts(db_path: str | Path | None, run_id: str) -> dict[str, Any]:
    """Read and semantically redact one legacy MCJ run's persisted artifacts."""
    redacted_fields: list[str] = []
    truncated_fields: list[str] = []
    result: dict[str, Any] = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "run_id": run_id,
        "agent_run": None,
        "trace_events": [],
        "node_artifacts": [],
        "run_assurance": None,
        "redacted_fields": redacted_fields,
        "truncated_fields": truncated_fields,
    }
    if db_path is None:
        return result
    path = Path(db_path)
    if not path.is_file():
        return result

    conn = _open_readonly(path)
    try:
        agent_columns = [
            column[1] for column in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
        ]
        for row in _read_rows(conn, "agent_runs", run_id):
            agent_run = _public_row(
                _row_to_dict(row, agent_columns),
                _AGENT_RUN_FIELDS,
                "agent_run",
                redacted_fields,
            )
            if agent_run.get("error_message") is not None:
                redacted_fields.append("agent_run.error_message")
                agent_run["error_message"] = None
            if agent_run.get("model_id_per_role") is not None:
                agent_run["model_id_per_role"] = _redact_unstructured(
                    _parse_payload(agent_run["model_id_per_role"]),
                    redacted_fields,
                    truncated_fields,
                    "$.agent_run.model_id_per_role",
                )
            result["agent_run"] = agent_run

        trace_columns = [
            column[1] for column in conn.execute("PRAGMA table_info(trace_events)").fetchall()
        ]
        for row in _read_rows(conn, "trace_events", run_id):
            event = _public_row(
                _row_to_dict(row, trace_columns),
                _TRACE_EVENT_FIELDS,
                "trace_events",
                redacted_fields,
            )
            if event.get("error_message") is not None:
                redacted_fields.append("trace_events.error_message")
                event["error_message"] = None
            if event.get("decision") is not None:
                event["decision"] = _redact_unstructured(
                    _parse_payload(event["decision"]),
                    redacted_fields,
                    truncated_fields,
                    "$.trace_events.decision",
                )
            result["trace_events"].append(event)

        artifact_columns = [
            column[1] for column in conn.execute("PRAGMA table_info(node_artifacts)").fetchall()
        ]
        for row in _read_rows(conn, "node_artifacts", run_id):
            raw_artifact = _row_to_dict(row, artifact_columns)
            artifact = _public_row(
                raw_artifact,
                _ARTIFACT_FIELDS,
                "node_artifacts",
                redacted_fields,
            )
            artifact_type = artifact.get("artifact_type") or "unknown"
            raw_payload = raw_artifact.get("payload_json")
            artifact["payload"] = _redact_payload(
                _parse_payload(raw_payload),
                artifact_type,
                redacted_fields,
                truncated_fields,
            )
            artifact.pop("payload_json", None)
            result["node_artifacts"].append(artifact)

        assurance_columns = [
            column[1] for column in conn.execute("PRAGMA table_info(run_assurance)").fetchall()
        ]
        for row in _read_rows(conn, "run_assurance", run_id):
            raw_assurance = _row_to_dict(row, assurance_columns)
            assurance = _public_row(
                raw_assurance,
                _ASSURANCE_FIELDS,
                "run_assurance",
                redacted_fields,
            )
            assurance["record"] = _redact_node(
                _parse_payload(raw_assurance.get("record_json")),
                redacted_fields,
                truncated_fields,
                "$.run_assurance.record",
            )
            result["run_assurance"] = assurance
    finally:
        conn.close()
    return result


def _parse_payload(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


__all__ = [
    "ADAPTER_SCHEMA_VERSION",
    "MAX_DISPLAY_LENGTH",
    "read_legacy_run_artifacts",
]
