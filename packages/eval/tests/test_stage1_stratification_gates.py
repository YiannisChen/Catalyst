"""M7-2: Stage-1 stratification gates (fixture-only, Q-011 gated).

The validator must fail closed on the legitimate Stage-1 12-case contract:
presence gates (at least one SUFFICIENT/PARTIAL/ABSTAIN, positive AND
negative directions, COMPANY_SPECIFIC + MACRO + SECTOR challenge families,
one recoverable corrective case and one multi-gap recoverable case), empty
accepted cause labels plus refusal reasons on every ABSTAIN, FULL_TEXT_BODY
binding for every non-empty expected_primary_evidence, truthful EIGHT_K_SHELL
recording only with empty expected_primary_evidence, and declared aggregate
strata that equal independently recomputed per-case values for EVERY declared
aggregate key. Exact 5/4/3 or 6/6 quotas are NOT enforced. No human approval
or reviewer identity is represented here.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.loader import (
    load_golden_cases,
    validate_stage1_dataset_manifest,
)

from tests.v1_1_fixtures import (
    make_dataset_manifest,
    make_stage1_cases,
    make_stratification,
)

# Rows that start as no-material PARTIAL; flipping them direct creates a
# mixed primary split (direct 8 / no_material 3 / shell 1) that must PASS
# because exact 6/6 is not a requirement.
NO_MATERIAL_PARTIAL_IDS = ("v1f-007", "v1f-008")


def _dataset(tmp_path: Path):
    rows = make_stage1_cases()
    path = tmp_path / "stage1_cases.jsonl"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )
    cases = load_golden_cases(path)
    stratification = make_stratification(rows)
    manifest = make_dataset_manifest(rows, stratification)
    return rows, cases, stratification, manifest


def _case_row(rows, case_id: str) -> dict:
    return next(row for row in rows if row["case_id"] == case_id)


def _rebuild(rows):
    stratification = make_stratification(rows)
    manifest = make_dataset_manifest(rows, stratification)
    cases = [GoldenCase.model_validate(row) for row in rows]
    return cases, stratification, manifest


def _validate(tmp_path: Path, *, mutate=None):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    if mutate is not None:
        mutate(rows, cases, stratification, manifest)
    return validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )


def _sync_primary_strata(rows, stratification) -> None:
    """Recompute declared primary-evidence strata from per_case kinds after a
    per-case mutation so declared aggregates match the mutated reality."""
    kinds: dict[str, int] = {}
    per_case = stratification["per_case"]
    for entry in per_case.values():
        kind = entry["primary_evidence_kind"]
        kinds[kind] = kinds.get(kind, 0) + 1
    direct_primary = sum(
        1 for r in rows
        if r["expected_primary_evidence"]
        and per_case[r["case_id"]]["primary_evidence_kind"] == "FULL_TEXT_BODY"
    )
    stratification["strata"]["primary_evidence"] = {
        "direct_primary": direct_primary,
        "EIGHT_K_SHELL": kinds.get("EIGHT_K_SHELL", 0),
        "no_material": kinds.get("NONE", 0),
    }


def test_fixture_manifest_passes_every_stage1_gate(tmp_path):
    validated = _validate(tmp_path)
    assert validated["schema_version"] == "v1_1_stage1_dataset_manifest_v1"


def test_mixed_primary_split_without_six_six_quota_passes(tmp_path):
    """A non-6/6 primary split (8 direct / 3 no-material / 1 shell) must pass
    when the legitimate presence gates hold."""
    rows, _cases, _stratification, _manifest = _dataset(tmp_path)
    for case_id in NO_MATERIAL_PARTIAL_IDS:
        row = _case_row(rows, case_id)
        row["expected_primary_evidence"] = [f"fixture-ev-extra:{case_id}"]
    cases, stratification, manifest = _rebuild(rows)
    primary = stratification["strata"]["primary_evidence"]
    assert primary["direct_primary"] == 8
    assert primary["no_material"] == 3
    assert primary["EIGHT_K_SHELL"] == 1
    validated = validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )
    assert validated["case_count"] == 12


def test_all_company_challenge_families_rejected(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    for case_id, entry in stratification["per_case"].items():
        entry["challenge_family"] = "COMPANY_SPECIFIC"
    with pytest.raises(ValueError, match="challenge_family"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_abstain_with_accepted_macro_event_label_rejected(tmp_path):
    rows, cases, _stratification, _manifest = _dataset(tmp_path)
    row = _case_row(rows, "v1f-011")  # MACRO-challenge ABSTAIN
    row["acceptable_cause_labels"] = [
        {
            "cause_type": "MACRO_EVENT",
            "label": "fixture-accepted-macro",
            "direction": "mixed",
            "materiality": "material",
        }
    ]
    cases, stratification, manifest = _rebuild(rows)
    with pytest.raises(ValueError, match="ABSTAIN"):
        validate_stage1_dataset_manifest(
            manifest, cases, stratification=stratification
        )


def test_abstain_without_refusal_reason_rejected(tmp_path):
    rows, cases, _stratification, _manifest = _dataset(tmp_path)
    row = _case_row(rows, "v1f-011")
    row["expected_refusal_reason"] = None
    cases, stratification, manifest = _rebuild(rows)
    with pytest.raises(ValueError, match="refusal"):
        validate_stage1_dataset_manifest(
            manifest, cases, stratification=stratification
        )


@pytest.mark.parametrize("case_id", ["v1f-003", "v1f-007", "v1f-011"])
def test_eight_k_shell_with_empty_primary_allowed(tmp_path, case_id):
    """EIGHT_K_SHELL + empty expected_primary_evidence is ALLOWED on any
    status (SUFFICIENT, PARTIAL, or ABSTAIN); the shell is truthfully
    recorded and never bound as primary evidence."""
    rows, cases, stratification, manifest = _dataset(tmp_path)
    assert not _case_row(rows, case_id)["expected_primary_evidence"]
    stratification["per_case"][case_id]["primary_evidence_kind"] = "EIGHT_K_SHELL"
    _sync_primary_strata(rows, stratification)
    validated = validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )
    assert validated["case_count"] == 12


def test_eight_k_shell_with_direct_primary_rejected(tmp_path):
    """EIGHT_K_SHELL + non-empty expected_primary_evidence is still REJECTED."""
    rows, cases, stratification, manifest = _dataset(tmp_path)
    stratification["per_case"]["v1f-001"]["primary_evidence_kind"] = "EIGHT_K_SHELL"
    _sync_primary_strata(rows, stratification)
    with pytest.raises(ValueError, match="FULL_TEXT_BODY"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_direct_primary_with_none_kind_rejected(tmp_path):
    """Non-empty expected_primary_evidence requires FULL_TEXT_BODY."""
    rows, cases, stratification, manifest = _dataset(tmp_path)
    stratification["per_case"]["v1f-001"]["primary_evidence_kind"] = "NONE"
    _sync_primary_strata(rows, stratification)
    with pytest.raises(ValueError, match="FULL_TEXT_BODY"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_sufficient_with_empty_primary_and_none_kind_is_allowed(tmp_path):
    """A SUFFICIENT case with no local material primary (kind NONE, empty
    expected_primary_evidence) passes once the SUFFICIENT-requires-body rule
    is removed; this is the c01-equivalent default fixture case v1f-003."""
    rows, cases, stratification, manifest = _dataset(tmp_path)
    entry = stratification["per_case"]["v1f-003"]
    row = _case_row(rows, "v1f-003")
    assert row["oracle_status"] == "SUFFICIENT"
    assert entry["primary_evidence_kind"] == "NONE"
    assert row["expected_primary_evidence"] == []
    validated = validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )
    assert validated["case_count"] == 12


def test_no_negative_move_direction_rejected(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    for entry in stratification["per_case"].values():
        entry["move_direction"] = "positive"
    with pytest.raises(ValueError, match="move_direction"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_missing_challenge_family_rejected(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    del stratification["per_case"]["v1f-001"]["challenge_family"]
    with pytest.raises(ValueError, match="challenge_family"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_missing_primary_evidence_kind_rejected(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    del stratification["per_case"]["v1f-001"]["primary_evidence_kind"]
    with pytest.raises(ValueError, match="primary_evidence_kind"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_invalid_challenge_family_value_rejected(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    stratification["per_case"]["v1f-001"]["challenge_family"] = "MACRO_EVENT"
    with pytest.raises(ValueError, match="challenge_family"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_no_recoverable_corrective_case_rejected(tmp_path):
    rows, cases, _stratification, _manifest = _dataset(tmp_path)
    for row in rows:
        row["expected_research_behavior"]["corrective_recoverable"] = False
    cases, stratification, manifest = _rebuild(rows)
    with pytest.raises(ValueError, match="corrective_recoverable"):
        validate_stage1_dataset_manifest(
            manifest, cases, stratification=stratification
        )


def test_no_multi_gap_recoverable_case_rejected(tmp_path):
    rows, cases, _stratification, _manifest = _dataset(tmp_path)
    for row in rows:
        if row["expected_research_behavior"]["corrective_recoverable"]:
            row["expected_research_behavior"]["expected_gap_reason_codes"] = [
                row["expected_research_behavior"]["expected_gap_reason_codes"][0]
            ]
    cases, stratification, manifest = _rebuild(rows)
    with pytest.raises(ValueError, match="multi-gap"):
        validate_stage1_dataset_manifest(
            manifest, cases, stratification=stratification
        )


def test_partial_no_material_with_full_text_body_kind_rejected(tmp_path):
    """FULL_TEXT_BODY records a bound material body; empty
    expected_primary_evidence cannot claim a FULL_TEXT_BODY kind."""
    rows, cases, stratification, manifest = _dataset(tmp_path)
    stratification["per_case"]["v1f-007"]["primary_evidence_kind"] = "FULL_TEXT_BODY"
    _sync_primary_strata(rows, stratification)
    with pytest.raises(ValueError, match="expected_primary_evidence"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


# --- declared aggregates are independently recomputed (false-green fix) ---

def _mutate_declared_stratum(tmp_path: Path, section: str, key: str, new_value: int):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    assert key in stratification["strata"][section]
    stratification["strata"][section][key] = new_value
    return validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )


@pytest.mark.parametrize(
    ("section", "key", "new_value"),
    [
        ("oracle_status", "SUFFICIENT", 5),
        ("primary_evidence", "no_material", 6),
        ("challenge_family", "COMPANY_SPECIFIC", 9),
        ("move_direction", "positive", 1),
        ("coverage", "coverage_limited", 0),
        ("cause_category", "macro", 1),
    ],
)
def test_declared_stratum_mutation_fails(tmp_path, section, key, new_value):
    """Mutating any declared aggregate count while leaving per-case rows and
    GoldenCase rows unchanged must fail closed."""
    with pytest.raises(ValueError, match="strata"):
        _mutate_declared_stratum(tmp_path, section, key, new_value)


def test_declared_oracle_status_contradicts_per_case(tmp_path):
    """Per-case rows imply SUFFICIENT=9; a declared SUFFICIENT=5 fails even
    though per-case rows are internally consistent."""
    with pytest.raises(ValueError, match="strata"):
        _mutate_declared_stratum(tmp_path, "oracle_status", "SUFFICIENT", 5)


MANDATORY_STRATA_SECTIONS = (
    "oracle_status",
    "challenge_family",
    "move_direction",
    "primary_evidence",
    "coverage",
)


@pytest.mark.parametrize("section", MANDATORY_STRATA_SECTIONS)
def test_omitting_mandatory_strata_section_fails(tmp_path, section):
    """Every mandatory aggregate section must be present; omitting one fails
    even when remaining declared counts still match per-case rows."""
    rows, cases, stratification, manifest = _dataset(tmp_path)
    del stratification["strata"][section]
    with pytest.raises(ValueError, match="mandatory"):
        validate_stage1_dataset_manifest(
            manifest, cases, stratification=stratification
        )
