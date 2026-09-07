"""Q-011 evidence-ground-truth contract (fixture-only, no human approval).

The operative Q-011 packet is unsigned and non-authoritative. This suite
locks the fail-closed contract the authoritative Stage-1 dataset must meet
before it may be sealed under M7-2:

- a resolved case must carry the evidence-level ground truth required by the
  M7 plan and retrieval metrics (evidence judgments that bind every expected
  primary evidence ID with the locked schema: canonical identity, role,
  support, materiality, temporal/cutoff eligibility, independence group);
- evidence identities must be compatible with the identity-bound
  PoolManifest/dataset contracts; no invented ``legacy:*`` identity may
  become authoritative (``legacy:`` is permitted only on lineage.source);
- corrective-required/recoverable cases must carry acceptable initial-task
  and corrective-action truth (c09-style multi-gap recoverable coherence);
- ABSTAIN/PARTIAL/SUFFICIENT, expected-primary and coverage-limited
  semantics stay consistent with the M7-2 lock;
- missing human approval remains a hard stop even when strata counts and
  expected-primary membership are valid;
- official artifact publication is fail-closed and write-once.

The synthetic fixtures are NOT the authoritative dataset and represent no
human decision.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.loader import validate_stage1_dataset_manifest

from tests.v1_1_fixtures import (
    make_dataset_manifest,
    make_stage1_cases,
    make_stratification,
)


def _rows() -> list[dict]:
    return make_stage1_cases()


def _rebuild(rows, *, approved: bool = True, stratification=None):
    if stratification is None:
        stratification = make_stratification(rows)
    manifest = make_dataset_manifest(rows, stratification, approved=approved)
    cases = [GoldenCase.model_validate(row) for row in rows]
    return cases, stratification, manifest


def _validate(rows, *, approved: bool = True, stratification=None):
    cases, strat, manifest = _rebuild(rows, approved=approved, stratification=stratification)
    return validate_stage1_dataset_manifest(manifest, cases, stratification=strat)


def _strip_evidence_ground_truth(rows: list[dict]) -> None:
    """Emulate the current unsigned-packet generator output: resolved rows with
    empty evidence judgments, empty task/action truth, and empty
    human_confirmed_fields while counts and expected-primary membership stay
    valid."""
    for row in rows:
        row["evidence_judgments"] = []
        row["lineage"]["human_confirmed_fields"] = []
        row["expected_research_behavior"]["acceptable_initial_tasks"] = []
        row["expected_research_behavior"]["acceptable_corrective_actions"] = []


def _reslot_cases(rows: list[dict]) -> list[dict]:
    """Bind the fixture rows to Stage-1 slots c01..c12 (packet reality)."""
    out: list[dict] = []
    for index, row in enumerate(rows, start=1):
        row = copy.deepcopy(row)
        row["case_id"] = f"c{index:02d}"
        row["lineage"]["source"] = f"legacy:c{index:02d}"
        out.append(row)
    return out


def _reslot_stratification(original_rows, slotted_rows) -> dict:
    """Rebind per-case stratification keys to the c01..c12 slot ids."""
    stratification = make_stratification(original_rows)
    old_keys = [row["case_id"] for row in original_rows]
    new_keys = [row["case_id"] for row in slotted_rows]
    per_case = stratification["per_case"]
    stratification["per_case"] = {
        new_key: {**per_case[old_key], "parent_case_id": new_key}
        for old_key, new_key in zip(old_keys, new_keys)
    }
    return stratification


def _slot_manifest_subsets(manifest: dict, original_ids: list[str]) -> dict:
    """Remap manifest subset ids from the fixture ids to c01..c12 slots."""
    order = {case_id: f"c{index:02d}" for index, case_id in enumerate(original_ids, start=1)}
    for field in ("second_pass_case_ids", "a3_eligible_ids", "a4_readiness_eligible_ids"):
        manifest[field] = [order[case_id] for case_id in manifest[field]]
    return manifest


def _validate_slotted(rows, *, approved: bool):
    """Validate packet-slot rows with slot-keyed stratification + manifest."""
    original_rows = _rows()
    stratification = _reslot_stratification(original_rows, rows)
    cases, _strat, manifest = _rebuild(
        rows, approved=approved, stratification=stratification
    )
    _slot_manifest_subsets(manifest, [row["case_id"] for row in original_rows])
    return validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )


# ---------------------------------------------------------------------------
# 1. Resolved cases may not omit evidence-level retrieval ground truth
# ---------------------------------------------------------------------------

def test_resolved_expected_primary_without_evidence_judgment_is_rejected():
    """A resolved case may not claim direct primary evidence while omitting the
    human evidence judgment for that evidence ID (retrieval ground truth)."""
    rows = _rows()
    direct = next(r for r in rows if r["expected_primary_evidence"])
    primary_id = direct["expected_primary_evidence"][0]
    direct["evidence_judgments"] = [
        j for j in direct["evidence_judgments"] if j["evidence_id"] != primary_id
    ]
    with pytest.raises(ValueError, match="evidence_judgments"):
        _validate(rows)


def test_resolved_expected_primary_must_be_judged_primary_support_temporal_eligible():
    """The certified primary membership must be judged primary_support and
    cutoff-eligible; a secondary-only or ineligible judgment is not enough."""
    rows = _rows()
    direct = next(r for r in rows if r["expected_primary_evidence"])
    primary_id = direct["expected_primary_evidence"][0]
    for judgment in direct["evidence_judgments"]:
        if judgment["evidence_id"] == primary_id:
            judgment["role"] = "secondary_support"
            judgment["temporal_eligible"] = False
    with pytest.raises(ValueError, match="primary_support"):
        _validate(rows)


def test_relevant_judgment_requires_independence_group():
    """Role/relevance judgments used by retrieval metrics must declare the
    human independence group (duplicate-adjusted precision / independent-group
    recall ground truth)."""
    rows = _rows()
    direct = next(r for r in rows if r["expected_primary_evidence"])
    direct["evidence_judgments"][0]["independence_group"] = None
    with pytest.raises(ValueError, match="independence_group"):
        _validate(rows)


# ---------------------------------------------------------------------------
# 2. Evidence identity compatibility (no invented legacy:* evidence)
# ---------------------------------------------------------------------------

def test_legacy_prefix_expected_primary_identity_is_rejected():
    """``legacy:`` is a lineage.parent marker, not a corpus evidence identity.
    A resolved dataset may not mint legacy:* evidence ids authoritatively."""
    rows = _rows()
    direct = next(r for r in rows if r["expected_primary_evidence"])
    direct["expected_primary_evidence"] = ["legacy:c01"]
    with pytest.raises(ValueError, match="legacy:"):
        _validate(rows)


def test_legacy_prefix_evidence_judgment_identity_is_rejected():
    rows = _rows()
    direct = next(r for r in rows if r["expected_primary_evidence"])
    first = direct["evidence_judgments"][0]
    direct["evidence_judgments"] = [
        {
            **first,
            "evidence_id": "legacy:c01",
            "chunk_id": "legacy:c01",
            "canonical_asset_id": "asset:legacy:c01",
            "canonical_content_version_id": "ver:legacy:c01",
        }
    ]
    with pytest.raises(ValueError, match="legacy:"):
        _validate(rows)


# ---------------------------------------------------------------------------
# 3. Corrective-research truth coherence (c09-style)
# ---------------------------------------------------------------------------

def test_corrective_required_case_without_initial_task_truth_is_rejected():
    rows = _rows()
    corrective = next(
        r for r in rows
        if r["expected_research_behavior"]["corrective_recoverable"]
    )
    corrective["expected_research_behavior"]["acceptable_initial_tasks"] = []
    with pytest.raises(ValueError, match="acceptable_initial_tasks"):
        _validate(rows)


def test_corrective_required_case_without_corrective_action_truth_is_rejected():
    rows = _rows()
    corrective = next(
        r for r in rows
        if r["expected_research_behavior"]["corrective_recoverable"]
    )
    corrective["expected_research_behavior"]["acceptable_corrective_actions"] = []
    with pytest.raises(ValueError, match="acceptable_corrective_actions"):
        _validate(rows)


def test_multi_gap_recoverable_case_with_task_and_action_truth_passes():
    """c09-style recoverable case: corrective_required=true with >=2 gap reason
    codes plus acceptable initial-task/action truth is valid ground truth."""
    rows = _rows()
    corrective = next(
        r for r in rows
        if r["expected_research_behavior"]["corrective_recoverable"]
    )
    assert corrective["expected_research_behavior"]["corrective_required"] is True
    assert len(corrective["expected_research_behavior"]["expected_gap_reason_codes"]) >= 2
    assert corrective["expected_research_behavior"]["acceptable_initial_tasks"]
    assert corrective["expected_research_behavior"]["acceptable_corrective_actions"]
    validated = _validate(rows)
    assert validated["case_count"] == 12


def test_resolved_case_requires_human_confirmed_fields():
    rows = _rows()
    rows[0]["lineage"]["human_confirmed_fields"] = []
    with pytest.raises(ValueError, match="human_confirmed_fields"):
        _validate(rows)


# ---------------------------------------------------------------------------
# 4. ABSTAIN / coverage-limited semantics remain consistent
# ---------------------------------------------------------------------------

def test_abstain_without_primary_or_judgments_remains_valid():
    """An ABSTAIN public-world case with no local primary evidence and no
    retrieval judgments stays valid; it is only non-scorable for retrieval."""
    rows = _rows()
    abstain = next(r for r in rows if r["oracle_status"] == "ABSTAIN")
    assert not abstain["expected_primary_evidence"]
    assert not abstain["evidence_judgments"]
    assert abstain["expected_refusal_reason"]
    assert abstain["lineage"]["human_confirmed_fields"]
    validated = _validate(rows)
    assert validated["case_count"] == 12


def test_coverage_limited_sufficient_without_local_primary_remains_valid():
    """The c01-equivalent regression: public-world SUFFICIENT + locally
    coverage-limited with no material primary stays valid without judgments."""
    rows = _rows()
    case = next(r for r in rows if r["case_id"] == "v1f-003")
    assert case["oracle_status"] == "SUFFICIENT"
    assert not case["expected_primary_evidence"]
    assert not case["evidence_judgments"]
    validated = _validate(rows)
    assert validated["case_count"] == 12


# ---------------------------------------------------------------------------
# 5. Missing human approval is a hard stop
# ---------------------------------------------------------------------------

def test_full_annotation_without_approval_is_still_rejected():
    """Even a fully annotated identity-consistent dataset cannot become
    authoritative without approval_authority/approved_at."""
    rows = _rows()
    with pytest.raises(ValueError, match="approval"):
        _validate(rows, approved=False)


# ---------------------------------------------------------------------------
# 6. The unsigned packet cannot become authoritative on counts alone
# ---------------------------------------------------------------------------

def test_unsigned_packet_shape_with_valid_counts_is_rejected_without_approval():
    """Q-011 slot shape (9 SUFFICIENT / 2 PARTIAL / 1 ABSTAIN) plus valid
    expected-primary membership does not make the unsigned packet
    authoritative while approval is absent."""
    rows = _reslot_cases(_rows())
    oracle = {r["oracle_status"] for r in rows}
    assert oracle == {"SUFFICIENT", "PARTIAL", "ABSTAIN"}
    counts = {}
    for r in rows:
        counts[r["oracle_status"]] = counts.get(r["oracle_status"], 0) + 1
    assert counts["SUFFICIENT"] == 9 and counts["PARTIAL"] == 2
    assert counts["ABSTAIN"] == 1
    assert sum(len(r["expected_primary_evidence"]) for r in rows) == 6
    _strip_evidence_ground_truth(rows)
    with pytest.raises(ValueError, match="approval"):
        _validate_slotted(rows, approved=False)


def test_unsigned_packet_shape_rejected_even_with_invented_approval():
    """Even if approval fields were (incorrectly) supplied, the packet-shaped
    resolved rows are rejected until evidence judgments, task/action truth,
    and human-confirmed lineage exist."""
    rows = _reslot_cases(_rows())
    _strip_evidence_ground_truth(rows)
    with pytest.raises(ValueError, match="evidence"):
        _validate_slotted(rows, approved=True)


# ---------------------------------------------------------------------------
# 7. Official artifact publication is fail-closed and write-once
# ---------------------------------------------------------------------------

def _approved_payload(tmp_path: Path):
    from catalyst_eval.v1_1 import loader

    rows = _rows()
    cases, stratification, manifest = _rebuild(rows)
    return loader, rows, cases, stratification, manifest


def _write_dataset_files(tmp_path: Path, rows, stratification, manifest):
    from catalyst_eval.v1_1 import loader

    return loader.write_stage1_dataset_files(
        tmp_path,
        rows=rows,
        stratification=stratification,
        manifest=manifest,
    )


def test_publish_creates_all_files_atomically_and_no_partials(tmp_path):
    loader, rows, _cases, stratification, manifest = _approved_payload(tmp_path)
    digests = _write_dataset_files(tmp_path, rows, stratification, manifest)
    assert set(digests) == {
        "v1_1_stage1_cases.jsonl",
        "v1_1_stage1_stratification.json",
        "v1_1_stage1_dataset_manifest.json",
    }
    for name in digests:
        data = (tmp_path / name).read_bytes()
        assert len(data) > 0
        import hashlib
        assert hashlib.sha256(data).hexdigest() == digests[name]
    # No atomic temp/partial names may remain.
    assert not list(tmp_path.glob(".*.tmp"))
    assert {p.name for p in tmp_path.iterdir()} == set(digests)


def test_publish_identical_bytes_is_idempotent(tmp_path):
    loader, rows, _cases, stratification, manifest = _approved_payload(tmp_path)
    first = _write_dataset_files(tmp_path, rows, stratification, manifest)
    second = _write_dataset_files(tmp_path, rows, stratification, manifest)
    assert second == first


def test_publish_conflicting_bytes_raise_typed_conflict_and_preserve_file(tmp_path):
    loader, rows, _cases, stratification, manifest = _approved_payload(tmp_path)
    digests = _write_dataset_files(tmp_path, rows, stratification, manifest)

    # A different (but still internally valid) dataset must conflict.
    changed = copy.deepcopy(rows)
    changed[0]["notes"] = "changed note"
    cases2, strat2, manifest2 = _rebuild(changed, stratification=stratification)
    with pytest.raises(loader.PublicationConflictError, match="refusing"):
        _write_dataset_files(tmp_path, changed, strat2, manifest2)
    # The original authoritative files are unchanged.
    for name, digest in digests.items():
        import hashlib
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    assert not list(tmp_path.glob(".*.tmp"))


def test_publish_fails_closed_without_approval_before_writing(tmp_path):
    loader, rows, _cases, _stratification, _manifest = _approved_payload(tmp_path)
    cases, stratification, manifest = _rebuild(rows, approved=False)
    with pytest.raises(ValueError, match="approval"):
        _write_dataset_files(tmp_path, rows, stratification, manifest)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 8. Positive path: fully annotated approved dataset drives non-empty metrics
# ---------------------------------------------------------------------------

def test_fully_annotated_approved_dataset_validates_and_drives_retrieval():
    """A fully annotated, identity-consistent, approved dataset validates and
    produces non-empty retrieval denominators (the Q-011 target state)."""
    rows = _rows()
    cases, stratification, manifest = _rebuild(rows)
    validated = validate_stage1_dataset_manifest(
        manifest, cases, stratification=stratification
    )
    assert validated["case_count"] == 12
    for case in cases:
        judged = {j.evidence_id for j in case.evidence_judgments}
        if case.expected_primary_evidence:
            assert set(case.expected_primary_evidence) <= judged
            assert case.lineage.human_confirmed_fields

    from catalyst_eval.benchmark.pool_manifest import PoolArm, PoolManifest
    from catalyst_eval.v1_1.retrieval_metrics import (
        RetrievalResult,
        compute_retrieval_metrics,
    )

    def _pool(case: GoldenCase):
        inventory = tuple(
            dict.fromkeys((*(j.evidence_id for j in case.evidence_judgments),))
        )
        data = dict(
            schema_version="1.0.0",
            case_id=case.case_id,
            arms=(
                PoolArm(arm="lexical", version="1.0.0", top_k=8),
                PoolArm(arm="dense", version="1.0.0", top_k=8),
                PoolArm(arm="hybrid", version="1.0.0", top_k=8),
                PoolArm(arm="reranked", version="1.0.0", top_k=8),
            ),
            chunk_inventory=tuple(sorted(set(inventory))),
            corpus_manifest_id="c" * 64,
            index_manifest_id="e" * 64,
            source_artifact_id="f" * 64,
            created_at="2026-08-19T00:00:00Z",
        )
        import hashlib
        pool_id = hashlib.sha256(
            json.dumps(
                {
                    "schema_version": data["schema_version"],
                    "case_id": data["case_id"],
                    "arms": [
                        {"arm": a.arm, "version": a.version, "top_k": a.top_k}
                        for a in data["arms"]
                    ],
                    "chunk_inventory": list(data["chunk_inventory"]),
                    "corpus_manifest_id": data["corpus_manifest_id"],
                    "index_manifest_id": data["index_manifest_id"],
                    "source_artifact_id": data["source_artifact_id"],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return PoolManifest(pool_id=pool_id, **data)

    results = [
        RetrievalResult(
            case_id=case.case_id,
            pool=_pool(case),
            ranked_evidence_ids=tuple(j.evidence_id for j in case.evidence_judgments),
        )
        for case in cases
    ]
    metrics = compute_retrieval_metrics(results, cases, top_k=8)
    assert metrics.recall_at_k.denominator > 0
    assert metrics.mrr.denominator > 0
    assert metrics.ndcg_at_k.denominator > 0
    assert metrics.primary_source_hit.denominator > 0
    assert metrics.recall_at_k.value is not None
