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

# Legacy T4/golden parents plus the Stage-1 slot ids c01..c12 used by the
# Q-011 candidate packets (M7-2 stratification contract). The validator's
# parent allow-list must match how real packets bind slots.
ALLOWED_LEGACY_PARENT_IDS = frozenset(
    {
        "g006", "g013", "g017", "g024", "g041", "g007",
        "h001", "h004", "h005", "h007", "pre_b6",
        "c01", "c02", "c03", "c04", "c05", "c06",
        "c07", "c08", "c09", "c10", "c11", "c12",
    }
)

# Closed per-case stratification vocabularies (M7-2 Q-011 lock). challenge_family
# is the scenario/challenge class from Frozen V1.1 §7.2 and is never inferred
# from accepted CauseType labels. primary_evidence_kind says whether the bound
# primary body is FULL_TEXT material evidence, an 8-K shell (never material),
# or absent.
CHALLENGE_FAMILY_VALUES = frozenset(
    {
        "COMPANY_SPECIFIC",
        "MACRO",
        "SECTOR",
        "CONTINUATION",
        "DISTRACTOR",
        "QUIET",
        "ABSTENTION",
    }
)
PRIMARY_EVIDENCE_KIND_VALUES = frozenset(
    {"FULL_TEXT_BODY", "EIGHT_K_SHELL", "NONE"}
)
MOVE_DIRECTION_VALUES = frozenset({"positive", "negative", "mixed", "unknown"})

# Required aggregate Stage-1 strata on the 12-case human-reviewed set.
REQUIRED_ORACLE_STATUS_COUNTS = {"SUFFICIENT": 5, "PARTIAL": 4, "ABSTAIN": 3}
REQUIRED_PRIMARY_EVIDENCE_COUNTS = {"direct_primary": 6, "no_material": 6}
REQUIRED_CHALLENGE_FAMILY_COVERAGE = {"COMPANY_SPECIFIC", "MACRO", "SECTOR"}


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

        challenge_family = entry.get("challenge_family")
        if challenge_family not in CHALLENGE_FAMILY_VALUES:
            raise ValueError(
                f"case {case.case_id!r} challenge_family {challenge_family!r} is "
                f"not a Stage-1 challenge family"
            )
        primary_kind = entry.get("primary_evidence_kind")
        if primary_kind not in PRIMARY_EVIDENCE_KIND_VALUES:
            raise ValueError(
                f"case {case.case_id!r} primary_evidence_kind {primary_kind!r} "
                f"must be one of FULL_TEXT_BODY/EIGHT_K_SHELL/NONE"
            )
        move_direction = entry.get("move_direction")
        if move_direction not in MOVE_DIRECTION_VALUES:
            raise ValueError(
                f"case {case.case_id!r} move_direction {move_direction!r} is invalid"
            )

    _validate_stage1_gates(cases, per_case)


