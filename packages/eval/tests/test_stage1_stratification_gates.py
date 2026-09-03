"""M7-2: Stage-1 stratification gates (fixture-only, Q-011 gated).

The validator must fail closed on the full Stage-1 12-case contract:
exactly 5/4/3 oracle statuses, exactly 6 direct_primary / 6 no_material,
positive AND negative move directions, COMPANY_SPECIFIC + MACRO + SECTOR
challenge-family coverage, at least one recoverable corrective case and one
multi-gap recoverable case, empty accepted cause labels for every ABSTAIN,
and no 8-K-shell primary evidence. No human approval or reviewer identity is
represented here.
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

# Rows that start as PARTIAL no_material; flipping them direct creates a
# 9/3 primary split while keeping per-case consistency.
NO_MATERIAL_PARTIAL_IDS = ("v1f-007", "v1f-008", "v1f-010")


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


def test_fixture_manifest_passes_every_stage1_gate(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    validated = validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )
    assert validated["schema_version"] == "v1_1_stage1_dataset_manifest_v1"


def test_nine_three_primary_split_rejected(tmp_path):
    rows, cases, _stratification, _manifest = _dataset(tmp_path)
    for case_id in NO_MATERIAL_PARTIAL_IDS:
        row = _case_row(rows, case_id)
        row["expected_primary_evidence"] = [f"fixture-ev-extra:{case_id}"]
    cases, stratification, manifest = _rebuild(rows)
    with pytest.raises(ValueError, match="primary_evidence"):
        validate_stage1_dataset_manifest(
            manifest, cases, stratification=stratification
        )


def test_all_company_challenge_families_rejected(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    for case_id, entry in stratification["per_case"].items():
        entry["challenge_family"] = "COMPANY_SPECIFIC"
    with pytest.raises(ValueError, match="challenge_family"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_abstain_with_accepted_macro_event_label_rejected(tmp_path):
    rows, cases, _stratification, _manifest = _dataset(tmp_path)
    row = _case_row(rows, "v1f-003")  # MACRO-challenge ABSTAIN
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
    row = _case_row(rows, "v1f-003")
    row["expected_refusal_reason"] = None
    cases, stratification, manifest = _rebuild(rows)
    with pytest.raises(ValueError, match="refusal"):
        validate_stage1_dataset_manifest(
            manifest, cases, stratification=stratification
        )


def test_eight_k_shell_sufficient_rejected(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    stratification["per_case"]["v1f-001"]["primary_evidence_kind"] = "EIGHT_K_SHELL"
    with pytest.raises(ValueError, match="EIGHT_K_SHELL"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_eight_k_shell_partial_direct_rejected(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    stratification["per_case"]["v1f-004"]["primary_evidence_kind"] = "EIGHT_K_SHELL"
    with pytest.raises(ValueError, match="EIGHT_K_SHELL"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_eight_k_shell_no_material_rejected(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    stratification["per_case"]["v1f-003"]["primary_evidence_kind"] = "EIGHT_K_SHELL"
    with pytest.raises(ValueError, match="EIGHT_K_SHELL"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_sufficient_with_none_primary_kind_rejected(tmp_path):
    rows, cases, stratification, manifest = _dataset(tmp_path)
    stratification["per_case"]["v1f-001"]["primary_evidence_kind"] = "NONE"
    with pytest.raises(ValueError, match="FULL_TEXT_BODY"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


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
    rows, cases, stratification, manifest = _dataset(tmp_path)
    stratification["per_case"]["v1f-007"]["primary_evidence_kind"] = "FULL_TEXT_BODY"
    with pytest.raises(ValueError, match="primary_evidence_kind"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)
