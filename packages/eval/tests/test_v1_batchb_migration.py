"""M7 Batch-B corrective: migration regression.

The migration regression must consume the sealed M1/M5 adapters (the sealed
baseline report and the M5 Gate A artifact) and require
``comparability_declared=false`` while Q-002 is unrecovered. The legacy
reader may never declare comparability on its own.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalyst_eval.v1_1.migration_regression import (
    BaselineFacts,
    V1Facts,
    compare_structural_invariants,
    read_baseline_facts,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SEALED_BASELINE = (
    REPO_ROOT / "data/baseline/reports/v1_1_baseline_621375bc_corrective_seal.json"
)
M5_GATE_A = (
    REPO_ROOT / "data/baseline/reports/v1_1_m5_gate_a_sealed_baseline_integrity.json"
)


def _v1_facts() -> V1Facts:
    return V1Facts(
        case_id="g006",
        request_facts={"ticker": "TSLA", "trade_date": "2025-07-24", "query": "q"},
        runtime_identity_ref="runtime-id:7a004",
        runtime_identity_hash="d" * 64,
        context_pack_identity="pack:baseline",
        claim_lineage=("claim:baseline:1",),
        status_normalization="SUFFICIENT",
        refusal_normalization=None,
        leakage_findings=(),
    )


def test_migration_regression_reads_sealed_baseline_through_adapter():
    """The sealed M1 baseline is consumed through the sealed-baseline reader;
    Q-002 promoted-env unrecovered is preserved."""
    facts = read_baseline_facts(SEALED_BASELINE)
    assert isinstance(facts, BaselineFacts)
    assert facts.q002_promoted_env_recovered is False
    assert facts.q002_promoted_env_reason == "q_002_promoted_environment_tuple_unrecovered"


def test_migration_regression_never_declares_comparable_while_q002_unrecovered():
    """Even byte-identical structural invariants must stay NON-COMPARABLE
    while Q-002 is unrecovered."""
    baseline = read_baseline_facts(SEALED_BASELINE)
    v1 = V1Facts(
        case_id=baseline.case_id,
        request_facts=dict(baseline.request_facts),
        runtime_identity_ref=baseline.runtime_identity_ref,
        runtime_identity_hash=baseline.runtime_identity_hash,
        context_pack_identity=baseline.context_pack_identity,
        claim_lineage=tuple(baseline.claim_lineage),
        status_normalization=baseline.status_normalization,
        refusal_normalization=baseline.refusal_normalization,
        leakage_findings=tuple(baseline.leakage_findings),
    )
    result = compare_structural_invariants(
        baseline, v1, q002_recovered=baseline.q002_promoted_env_recovered
    )
    assert result.comparability_declared is False
    assert result.identity_fields_differ is False
    assert "Q-002" in " ".join(result.invariant_mismatches)


def test_migration_regression_requires_m5_gate_a_artifact_identity():
    """The sealed M5 Gate A artifact must be consumed for the run-token
    identities; its Q-002 marker must match the baseline."""
    payload = json.loads(M5_GATE_A.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "v1_1_m5_gate_a_v1"
    assert payload["q002_non_comparable"]["promoted_env_recovered"] is False
    assert payload["q002_non_comparable"]["promoted_env_reason"] == (
        "q_002_promoted_environment_tuple_unrecovered"
    )
    # Gate A's sealed report sha must equal the sealed baseline's sha.
    baseline = read_baseline_facts(SEALED_BASELINE)
    assert baseline.q002_promoted_env_recovered is False


def test_read_baseline_facts_rejects_non_sealed_artifact(tmp_path):
    """A hand-written JSON that is not the sealed M1 artifact fails closed."""
    path = tmp_path / "fake_baseline.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "baseline_v1",
                "request_facts": {"ticker": "TSLA"},
                "runtime_identity": {"ref": "r", "hash": "d" * 64},
                "comparability": {
                    "promoted_env_recovered": True,
                    "promoted_env_reason": "recovered",
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Q-002|sealed|promoted_env"):
        read_baseline_facts(path)
