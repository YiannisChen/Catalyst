"""V1.1 GoldenCase dataset loader + provenance (M7-1/M7-2).

Eval-owned loader (eval TSD §6; M7 execution lock). Validates the full Frozen
V1.1 GoldenCase contract, rejects legacy-only values, enforces ABSTAIN refusal
reasons, restricts MARKET_STRUCTURE_UNSUPPORTED to gap reason codes, verifies
the immutable dataset content hash against a dataset manifest, and derives
the dataset provenance string. Authoritative resolved Stage-1 datasets must
also carry evidence-level ground truth (evidence_judgments binding every
expected primary evidence ID, human independence groups, corrective
task/action truth, and human-confirmed lineage); publication of the official
three-file dataset is fail-closed and write-once. The Stage-1 manifest
validator separates the public-world human oracle_status from pinned-local
coverage: SUFFICIENT does
not require a local FULL_TEXT_BODY, EIGHT_K_SHELL truthfully records a local
8-K shell only while expected_primary_evidence is empty, and every declared
stratification aggregate is recomputed from per-case rows. Production
packages never import this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
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

# Retrieval-relevant human evidence roles (M7-5 formula lock). A resolved
# Stage-1 dataset must bind these roles to human independence groups.
RELEVANT_ROLE_VALUES = frozenset(
    {"primary_support", "secondary_support", "contradiction"}
)
PRIMARY_SUPPORT_ROLE = "primary_support"

# Official M7-2 authoritative Stage-1 file names (write-once publication).
STAGE1_DATASET_FILE_NAMES = (
    "v1_1_stage1_cases.jsonl",
    "v1_1_stage1_stratification.json",
    "v1_1_stage1_dataset_manifest.json",
)


class PublicationConflictError(RuntimeError):
    """Raised when a write-once authoritative artifact already differs."""


def write_once_bytes(path: str | Path, data: bytes) -> None:
    """Publish bytes without ever replacing a concurrently-created target.

    Absent targets are created atomically (temp file + hard link, no partial
    final names); identical bytes are idempotent; differing bytes raise a
    typed conflict and leave the existing file unchanged.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        if existing == data:
            return  # idempotent
        raise PublicationConflictError(
            f"refusing to overwrite existing authoritative artifact {path} "
            f"with differing bytes"
        )
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            if path.read_bytes() == data:
                return
            raise PublicationConflictError(
                f"refusing to overwrite existing authoritative artifact {path} "
                f"with differing bytes"
            ) from None
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


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

# Required aggregate presence on the 12-case human-reviewed set. Exact
# 5/4/3 and 6/6 quota locks are NOT Stage-1 requirements; the set needs at
# least one of each oracle status and challenge family so every scenario class
# is observable.
REQUIRED_CHALLENGE_FAMILY_COVERAGE = {"COMPANY_SPECIFIC", "MACRO", "SECTOR"}
REQUIRED_ORACLE_STATUS_PRESENCE = {"SUFFICIENT", "PARTIAL", "ABSTAIN"}


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

    _validate_evidence_ground_truth(cases)
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
    _validate_declared_strata_equal_recomputed(stratification, cases, per_case)


def _recompute_declared_aggregates(
    cases: Sequence[GoldenCase],
    per_case: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, int]]:
    """Independently recompute every aggregate family the stratification may
    declare. The validator compares each DECLARED key against these values so
    mutating any aggregate count (not only coverage) fails closed."""
    from collections import Counter

    oracle = Counter(case.oracle_status for case in cases)
    move = Counter()
    family = Counter()
    coverage_limited = 0
    direct_primary = 0
    shell = 0
    no_material = 0
    cause_company = 0
    cause_macro = 0
    cause_sector = 0
    for case in cases:
        entry = per_case[case.case_id]
        move[entry.get("move_direction")] += 1
        family[entry.get("challenge_family")] += 1
        if entry.get("coverage_limited"):
            coverage_limited += 1
        kind = entry.get("primary_evidence_kind")
        if case.expected_primary_evidence and kind == "FULL_TEXT_BODY":
            direct_primary += 1
        if kind == "EIGHT_K_SHELL":
            shell += 1
        if kind == "NONE":
            no_material += 1
        for label in case.acceptable_cause_labels:
            if label.cause_type == "COMPANY_SPECIFIC_CATALYST":
                cause_company += 1
            elif label.cause_type == "MACRO_EVENT":
                cause_macro += 1
            elif label.cause_type == "SECTOR_MOVE":
                cause_sector += 1
    return {
        "oracle_status": dict(oracle),
        "move_direction": dict(move),
        "challenge_family": dict(family),
        "primary_evidence": {
            "direct_primary": direct_primary,
            "EIGHT_K_SHELL": shell,
            "no_material": no_material,
        },
        "coverage": {
            "full_text": len(cases) - coverage_limited,
            "coverage_limited": coverage_limited,
        },
        "cause_category": {
            "company_specific": cause_company,
            "macro": cause_macro,
            "sector": cause_sector,
        },
    }


