"""M7-2 (fixture-only): Stage-1 dataset manifest + stratification contract.

The authoritative 12-case human-reviewed dataset is Q-011 gated and is NOT
created here (no human approval, reviewer identity, or audit decision is
invented). This suite validates the manifest/stratification schema and the
loader's fail-closed rules against synthetic hidden-gold fixtures so Batch B
contracts stay executable offline.

The fixture rows carry the Stage-1 stratification contract: per-case
``challenge_family`` (scenario class, never inferred from accepted cause
labels), ``primary_evidence_kind`` (FULL_TEXT_BODY / EIGHT_K_SHELL / NONE),
and ``move_direction`` (signed-session direction even when ABSTAIN has no
accepted labels). Oracle status is the public-world human adjudication and is
NOT inferred from local corpus completeness; a coverage-limited SUFFICIENT
case with empty ``expected_primary_evidence`` is valid, and EIGHT_K_SHELL is
truthful only when ``expected_primary_evidence`` is empty. Presence gates
(one of each status, positive/negative moves, company/macro/sector families,
corrective and multi-gap recoverable, ABSTAIN labels/refusal) are enforced in
the validator and tested in test_stage1_stratification_gates.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.loader import (
    STAGE1_MANIFEST_SCHEMA,
    case_list_sha256,
    dataset_content_sha256,
    load_golden_cases,
    validate_stage1_dataset_manifest,
)

from tests.v1_1_fixtures import (
    ALLOWED_LEGACY_PARENTS,
    CHALLENGE_FAMILY_VALUES,
    MOVE_DIRECTION_VALUES,
    PRIMARY_EVIDENCE_KIND_VALUES,
    make_case,
    make_dataset_manifest,
    make_stage1_cases,
    make_stratification,
)

def _fixture_dataset(tmp_path: Path, *, approved: bool = True):
    rows = make_stage1_cases()
    path = tmp_path / "stage1_cases.jsonl"
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    cases = load_golden_cases(path)
    stratification = make_stratification(rows)
    manifest = make_dataset_manifest(rows, stratification, approved=approved)
    return rows, cases, stratification, manifest


def test_fixture_has_twelve_cases_with_presence_strata_only():
    """The fixture covers every legitimate presence gate but locks NO exact
    status or primary-evidence quota (5/4/3 and 6/6 are not requirements)."""
    rows = make_stage1_cases()
    assert len(rows) == 12
    strata = make_stratification(rows)["strata"]
    # Presence gates only: at least one of each oracle status, direction,
    # challenge family, corrective and multi-gap recoverable.
    assert all(strata["oracle_status"].get(s, 0) >= 1 for s in ("SUFFICIENT", "PARTIAL", "ABSTAIN"))
    assert strata["challenge_family"]["COMPANY_SPECIFIC"] >= 1
    assert strata["challenge_family"]["MACRO"] >= 1
    assert strata["challenge_family"]["SECTOR"] >= 1
    assert strata["move_direction"]["positive"] >= 1
    assert strata["move_direction"]["negative"] >= 1
    recoverable = [
        r for r in rows
        if r["expected_research_behavior"]["corrective_recoverable"]
    ]
    assert recoverable, "fixture must contain a recoverable corrective case"
    assert any(
        len(r["expected_research_behavior"]["expected_gap_reason_codes"]) >= 2
        for r in recoverable
    ), "fixture must contain a multi-gap recoverable case"


def test_fixture_natural_distribution_is_not_a_locked_quota(tmp_path):
    """The default fixture is a 9/2/1-style mix with a shell-separated primary
    split (direct_primary + EIGHT_K_SHELL + no_material, not a 6/6 quota);
    validation must accept it when the presence gates hold."""
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    strata = stratification["strata"]
    # Presence gates only. No exact 5/4/3 or 6/6 arithmetic is required.
    assert len(rows) == 12
    assert all(strata["oracle_status"].get(s, 0) >= 1 for s in ("SUFFICIENT", "PARTIAL", "ABSTAIN"))
    primary = strata["primary_evidence"]
    assert primary["direct_primary"] >= 1
    assert primary["EIGHT_K_SHELL"] >= 1
    assert primary["no_material"] >= 1
    assert primary["direct_primary"] + primary["EIGHT_K_SHELL"] + primary["no_material"] == 12
    validated = validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )
    assert validated["case_count"] == 12


def test_every_case_references_an_allowed_legacy_parent(tmp_path):
    _rows, _cases, stratification, _manifest = _fixture_dataset(tmp_path)
    parents = {
        entry["parent_case_id"] for entry in stratification["per_case"].values()
    }
    assert parents <= set(ALLOWED_LEGACY_PARENTS)


def test_every_case_has_challenge_family_not_inferred_from_labels(tmp_path):
    """challenge_family is a stratification field, not a derived cause label.

    An ABSTAIN fixture carries a MACRO or SECTOR challenge with zero accepted
    cause labels; the family cannot have been inferred from labels because
    there are no labels to infer from.
    """
    rows, _cases, stratification, _manifest = _fixture_dataset(tmp_path)
    by_id = {row["case_id"]: row for row in rows}
    for case_id, entry in stratification["per_case"].items():
        assert entry["challenge_family"] in CHALLENGE_FAMILY_VALUES
        assert entry["move_direction"] in MOVE_DIRECTION_VALUES
        assert entry["primary_evidence_kind"] in PRIMARY_EVIDENCE_KIND_VALUES
        labels = by_id[case_id]["acceptable_cause_labels"]
        family = entry["challenge_family"]
        # The family is scenario-class; a MACRO/SECTOR challenge does not
        # require an accepted MACRO_EVENT/SECTOR_MOVE cause label.
        if by_id[case_id]["oracle_status"] == "ABSTAIN":
            assert not labels, (
                f"{case_id}: ABSTAIN must have empty acceptable cause labels"
            )
            assert family in {"MACRO", "SECTOR"}


def test_abstain_fixture_has_empty_labels_and_macro_sector_challenge():
    rows = make_stage1_cases()
    for row in rows:
        if row["oracle_status"] == "ABSTAIN":
            assert row["expected_refusal_reason"]
            assert row["acceptable_cause_labels"] == []
            assert not row["expected_primary_evidence"]


def test_sufficient_fixture_primary_bodies_are_full_text_not_8k_shell(tmp_path):
    """Direct-primary SUFFICIENT cases bind FULL_TEXT_BODY bodies only. The
    c01-equivalent SUFFICIENT case (v1f-003) is coverage-limited with no local
    material primary (NONE, empty expected_primary_evidence); v1f-012 shows a
    coverage-limited news state may still have a FULL_TEXT_BODY filing primary."""
    rows, _cases, stratification, _manifest = _fixture_dataset(tmp_path)
    by_id = {row["case_id"]: row for row in rows}
    # c01-equivalent: coverage-limited SUFFICIENT, no local material primary.
    c01_entry = stratification["per_case"]["v1f-003"]
    c01_row = by_id["v1f-003"]
    assert c01_row["oracle_status"] == "SUFFICIENT"
    assert c01_entry["coverage_limited"] is True
    assert not c01_row["expected_primary_evidence"]
    assert c01_entry["primary_evidence_kind"] == "NONE"
    # Any direct-primary SUFFICIENT case must bind a substantive FULL_TEXT body.
    for case_id, entry in stratification["per_case"].items():
        row = by_id[case_id]
        if row["oracle_status"] == "SUFFICIENT" and row["expected_primary_evidence"]:
            assert entry["primary_evidence_kind"] == "FULL_TEXT_BODY"


def test_coverage_limited_sufficient_without_local_primary_passes(tmp_path):
    """c01-equivalent regression: SUFFICIENT + coverage_limited + NONE + empty
    expected_primary_evidence must pass validation (public-world SUFFICIENT is
    not downgraded by a local coverage gap)."""
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    # v1f-003 is the c01-equivalent fixture case.
    entry = stratification["per_case"]["v1f-003"]
    row = next(r for r in rows if r["case_id"] == "v1f-003")
    assert row["oracle_status"] == "SUFFICIENT"
    assert entry["coverage_limited"] is True
    assert entry["news_content_state"] != "FULL_TEXT"
    assert entry["primary_evidence_kind"] == "NONE"
    assert not row["expected_primary_evidence"]
    validated = validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )
    assert validated["case_count"] == 12


def test_eight_k_shell_with_empty_primary_passes(tmp_path):
    """A truthful EIGHT_K_SHELL record with empty expected_primary_evidence is
    allowed for SUFFICIENT/PARTIAL/ABSTAIN under the corrected contract."""
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    entry = stratification["per_case"]["v1f-005"]
    row = next(r for r in rows if r["case_id"] == "v1f-005")
    assert entry["primary_evidence_kind"] == "EIGHT_K_SHELL"
    assert not row["expected_primary_evidence"]
    validated = validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )
    assert validated["case_count"] == 12


def test_eight_k_shell_with_direct_primary_is_rejected(tmp_path):
    """EIGHT_K_SHELL + non-empty expected_primary_evidence stays rejected."""
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    # v1f-001 is a direct-primary SUFFICIENT case; shell kind is contradictory.
    stratification["per_case"]["v1f-001"]["primary_evidence_kind"] = "EIGHT_K_SHELL"
    with pytest.raises(ValueError, match="primary_evidence_kind"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_manifest_schema_roundtrip_passes(tmp_path):
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    validated = validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )
    assert validated["schema_version"] == STAGE1_MANIFEST_SCHEMA
    assert validated["case_count"] == 12


def test_missing_or_pending_approval_is_rejected(tmp_path):
    rows, cases, stratification, _manifest = _fixture_dataset(tmp_path, approved=False)
    with pytest.raises(ValueError, match="approval"):
        validate_stage1_dataset_manifest(
            dict(_manifest), cases, stratification=stratification
        )


def test_duplicate_case_ids_rejected(tmp_path):
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    manifest["ordered_case_ids"] = list(manifest["ordered_case_ids"]) + ["v1f-001"]
    with pytest.raises(ValueError, match="unique"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_reordered_case_ids_rejected(tmp_path):
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    manifest["ordered_case_ids"] = list(reversed(manifest["ordered_case_ids"]))
    with pytest.raises(ValueError, match="one-to-one"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_case_list_hash_mismatch_rejected(tmp_path):
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    manifest["case_list_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="case_list_sha256"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_content_hash_mismatch_rejected(tmp_path):
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    manifest["dataset_content_sha256"] = "1" * 64
    with pytest.raises(ValueError, match="dataset_content_sha256"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_case_count_mismatch_rejected(tmp_path):
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    manifest["case_count"] = 11
    with pytest.raises(ValueError, match="case_count"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_unknown_eligible_or_second_pass_ids_rejected(tmp_path):
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    manifest["a3_eligible_ids"] = ["not-a-case"]
    with pytest.raises(ValueError, match="unknown case ids"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_answer_hint_leakage_rejected(tmp_path):
    rows, cases, stratification, _manifest = _fixture_dataset(tmp_path)
    leaked = dict(cases[0].model_dump())
    leaked["question"] = "Why did TSLA fall after fixture-label-0 earnings?"
    leaked_cases = [GoldenCase.model_validate(leaked)] + list(cases[1:])
    # Recompute the content hash so the manifest matches the mutated rows and
    # the leakage rule itself is the one under test.
    manifest = dict(_manifest)
    manifest["dataset_content_sha256"] = dataset_content_sha256(leaked_cases)
    with pytest.raises(ValueError, match="leakage"):
        validate_stage1_dataset_manifest(
            manifest, leaked_cases, stratification=stratification
        )


def test_coverage_limited_cases_are_never_full_text(tmp_path):
    rows, cases, stratification, manifest = _fixture_dataset(tmp_path)
    per_case = stratification["per_case"]
    for case_id, entry in per_case.items():
        if entry["coverage_limited"]:
            assert entry["news_content_state"] != "FULL_TEXT"
    coverage = stratification["strata"]["coverage"]
    assert coverage["coverage_limited"] == sum(
        1 for entry in per_case.values() if entry["coverage_limited"]
    )


def test_loader_rejects_disallowed_parent(tmp_path):
    rows = make_stage1_cases()
    rows[0]["lineage"]["source"] = "legacy:not-a-real-parent"
    path = tmp_path / "bad.jsonl"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )
    cases = load_golden_cases(path)
    stratification = make_stratification(rows)
    manifest = make_dataset_manifest(rows, stratification)
    with pytest.raises(ValueError, match="parent_case_id"):
        validate_stage1_dataset_manifest(manifest, cases, stratification=stratification)


def test_stage1_slot_parent_ids_are_allowed(tmp_path):
    """Q-011 packets bind stratification slots as c01..c12.

    The validator allow-list must therefore accept c01..c12 in addition to
    the legacy T4/golden parents (M7-2 stratification contract).
    """
    rows = make_stage1_cases()
    for index, row in enumerate(rows, start=1):
        row["lineage"]["source"] = f"legacy:c{index:02d}"
    path = tmp_path / "slots.jsonl"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )
    cases = load_golden_cases(path)
    stratification = make_stratification(rows)
    manifest = make_dataset_manifest(rows, stratification)
    validated = validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )
    assert validated["case_count"] == 12
    parents = {
        entry["parent_case_id"] for entry in stratification["per_case"].values()
    }
    assert parents <= {"c01", "c02", "c03", "c04", "c05", "c06",
                       "c07", "c08", "c09", "c10", "c11", "c12"}
