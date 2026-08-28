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


def case_list_sha256(case_ids: Sequence[str]) -> str:
    """Hash of the ordered case_id array; the list order is never sorted."""
    return hashlib.sha256(canonical_bytes(list(case_ids))).hexdigest()


STAGE1_MANIFEST_SCHEMA = "v1_1_stage1_dataset_manifest_v1"
STAGE1_STRATIFICATION_SCHEMA = "v1_1_stage1_stratification_v1"
LEGACY_PARENT_PREFIX = "legacy:"
ALLOWED_LEGACY_PARENT_IDS = frozenset(
    {
        "g006", "g013", "g017", "g024", "g041", "g007",
        "h001", "h004", "h005", "h007", "pre_b6",
    }
)


def _require_approval(manifest: Mapping[str, Any]) -> None:
    authority = manifest.get("approval_authority")
    approved_at = manifest.get("approved_at")
    adjudication = manifest.get("adjudication_state")
    if adjudication != "resolved":
        raise ValueError(
            f"dataset adjudication_state must be 'resolved', got {adjudication!r}"
        )
    if not authority or not approved_at:
        raise ValueError(
            "dataset approval is missing/pending: approval_authority and "
            "approved_at are required; human approval is Q-011 gated"
        )


def _check_no_answer_hint_leakage(case: GoldenCase) -> None:
    """No acceptable cause label may appear verbatim in the question text."""
    lowered_question = case.question.casefold()
    for label in case.acceptable_cause_labels:
        if label.label and label.label.casefold() in lowered_question:
            raise ValueError(
                f"case {case.case_id!r}: acceptable cause label appears "
                f"verbatim in question (answer-hint leakage)"
            )


def validate_stage1_dataset_manifest(
    manifest: Mapping[str, Any],
    cases: Sequence[GoldenCase],
    *,
    stratification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the authoritative Stage-1 dataset manifest (M7-2 contract).

    Rejects missing/pending approval, duplicate/reordered case ids, any
    hash/count mismatch, invalid lineage parents, question answer-hint
    leakage, and (when supplied) a stratification manifest whose counts or
    coverage states contradict the cases. The authoritative dataset itself is
    human-sealed under Q-011; this validator is fixture-testable without it.
    """
    if manifest.get("schema_version") != STAGE1_MANIFEST_SCHEMA:
        raise ValueError(
            f"manifest schema must be {STAGE1_MANIFEST_SCHEMA}, got "
            f"{manifest.get('schema_version')!r}"
        )
    _require_approval(manifest)

    ordered_ids = manifest.get("ordered_case_ids")
    if not isinstance(ordered_ids, list) or not ordered_ids:
        raise ValueError("ordered_case_ids must be a non-empty array")
    if len(ordered_ids) != len(set(ordered_ids)):
        raise ValueError("ordered_case_ids must be unique")
    expected_ids = [case.case_id for case in cases]
    if ordered_ids != expected_ids:
        raise ValueError(
            "ordered_case_ids must match the dataset rows one-to-one in order "
            "(no duplicate, reorder, missing, or extra ids)"
        )

    declared_list_hash = manifest.get("case_list_sha256")
    actual_list_hash = case_list_sha256(ordered_ids)
    if declared_list_hash != actual_list_hash:
        raise ValueError(
            f"case_list_sha256 mismatch: manifest={declared_list_hash} "
            f"actual={actual_list_hash}"
        )

    declared_content_hash = manifest.get("dataset_content_sha256")
    actual_content_hash = dataset_content_sha256(cases)
    if declared_content_hash != actual_content_hash:
        raise ValueError(
            f"dataset_content_sha256 mismatch: manifest={declared_content_hash} "
            f"actual={actual_content_hash}"
        )

    if manifest.get("case_count") != len(cases):
        raise ValueError(
            f"case_count mismatch: manifest={manifest.get('case_count')} "
            f"loaded={len(cases)}"
        )

    for field in ("reviewer_ids",):
        reviewers = manifest.get(field)
        if not isinstance(reviewers, list) or not reviewers:
            raise ValueError(f"{field} must be a non-empty list")

    case_ids = set(ordered_ids)
    for field in ("second_pass_case_ids", "a3_eligible_ids", "a4_readiness_eligible_ids"):
        subset = manifest.get(field, ())
        if not isinstance(subset, list):
            raise ValueError(f"{field} must be an array")
        unknown = set(subset) - case_ids
        if unknown:
            raise ValueError(f"{field} references unknown case ids: {sorted(unknown)}")

    if stratification is not None:
        _validate_stratification(stratification, cases, ordered_ids)
    else:
        for case in cases:
            _check_no_answer_hint_leakage(case)

    return dict(manifest)


def _validate_stratification(
    stratification: Mapping[str, Any],
    cases: Sequence[GoldenCase],
    ordered_ids: list[str],
) -> None:
    if stratification.get("schema_version") != STAGE1_STRATIFICATION_SCHEMA:
        raise ValueError(
            f"stratification schema must be {STAGE1_STRATIFICATION_SCHEMA}, got "
            f"{stratification.get('schema_version')!r}"
        )
    per_case = stratification.get("per_case")
    if not isinstance(per_case, dict):
        raise ValueError("stratification.per_case must be an object")
    if set(per_case.keys()) != set(ordered_ids):
        raise ValueError(
            "stratification per_case keys must match the dataset case ids"
        )
    coverage = stratification.get("strata", {}).get("coverage", {})
    if not isinstance(coverage, dict):
        raise ValueError("stratification.strata.coverage must be an object")

    for case in cases:
        _check_no_answer_hint_leakage(case)
        entry = per_case.get(case.case_id)
        if entry is None:
            raise ValueError(f"stratification missing case {case.case_id!r}")
        parent = entry.get("parent_case_id")
        if parent not in ALLOWED_LEGACY_PARENT_IDS:
            raise ValueError(
                f"case {case.case_id!r} parent_case_id {parent!r} is not an "
                f"allowed legacy T4/golden source"
            )
        state = entry.get("news_content_state")
        coverage_limited = bool(entry.get("coverage_limited"))
        if state not in ("FULL_TEXT", "TITLE_ONLY", "METADATA_ONLY", "unrecovered"):
            raise ValueError(
                f"case {case.case_id!r} news_content_state {state!r} is invalid"
            )
        if coverage_limited and state == "FULL_TEXT":
            raise ValueError(
                f"case {case.case_id!r} is coverage_limited but has FULL_TEXT"
            )
        if not coverage_limited and state != "FULL_TEXT":
            raise ValueError(
                f"case {case.case_id!r} has {state} but is not coverage_limited"
            )


__all__ = [
    "ALLOWED_LEGACY_PARENT_IDS",
    "LEGACY_PARENT_PREFIX",
    "STAGE1_MANIFEST_SCHEMA",
    "STAGE1_STRATIFICATION_SCHEMA",
    "canonical_bytes",
    "case_list_sha256",
    "dataset_content_sha256",
    "dataset_provenance",
    "load_golden_cases",
    "validate_stage1_dataset_manifest",
]
