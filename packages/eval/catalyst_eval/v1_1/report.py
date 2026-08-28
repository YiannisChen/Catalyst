"""Stage-1 report write-once publication (M7-8).

Report JSON is serialized with the M7 canonical bytes and published
write-once: an absent target is created atomically, identical bytes are
idempotent, and differing bytes raise a conflict without overwrite. Markdown
is a deterministic rendering of the JSON payload and cannot supply new
facts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_eval.v1_1.loader import canonical_bytes

REPORT_SCHEMA_VERSION = "v1_1_stage1_report_v1"

_SECRET_PATTERNS = (
    "sk-",
    "Bearer ",
    "api_key",
    "authorization",
)


class ReportConflictError(RuntimeError):
    pass


def report_bytes(payload: Mapping[str, Any]) -> bytes:
    return canonical_bytes(payload)


def write_report_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Write-once canonical JSON publication with conflict detection."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = report_bytes(payload)
    if path.exists():
        existing = path.read_bytes()
        if existing == data:
            return  # idempotent
        raise ReportConflictError(
            f"refusing to overwrite existing report {path} with differing bytes"
        )
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def render_report_markdown(payload: Mapping[str, Any]) -> str:
    """Deterministic markdown rendering; never adds facts beyond JSON."""
    lines = [
        f"# {payload.get('schema_version', REPORT_SCHEMA_VERSION)}",
        "",
        f"- eval_id: `{payload.get('eval_id', '')}`",
        f"- dataset_id: `{payload.get('dataset_id', '')}`",
        f"- execution_head: `{payload.get('execution_head_sha8', '')}`",
        "",
        "## Hard gates",
    ]
    gates = payload.get("hard_gates") or {}
    for gate_id, passed in sorted(gates.items()):
        lines.append(f"- `{gate_id}`: {passed}")
    lines.append("")
    lines.append("## Counts")
    for key in ("coverage_limited_count", "model_limited_count", "case_count"):
        if key in payload:
            lines.append(f"- {key}: {payload[key]}")
    lines.append("")
    lines.append("_Deterministic rendering of the canonical report JSON._")
    return "\n".join(lines) + "\n"


def scan_report_for_secrets(
    payload: Mapping[str, Any],
    *,
    secret_values: Sequence[str] = (),
) -> list[str]:
    """Scan a report payload for secret-shaped text or configured values."""
    findings: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, str):
            lowered = value.lower()
            if any(pattern.lower() in lowered for pattern in _SECRET_PATTERNS):
                findings.append(f"{path}: secret-shaped text")
            if any(secret and secret in value for secret in secret_values):
                findings.append(f"{path}: configured secret value")

    walk(payload, "")
    return findings


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "ReportConflictError",
    "render_report_markdown",
    "report_bytes",
    "scan_report_for_secrets",
    "write_report_json",
]