MANDATORY_STRATA_SECTIONS = (
    "oracle_status",
    "challenge_family",
    "move_direction",
    "primary_evidence",
    "coverage",
)


def _validate_declared_strata_equal_recomputed(
    stratification: Mapping[str, Any],
    cases: Sequence[GoldenCase],
    per_case: Mapping[str, Mapping[str, object]],
) -> None:
    """Recompute ALL declared aggregate strata and reject any disagreement."""
    strata = stratification.get("strata")
    if not isinstance(strata, dict):
        raise ValueError("stratification.strata must be an object")
    missing = [
        section for section in MANDATORY_STRATA_SECTIONS if section not in strata
    ]
    if missing:
        raise ValueError(
            "stratification.strata missing mandatory section(s): "
            + ", ".join(missing)
        )
    recomputed = _recompute_declared_aggregates(cases, per_case)
    for section, declared_counts in strata.items():
        if not isinstance(declared_counts, dict):
            raise ValueError(
                f"stratification.strata.{section} must be an object of counts"
            )
        expected = recomputed.get(section)
        if expected is None:
            raise ValueError(
                f"stratification.strata.{section} is not independently "
                f"recomputable from per-case rows"
            )
        for key, declared in declared_counts.items():
            if key not in expected:
                raise ValueError(
                    f"stratification.strata.{section}.{key} is not independently "
                    f"recomputable from per-case rows"
                )
            if declared != expected[key]:
                raise ValueError(
                    f"stratification declared strata mismatch: "
                    f"strata.{section}.{key}= {declared}, independently "
                    f"recomputed= {expected[key]} (mutate per-case rows, not "
                    f"aggregate counts)"
                )


