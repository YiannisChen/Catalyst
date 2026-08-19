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
    if isinstance(node, str) and len(node) > MAX_DISPLAY_LENGTH:
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
            agent_run = _row_to_dict(row, agent_columns)
            if agent_run.get("error_message") is not None:
                redacted_fields.append("agent_run.error_message")
                agent_run["error_message"] = None
            if agent_run.get("config") and isinstance(agent_run["config"], str):
                agent_run["config"] = agent_run["config"][:MAX_DISPLAY_LENGTH]
            result["agent_run"] = agent_run

        trace_columns = [
            column[1] for column in conn.execute("PRAGMA table_info(trace_events)").fetchall()
        ]
        for row in _read_rows(conn, "trace_events", run_id):
            event = _row_to_dict(row, trace_columns)
            if event.get("error_message") is not None:
                redacted_fields.append("trace_events.error_message")
                event["error_message"] = None
            result["trace_events"].append(event)

        artifact_columns = [
            column[1] for column in conn.execute("PRAGMA table_info(node_artifacts)").fetchall()
        ]
        for row in _read_rows(conn, "node_artifacts", run_id):
            artifact = _row_to_dict(row, artifact_columns)
            artifact_type = artifact.get("artifact_type") or "unknown"
            raw_payload = artifact.get("payload_json")
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
            assurance = _row_to_dict(row, assurance_columns)
            detail = _parse_payload(assurance.get("detail_json"))
            assurance["detail"] = _redact_node(
                detail, redacted_fields, truncated_fields, "$.run_assurance"
            )
            assurance.pop("detail_json", None)
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
