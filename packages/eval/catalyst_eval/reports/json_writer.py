"""JSON writer for frozen comparison artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_TOP_LEVEL_KEYS = (
    "schema_version",
    "header",
    "gates",
    "quality_metrics",
    "cost_latency",
    "per_case",
)


def normalize_comparison_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    missing = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in normalized]
    if missing:
        raise ValueError(f"comparison payload missing keys: {missing}")
    if normalized.get("schema_version") != "1.0":
        raise ValueError("comparison payload must use schema_version=1.0")
    normalized.setdefault("statistical_tests", {})
    return normalized


def write_comparison_json(payload: dict[str, Any], path: Path | str) -> Path:
    normalized = normalize_comparison_payload(payload)
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
    return out_path