def _validate_stage1_gates(
    cases: Sequence[GoldenCase],
    per_case: Mapping[str, Mapping[str, object]],
) -> None:
    """Fail-closed aggregate and cross-field checks for the 12-case Stage-1 set.

    Presence gates: exactly 12 cases, at least one SUFFICIENT/PARTIAL/ABSTAIN,
    positive and negative direction coverage, challenge-family coverage
    (COMPANY_SPECIFIC/MACRO/SECTOR), at least one recoverable corrective and
    one multi-gap recoverable case, empty accepted cause labels plus refusal
    reasons on every ABSTAIN, FULL_TEXT_BODY binding for every non-empty
    expected_primary_evidence, and truthful EIGHT_K_SHELL recording with empty
    expected_primary_evidence. Exact 5/4/3 and 6/6 quota arithmetic is NOT a
    Stage-1 requirement and is not enforced here.
    """
    if len(cases) != 12:
        raise ValueError(
            f"Stage-1 dataset must contain exactly 12 cases, got {len(cases)}"
        )

    oracle_counts: dict[str, int] = {}
    direction_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    has_corrective = False
    has_multi_gap = False

    for case in cases:
        oracle_counts[case.oracle_status] = oracle_counts.get(case.oracle_status, 0) + 1
        entry = per_case[case.case_id]
        primary_kind = entry.get("primary_evidence_kind")

        if case.expected_primary_evidence:
            if primary_kind != "FULL_TEXT_BODY":
                raise ValueError(
                    f"case {case.case_id!r} has expected_primary_evidence but "
                    f"primary_evidence_kind is {primary_kind!r}; direct primary "
                    f"evidence must be FULL_TEXT_BODY"
                )
            if case.oracle_status not in ("SUFFICIENT", "PARTIAL"):
                raise ValueError(
                    f"case {case.case_id!r} has expected_primary_evidence but "
                    f"oracle_status {case.oracle_status!r} (ABSTAIN must keep "
                    f"expected_primary_evidence empty)"
                )
        else:
            if primary_kind == "FULL_TEXT_BODY":
                raise ValueError(
                    f"case {case.case_id!r} primary_evidence_kind "
                    f"FULL_TEXT_BODY requires non-empty expected_primary_evidence; "
                    f"EIGHT_K_SHELL/NONE are the only empty-primary kinds"
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

    missing_statuses = sorted(REQUIRED_ORACLE_STATUS_PRESENCE - set(oracle_counts))
    if missing_statuses:
        raise ValueError(
            f"Stage-1 oracle_status strata require at least one each of "
            f"SUFFICIENT/PARTIAL/ABSTAIN, missing {missing_statuses}; got "
            f"{oracle_counts}"
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


# ---------------------------------------------------------------------------
# Q-011 evidence-ground-truth gates (authoritative resolved Stage-1 datasets)
# ---------------------------------------------------------------------------

def _evidence_ground_truth_errors(case: GoldenCase) -> list[str]:
    """Fail-closed evidence ground-truth checks for one resolved case.

    A resolved authoritative Stage-1 case must not omit the evidence-level
    ground truth required by the M7 plan and retrieval metrics: every expected
    primary evidence ID needs a schema-locked human evidence_judgments row
    judged primary_support and temporal-eligible; relevant judgments need a
    human independence group; corrective-required/recoverable cases need
    acceptable initial-task and corrective-action truth; resolved lineage
    needs human_confirmed_fields. ``legacy:*`` remains a lineage.parent
    marker only and is never a corpus evidence identity.
    """
    errors: list[str] = []
    prefix = f"case {case.case_id!r}"
    if case.lineage.adjudication_state == "resolved" and not case.lineage.human_confirmed_fields:
        errors.append(
            f"{prefix} resolved lineage requires non-empty human_confirmed_fields"
        )
    seen_ids: set[str] = set()
    for judgment in case.evidence_judgments:
        if judgment.evidence_id in seen_ids:
            errors.append(
                f"{prefix} evidence_judgments contains duplicate rows for "
                f"evidence_id {judgment.evidence_id!r}; evidence_id must be "
                f"unique within each case"
            )
        seen_ids.add(judgment.evidence_id)
        if judgment.evidence_id.startswith(LEGACY_PARENT_PREFIX):
            errors.append(
                f"{prefix} evidence_judgments identity {judgment.evidence_id!r} "
                f"uses the reserved {LEGACY_PARENT_PREFIX}* namespace; only "
                f"lineage.source may carry legacy parents"
            )
        if judgment.role in RELEVANT_ROLE_VALUES and not judgment.independence_group:
            errors.append(
                f"{prefix} evidence_judgments row {judgment.evidence_id!r} with "
                f"role {judgment.role!r} must declare independence_group for "
                f"retrieval ground truth"
            )
    for evidence_id in case.expected_primary_evidence:
        if evidence_id.startswith(LEGACY_PARENT_PREFIX):
            errors.append(
                f"{prefix} expected_primary_evidence identity {evidence_id!r} "
                f"uses the reserved {LEGACY_PARENT_PREFIX}* namespace; only "
                f"lineage.source may carry legacy parents"
            )
            continue
        matches = [
            judgment
            for judgment in case.evidence_judgments
            if judgment.evidence_id == evidence_id
        ]
        if len(matches) != 1:
            errors.append(
                f"{prefix} expected_primary_evidence {evidence_id!r} must match "
                f"exactly one unique evidence_judgments row, got "
                f"{len(matches)}"
            )
            continue
        primary = matches[0]
        failures: list[str] = []
        if primary.role != PRIMARY_SUPPORT_ROLE:
            failures.append(f"role={primary.role!r} != {PRIMARY_SUPPORT_ROLE}")
        if primary.support is not True:
            failures.append(f"support={primary.support!r} != true")
        if primary.materiality != "material":
            failures.append(f"materiality={primary.materiality!r} != material")
        if primary.temporal_eligible is not True:
            failures.append(f"temporal_eligible={primary.temporal_eligible!r} != true")
        if not primary.independence_group:
            failures.append("independence_group is empty")
        if failures:
            errors.append(
                f"{prefix} expected_primary_evidence {evidence_id!r} judgment is "
                f"not coherent primary_support ground truth: "
                + "; ".join(failures)
            )
    behavior = case.expected_research_behavior
    if behavior.corrective_required or behavior.corrective_recoverable:
        if not behavior.acceptable_initial_tasks:
            errors.append(
                f"{prefix} corrective_required/recoverable case must declare "
                f"acceptable_initial_tasks truth"
            )
        if not behavior.acceptable_corrective_actions:
            errors.append(
                f"{prefix} corrective_required/recoverable case must declare "
                f"acceptable_corrective_actions truth"
            )
    return errors


def _validate_evidence_ground_truth(cases: Sequence[GoldenCase]) -> None:
    """Reject any resolved Stage-1 dataset missing evidence-level ground truth."""
    errors: list[str] = []
    for case in cases:
        errors.extend(_evidence_ground_truth_errors(case))
    if errors:
        raise ValueError(
            "resolved Stage-1 dataset evidence ground truth is incomplete: "
            + " | ".join(errors)
        )


def write_stage1_dataset_files(
    out_dir: str | Path,
    *,
    rows: Sequence[Mapping[str, Any]],
    stratification: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, str]:
    """Publish the official Stage-1 three-file dataset fail-closed, write-once.

    Validates the complete rows/stratification/manifest (including approval
    and evidence ground truth) before publishing. Existing identical files are
    idempotent; any differing existing file raises :class:
    `PublicationConflictError` before any file is written; each final file is
    created atomically and never via ordinary ``Path.write_text``.
    """
    out = Path(out_dir)
    parsed = [GoldenCase.model_validate(row) for row in rows]
    validate_stage1_dataset_manifest(
        manifest, parsed, stratification=stratification
    )
    payloads = {
        STAGE1_DATASET_FILE_NAMES[0]: (
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
        ).encode("utf-8"),
        STAGE1_DATASET_FILE_NAMES[1]: json.dumps(
            stratification, indent=2, sort_keys=True
        ).encode("utf-8") + b"\n",
        STAGE1_DATASET_FILE_NAMES[2]: json.dumps(
            manifest, indent=2, sort_keys=True
        ).encode("utf-8") + b"\n",
    }
    out.mkdir(parents=True, exist_ok=True)
    # Preflight conflict check across the whole set so no partial authoritative
    # update can occur when any single target already differs.
    for name, data in payloads.items():
        target = out / name
        if target.exists() and target.read_bytes() != data:
            raise PublicationConflictError(
                f"refusing to overwrite existing authoritative artifact "
                f"{target} with differing bytes"
            )
    digests: dict[str, str] = {}
    for name, data in payloads.items():
        write_once_bytes(out / name, data)
        digests[name] = hashlib.sha256(data).hexdigest()
    return digests


__all__ = [
    "ALLOWED_LEGACY_PARENT_IDS",
    "CHALLENGE_FAMILY_VALUES",
    "LEGACY_PARENT_PREFIX",
    "MANDATORY_STRATA_SECTIONS",
    "MOVE_DIRECTION_VALUES",
    "PRIMARY_EVIDENCE_KIND_VALUES",
    "RELEVANT_ROLE_VALUES",
    "REQUIRED_CHALLENGE_FAMILY_COVERAGE",
    "REQUIRED_ORACLE_STATUS_PRESENCE",
    "STAGE1_DATASET_FILE_NAMES",
    "STAGE1_MANIFEST_SCHEMA",
    "STAGE1_STRATIFICATION_SCHEMA",
    "PublicationConflictError",
    "canonical_bytes",
    "case_list_sha256",
    "dataset_content_sha256",
    "dataset_provenance",
    "load_golden_cases",
    "validate_stage1_dataset_manifest",
    "write_once_bytes",
    "write_stage1_dataset_files",
]
