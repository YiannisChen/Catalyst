"""M7-9: T4/user-smoke regression + migration regression.

Migration regression reads the sealed M1 baseline and M5 Gate A/B artifacts
through their existing adapters, then compares the M6 V1 app/SSE output on
identical request facts. It compares only locked structural invariants
(request facts, runtime identity, ContextPack identity, claim lineage,
status/refusal normalization, leakage), declares comparability_declared=false
whenever data/model/environment identity differs, and makes no direct
quality/equality claim. Legacy eval remains a sealed baseline reader only.
"""
from __future__ import annotations

import json

import pytest

from catalyst_eval.v1_1.migration_regression import (
    BaselineFacts,
    MigrationRegressionResult,
    V1Facts,
    compare_structural_invariants,
    read_baseline_facts,
)


def _facts(**overrides) -> dict:
    base = dict(
        case_id="g006",
        request_facts={
            "ticker": "TSLA",
            "trade_date": "2025-07-24",
            "query": "Why did TSLA fall?",
        },
        runtime_identity_ref="runtime-id:7a004",
        runtime_identity_hash="d" * 64,
        context_pack_identity="pack:baseline",
        claim_lineage=("claim:baseline:1",),
        status_normalization="SUFFICIENT",
        refusal_normalization=None,
        leakage_findings=(),
    )
    base.update(overrides)
    return base


def _baseline(**overrides) -> BaselineFacts:
    return BaselineFacts(**_facts(**overrides))


def _v1(**overrides) -> V1Facts:
    return V1Facts(**_facts(**overrides))


def test_identical_structural_invariants_declare_comparable_only_when_q002_recovered():
    # Identical invariants are comparable only when Q-002 is explicitly
    # recovered; the sealed baseline keeps promoted_env_recovered=false so the
    # default verdict is MANDATORY NON-COMPARABLE (Batch-B corrective).
    result = compare_structural_invariants(_baseline(), _v1(), q002_recovered=True)
    assert isinstance(result, MigrationRegressionResult)
    assert result.comparability_declared is True
    assert result.invariant_mismatches == ()
    unrecovered = compare_structural_invariants(_baseline(), _v1())
    assert unrecovered.comparability_declared is False
    assert any("Q-002" in m for m in unrecovered.invariant_mismatches)


def test_runtime_identity_difference_declares_non_comparable():
    result = compare_structural_invariants(
        _baseline(), _v1(runtime_identity_hash="e" * 64)
    )
    assert result.comparability_declared is False
    assert any("runtime identity" in m for m in result.invariant_mismatches)


def test_request_facts_mismatch_is_flagged():
    result = compare_structural_invariants(
        _baseline(), _v1(request_facts={"ticker": "TSLA", "trade_date": "2025-07-24", "query": "other"})
    )
    assert result.comparability_declared is False
    assert any("request facts" in m for m in result.invariant_mismatches)


def test_status_or_refusal_normalization_mismatch_is_flagged():
    result = compare_structural_invariants(
        _baseline(), _v1(status_normalization="PARTIAL")
    )
    assert result.comparability_declared is False
    assert any("status" in m for m in result.invariant_mismatches)
    result2 = compare_structural_invariants(
        _baseline(), _v1(refusal_normalization="insufficient_public_evidence")
    )
    assert any("refusal" in m for m in result2.invariant_mismatches)


def test_leakage_findings_are_flagged():
    result = compare_structural_invariants(
        _baseline(), _v1(leakage_findings=("prompt:analyst: oracle status",))
    )
    assert any("leakage" in m for m in result.invariant_mismatches)


def test_context_pack_and_claim_lineage_mismatch_are_flagged():
    result = compare_structural_invariants(
        _baseline(), _v1(context_pack_identity="pack:other")
    )
    assert any("context pack" in m for m in result.invariant_mismatches)
    result2 = compare_structural_invariants(
        _baseline(), _v1(claim_lineage=("claim:other:1",))
    )
    assert any("claim lineage" in m for m in result2.invariant_mismatches)


def test_read_baseline_facts_roundtrip(tmp_path):
    payload = {
        "schema_version": "baseline_v1",
        "request_facts": {"ticker": "TSLA", "trade_date": "2025-07-24", "query": "q"},
        "runtime_identity": {"ref": "runtime-id:7a004", "hash": "d" * 64},
        "context_pack_identity": "pack:baseline",
        "claim_lineage": ["claim:baseline:1"],
        "status_normalization": "SUFFICIENT",
        "refusal_normalization": None,
        "leakage_findings": [],
        "comparability": {
            "promoted_env_recovered": False,
            "promoted_env_reason": "q_002_promoted_environment_tuple_unrecovered",
        },
    }
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    facts = read_baseline_facts(path)
    assert facts.runtime_identity_ref == "runtime-id:7a004"
    assert facts.request_facts["ticker"] == "TSLA"


def test_read_baseline_facts_rejects_missing_identity(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "baseline_v1",
                "comparability": {
                    "promoted_env_recovered": False,
                    "promoted_env_reason": "q_002_promoted_environment_tuple_unrecovered",
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="runtime_identity"):
        read_baseline_facts(path)
