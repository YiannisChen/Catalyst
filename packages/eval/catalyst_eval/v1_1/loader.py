"""V1.1 GoldenCase dataset loader + provenance (M7-1).

Eval-owned loader (eval TSD §6; M7 execution lock). Validates the full Frozen
V1.1 GoldenCase contract, rejects legacy-only values, enforces ABSTAIN refusal
reasons, restricts MARKET_STRUCTURE_UNSUPPORTED to gap reason codes, verifies
the immutable dataset content hash against a dataset manifest, and derives
the dataset provenance string. Production packages never import this module.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_eval.v1_1.case import (
    GoldenCase,
    OracleStatusV1,
)

# The M7 canonical byte serialization (execution lock). All new M7 hashes use
# exactly this form: sorted keys, compact separators, no NaN, UTF-8.
def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def dataset_content_sha256(cases: Sequence[GoldenCase]) -> str:
    """Content hash over the ordered GoldenCase rows (never sorted)."""
    payload = [case.model_dump(mode="json") for case in cases]
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _reject_market_structure_as_taxonomy(value: object, *, field: str) -> None:
    if value == "MARKET_STRUCTURE_UNSUPPORTED":
        raise ValueError(
            f"MARKET_STRUCTURE_UNSUPPORTED is a gap reason code only and may "
            f"not be used as {field}"
        )


def _validate_case(case: GoldenCase) -> None:
    """Post-parse invariants beyond the pydantic field contract."""
    if case.oracle_status == "INSUFFICIENT":
        raise ValueError(
            f"case {case.case_id!r}: INSUFFICIENT is read-only legacy input; "
            f"V1.1 oracle_status must be one of {OracleStatusV1.__args__}"
        )
    if case.oracle_status == "ABSTAIN" and not case.expected_refusal_reason:
        raise ValueError(
            f"case {case.case_id!r}: ABSTAIN cases require expected_refusal_reason"
        )
    for label in case.acceptable_cause_labels:
        _reject_market_structure_as_taxonomy(
            label.cause_type, field=f"acceptable_cause_labels[].cause_type"
        )
    _reject_market_structure_as_taxonomy(
        case.expected_attribution_type, field="expected_attribution_type"
    )


def load_golden_cases(
    path: str | Path, *, manifest: Mapping[str, Any] | None = None
) -> list[GoldenCase]:
    """Load and strictly validate an ordered JSONL GoldenCase dataset.

    When ``manifest`` is supplied its ``dataset_content_sha256`` must match
    the loaded ordered rows; a mismatch fails closed before any row is used.
    """
    path = Path(path)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: row must be a JSON object")
            rows.append(row)

    seen: set[str] = set()
    cases: list[GoldenCase] = []
    for index, row in enumerate(rows):
        case = GoldenCase.model_validate(row)
        if case.case_id in seen:
            raise ValueError(f"duplicate case_id {case.case_id!r} at row {index + 1}")
        seen.add(case.case_id)
        _validate_case(case)
        cases.append(case)

    if not cases:
        raise ValueError(f"{path}: dataset contains no cases")

    if manifest is not None:
        declared = manifest.get("dataset_content_sha256")
        if declared is None:
            raise ValueError("dataset manifest is missing dataset_content_sha256")
        actual = dataset_content_sha256(cases)
        if actual != declared:
            raise ValueError(
                f"case file content hash mismatch: manifest={declared} "
                f"loaded={actual} (dataset content is immutable)"
            )
    return cases


def dataset_provenance(
    manifest: Mapping[str, Any], cases: Sequence[GoldenCase]
) -> str:
    """Deterministic provenance string: manifest id/version + content hash +
    per-case lineage summary. The case file is immutable by content hash."""
    payload = {
        "schema_version": manifest.get("schema_version"),
        "dataset_id": manifest.get("dataset_id"),
        "dataset_version": manifest.get("dataset_version"),
        "dataset_content_sha256": dataset_content_sha256(cases),
        "lineage": [
            {
                "case_id": case.case_id,
                "source": case.lineage.source,
                "adjudication_state": case.lineage.adjudication_state,
            }
            for case in cases
        ],
    }
    return canonical_bytes(payload).decode("utf-8")


__all__ = [
    "canonical_bytes",
    "dataset_content_sha256",
    "dataset_provenance",
    "load_golden_cases",
]