def _validate_stage1_gates(
    cases: Sequence[GoldenCase],
    per_case: Mapping[str, Mapping[str, object]],
) -> None:
    """Fail-closed aggregate and cross-field checks for the 12-case Stage-1 set.

    The exact 12-case oracle and primary splits, positive/negative direction
    coverage, challenge-family coverage (COMPANY_SPECIFIC/MACRO/SECTOR), at
    least one recoverable corrective and one multi-gap recoverable case,
    empty accepted cause labels on every ABSTAIN, refusal reasons on every
    ABSTAIN, and the no-8-K-shell primary rule are enforced here.
    """
    if len(cases) != 12:
        raise ValueError(
            f"Stage-1 dataset must contain exactly 12 cases, got {len(cases)}"
        )

    oracle_counts: dict[str, int] = {}
    primary_counts = {"direct_primary": 0, "no_material": 0}
    direction_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    has_corrective = False
    has_multi_gap = False

    for case in cases:
        oracle_counts[case.oracle_status] = oracle_counts.get(case.oracle_status, 0) + 1
        entry = per_case[case.case_id]
        primary_kind = entry.get("primary_evidence_kind")

        if primary_kind == "EIGHT_K_SHELL":
            raise ValueError(
                f"case {case.case_id!r}: EIGHT_K_SHELL is never material "
                f"primary evidence and cannot appear in a Stage-1 manifest"
            )

        if case.expected_primary_evidence:
            primary_counts["direct_primary"] += 1
            if primary_kind != "FULL_TEXT_BODY":
                raise ValueError(
                    f"case {case.case_id!r} is direct_primary but "
                    f"primary_evidence_kind is {primary_kind!r}; direct primary "
                    f"evidence must be FULL_TEXT_BODY"
                )
            if case.oracle_status not in ("SUFFICIENT", "PARTIAL"):
                raise ValueError(
                    f"case {case.case_id!r} has expected_primary_evidence but "
                    f"oracle_status {case.oracle_status!r} (no-material statuses "
                    f"must keep expected_primary_evidence empty)"
                )
        else:
            primary_counts["no_material"] += 1
            if primary_kind != "NONE":
                raise ValueError(
                    f"case {case.case_id!r} primary_evidence_kind "
                    f"{primary_kind!r} requires expected_primary_evidence; "
                    f"no_material cases must use NONE"
                )
            if case.oracle_status == "SUFFICIENT":
                raise ValueError(
                    f"case {case.case_id!r}: SUFFICIENT requires direct "
                    f"FULL_TEXT_BODY primary evidence"
                )

        if case.oracle_status == "ABSTAIN":
            if not case.expected_refusal_reason:
                raise ValueError(
                    f"case {case.case_id!r}: ABSTAIN requires "
                    f"expected_refusal_reason"
                )
            if case.acceptable_cause_labels:
                raise ValueError(
                    f"case {case.case_id!r}: ABSTAIN must have empty "
                    f"acceptable_cause_labels; challenge_family is the scenario "
                    f"class and is not an accepted causal label"
                )

        move_direction = entry.get("move_direction")
        if move_direction in ("positive", "negative"):
            direction_counts[move_direction] = (
                direction_counts.get(move_direction, 0) + 1
            )
        family = entry.get("challenge_family")
        if family in CHALLENGE_FAMILY_VALUES:
            family_counts[family] = family_counts.get(family, 0) + 1

        behavior = case.expected_research_behavior
        if behavior.corrective_recoverable:
            has_corrective = True
            if len(behavior.expected_gap_reason_codes) >= 2:
                has_multi_gap = True

    if oracle_counts != REQUIRED_ORACLE_STATUS_COUNTS:
        raise ValueError(
            f"Stage-1 oracle_status strata must be {REQUIRED_ORACLE_STATUS_COUNTS}, "
            f"got {oracle_counts}"
        )
    if primary_counts != REQUIRED_PRIMARY_EVIDENCE_COUNTS:
        raise ValueError(
            f"Stage-1 primary_evidence split must be "
            f"{REQUIRED_PRIMARY_EVIDENCE_COUNTS}, got {primary_counts}"
        )
    if not direction_counts.get("positive") or not direction_counts.get("negative"):
        raise ValueError(
            f"Stage-1 move_direction strata require at least one positive and "
            f"one negative case, got {direction_counts}"
        )
    missing_families = sorted(REQUIRED_CHALLENGE_FAMILY_COVERAGE - set(family_counts))
    if missing_families:
        raise ValueError(
            f"Stage-1 challenge_family coverage requires at least one each of "
            f"COMPANY_SPECIFIC/MACRO/SECTOR, missing {missing_families}; "
            f"challenge-family strata, not accepted cause labels, prove "
            f"macro/sector coverage"
        )
    if not has_corrective:
        raise ValueError(
            "Stage-1 set requires at least one corrective_recoverable=true case"
        )
    if not has_multi_gap:
        raise ValueError(
            "Stage-1 set requires at least one recoverable case with >=2 "
            "expected_gap_reason_codes (multi-gap recoverable)"
        )




__all__ = [
    "ALLOWED_LEGACY_PARENT_IDS",
    "CHALLENGE_FAMILY_VALUES",
    "LEGACY_PARENT_PREFIX",
    "MOVE_DIRECTION_VALUES",
    "PRIMARY_EVIDENCE_KIND_VALUES",
    "REQUIRED_CHALLENGE_FAMILY_COVERAGE",
    "REQUIRED_ORACLE_STATUS_COUNTS",
    "REQUIRED_PRIMARY_EVIDENCE_COUNTS",
    "STAGE1_MANIFEST_SCHEMA",
    "STAGE1_STRATIFICATION_SCHEMA",
    "canonical_bytes",
    "case_list_sha256",
    "dataset_content_sha256",
    "dataset_provenance",
    "load_golden_cases",
    "validate_stage1_dataset_manifest",
]
