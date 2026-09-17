"""M7 Batch-B corrective: human audit authoritative.

Typed decision/reason vocabularies; unique case and claim rows; audit
material/citation identities must exactly match immutable run output; every
material claim and every cited unit must be audited. Citation correctness is
supported resolved citation units divided by all resolved citation units,
not one count per claim.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from catalyst_eval.v1_1.attribution_metrics import (
    RunAttributionOutput,
    RunClaimOutput,
    compute_attribution_metrics,
)
from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.output_audit import (
    AuditClaimDecision,
    Stage1OutputAudit,
    load_output_audit,
    validate_output_audit,
)
from catalyst_eval.v1_1.output_audit import _run_output_from_ledger_row

from tests.v1_1_fixtures import make_stage1_cases


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _gold() -> GoldenCase:
    return GoldenCase.model_validate(make_stage1_cases()[0])


def _run(gold: GoldenCase, *, claims=()) -> RunAttributionOutput:
    claims = claims or (
        RunClaimOutput(
            claim_id="c1", material=True,
            citation_ids=("fixture-ev-001", "fixture-ev-002"), role="PRIMARY",
        ),
    )
    return RunAttributionOutput(
        case_id=gold.case_id,
        output_status=gold.oracle_status,
        attribution_type=gold.expected_attribution_type or "EVIDENCE_BACKED_CAUSAL",
        refusal_reason=gold.expected_refusal_reason,
        claims=claims,
        latency_ms=100, tokens=200, cost_usd=0.01,
    )


def _audit(gold: GoldenCase, run: RunAttributionOutput, *, decisions=()) -> Stage1OutputAudit:
    decisions = decisions or (
        AuditClaimDecision(
            claim_id="c1", material=True,
            citation_ids=("fixture-ev-001", "fixture-ev-002"),
            decision="SUPPORT", reason_code="citations_resolve",
        ),
    )
    return Stage1OutputAudit(
        eval_id="eval:stage1:v1",
        case_id=gold.case_id,
        run_manifest_id="manifest:v1f-001",
        run_manifest_hash="b" * 64,
        result_artifact_id="result:v1f-001",
        result_artifact_hash="c" * 64,
        auditor_id="fixture-auditor",
        audited_at=_utc("2026-08-19T00:00:00Z"),
        adjudication_state="resolved",
        claims=decisions,
    )


def test_audit_decision_uses_typed_vocabulary():
    gold, run = _gold(), _run(_gold())
    with pytest.raises(ValueError, match="decision"):
        _audit(
            gold, run,
            decisions=(
                AuditClaimDecision(
                    claim_id="c1", material=True,
                    citation_ids=("fixture-ev-001",),
                    decision="MAYBE", reason_code="citations_resolve",
                ),
            ),
        )
    with pytest.raises(ValueError, match="reason_code"):
        _audit(
            gold, run,
            decisions=(
                AuditClaimDecision(
                    claim_id="c1", material=True,
                    citation_ids=("fixture-ev-001",),
                    decision="SUPPORT", reason_code="not-a-real-reason",
                ),
            ),
        )
    with pytest.raises(ValueError, match="material"):
        _audit(
            gold, run,
            decisions=(
                AuditClaimDecision(
                    claim_id="c1", material="true",
                    citation_ids=("fixture-ev-001",),
                    decision="SUPPORT", reason_code="citations_resolve",
                ),
            ),
        )


def test_runtime_limitation_reason_is_typed_and_role_bound():
    gold = _gold()
    limitation = RunClaimOutput(
        claim_id="limitation-1",
        material=False,
        citation_ids=(),
        role="LIMITATION",
        statement="The runtime could not establish causal attribution.",
    )
    run = _run(gold, claims=(limitation,))
    audit = _audit(
        gold,
        run,
        decisions=(
            AuditClaimDecision(
                claim_id="limitation-1",
                material=False,
                citation_ids=(),
                decision="SUPPORT",
                reason_code="runtime_limitation_verified",
            ),
        ),
    )
    assert validate_output_audit(audit, run, gold, eval_id="eval:stage1:v1")


@pytest.mark.parametrize(
    "kwargs",
    (
        {"material": True},
        {"citation_ids": ("e1",)},
        {"decision": "PARTIAL_SUPPORT"},
        {"decision": "UNSUPPORTED"},
    ),
)
def test_runtime_limitation_reason_rejects_invalid_decision_combinations(kwargs):
    values = {
        "claim_id": "limitation-1",
        "material": False,
        "citation_ids": (),
        "decision": "SUPPORT",
        "reason_code": "runtime_limitation_verified",
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        AuditClaimDecision(**values)


def test_runtime_limitation_reason_rejects_non_limitation_role():
    gold = _gold()
    claim = RunClaimOutput(
        claim_id="context-1",
        material=False,
        citation_ids=(),
        role="CONTEXT",
        statement="A runtime context fact.",
    )
    run = _run(gold, claims=(claim,))
    audit = _audit(
        gold,
        run,
        decisions=(
            AuditClaimDecision(
                claim_id="context-1",
                material=False,
                citation_ids=(),
                decision="SUPPORT",
                reason_code="runtime_limitation_verified",
            ),
        ),
    )
    with pytest.raises(ValueError, match="LIMITATION"):
        validate_output_audit(audit, run, gold, eval_id="eval:stage1:v1")


def test_nonmaterial_limitation_cannot_use_non_runtime_audit_reason():
    gold = _gold()
    claim = RunClaimOutput(
        claim_id="limitation-1",
        material=False,
        citation_ids=(),
        role="LIMITATION",
        statement="The runtime limitation is observable.",
    )
    run = _run(gold, claims=(claim,))
    audit = _audit(
        gold,
        run,
        decisions=(
            AuditClaimDecision(
                claim_id="limitation-1",
                material=False,
                citation_ids=(),
                decision="SUPPORT",
                reason_code="no_causal_support",
            ),
        ),
    )
    with pytest.raises(ValueError, match="runtime_limitation_verified"):
        validate_output_audit(audit, run, gold, eval_id="eval:stage1:v1")


def test_output_audit_rejects_claims_with_missing_truth_fields():
    gold = _gold()
    row = SimpleNamespace(
        provider_calls=0,
        run_facts={
            "claims": [{"claim_id": "c1", "citation_ids": []}],
        }
    )
    with pytest.raises(ValueError, match="material.*role|role.*material"):
        _run_output_from_ledger_row(row, gold)


def _run_facts_for_validation(gold: GoldenCase, **overrides):
    facts = {
        "schema_version": "v1_1_stage1_run_facts_v1",
        "case_id": gold.case_id,
        "output_status": "ABSTAIN",
        "attribution_type": "NO_MATERIAL_PUBLIC_CATALYST",
        "refusal_reason": "no supported catalyst",
        "refusal_reason_available": True,
        "claims": [],
        "sanity_tasks_completed": [],
        "latency_ms": 10,
        "tokens": 20,
        "cost_usd": 0.01,
        "model_limited": False,
        "trajectory": {
            "corrective_triggered": False,
            "rounds_executed": 0,
            "gap_reason_codes": [],
            "corrective_actions": [],
            "research_fingerprints": [],
            "evidence_delta_ids": [],
            "produced_structure": False,
        },
        "retrieval": {
            "observed": False,
            "pool": None,
            "ranked_evidence_ids": [],
            "candidate_evidence_ids": [],
            "reranker_contributed": False,
            "latency_ms": None,
            "degraded": False,
            "ticker_violations": [],
            "cutoff_violations": [],
        },
        "provider_accounting": {
            "provider_calls": 0,
            "tokens_in": 10,
            "tokens_out": 10,
            "cost_usd": 0.01,
            "cost_method": "reported",
        },
        "run_id": "run:validation",
    }
    facts.update(overrides)
    return facts


@pytest.mark.parametrize(
    ("field", "value", "pattern"),
    (
        ("output_status", "UNKNOWN", "output_status"),
        ("attribution_type", "NOT_A_BINDING_VALUE", "attribution_type"),
        ("refusal_reason_available", 1, "refusal_reason_available"),
        ("claims", [{"claim_id": "c1", "material": "true", "role": "PRIMARY", "citation_ids": []}], "material"),
        ("claims", [{"claim_id": "c1", "material": True, "role": "NOT_A_ROLE", "citation_ids": []}], "role"),
    ),
)
def test_run_facts_reconstruction_rejects_malformed_observed_fields(
    field, value, pattern
):
    gold = _gold()
    row = SimpleNamespace(
        provider_calls=0,
        run_facts=_run_facts_for_validation(gold, **{field: value}),
    )
    with pytest.raises(ValueError, match=pattern):
        _run_output_from_ledger_row(row, gold)


def test_output_audit_reconstruction_preserves_refusal_reason_availability():
    gold = _gold()
    row = SimpleNamespace(
        provider_calls=0,
        run_facts=_run_facts_for_validation(
            gold,
            claims=[
                {
                    "claim_id": "c1",
                    "material": False,
                    "role": "CONTEXT",
                    "citation_ids": [],
                }
            ],
        )
    )
    output = _run_output_from_ledger_row(row, gold)
    assert output.refusal_reason == "no supported catalyst"
    assert output.refusal_reason_available is True


def test_output_audit_reconstruction_rejects_ledger_provider_call_mismatch():
    gold = _gold()
    row = SimpleNamespace(
        provider_calls=1,
        run_facts=_run_facts_for_validation(gold),
    )
    with pytest.raises(ValueError, match="provider_calls"):
        _run_output_from_ledger_row(row, gold)


def test_audit_rejects_duplicate_case_and_claim_rows(tmp_path):
    gold = _gold()
    run = _run(gold)
    audit = _audit(gold, run)
    path = tmp_path / "audit.jsonl"
    path.write_text(
        audit.model_dump_json() + "\n" + audit.model_dump_json() + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_output_audit(path)

    dup_claims = _audit(
        gold, run,
        decisions=(
            AuditClaimDecision(
                claim_id="c1", material=True,
                citation_ids=("fixture-ev-001",),
                decision="SUPPORT", reason_code="citations_resolve",
            ),
            AuditClaimDecision(
                claim_id="c1", material=True,
                citation_ids=("fixture-ev-002",),
                decision="SUPPORT", reason_code="citations_resolve",
            ),
        ),
    )
    path2 = tmp_path / "audit_dup_claims.jsonl"
    path2.write_text(dup_claims.model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_output_audit(path2)


def test_audit_citation_identities_must_exactly_match_run_output():
    """Audited citation ids must equal the immutable run output claim ids."""
    gold = _gold()
    run = _run(gold)  # c1 cites fixture-ev-001, fixture-ev-002
    audit = _audit(
        gold, run,
        decisions=(
            AuditClaimDecision(
                claim_id="c1", material=True,
                citation_ids=("fixture-ev-001",),  # missing fixture-ev-002
                decision="SUPPORT", reason_code="citations_resolve",
            ),
        ),
    )
    with pytest.raises(ValueError, match="citation"):
        validate_output_audit(audit, run, gold, eval_id="eval:stage1:v1")


def test_audit_requires_every_cited_unit_audited():
    """A non-material claim that is never audited still leaves its cited
    units unaudited and must fail validation."""
    gold = _gold()
    run = _run(
        gold,
        claims=(
            RunClaimOutput(
                claim_id="c1", material=True,
                citation_ids=("fixture-ev-001",), role="PRIMARY",
            ),
            RunClaimOutput(
                claim_id="c2", material=False,
                citation_ids=("fixture-ev-002",), role="CONTEXT",
            ),
        ),
    )
    audit = _audit(gold, run, decisions=(
        AuditClaimDecision(
            claim_id="c1", material=True,
            citation_ids=("fixture-ev-001",),
            decision="SUPPORT", reason_code="citations_resolve",
        ),
    ))
    with pytest.raises(ValueError, match="missing from the human audit"):
        validate_output_audit(audit, run, gold, eval_id="eval:stage1:v1")


def test_citation_correctness_counts_units_not_claims():
    """A claim with two citations counts as two units, not one."""
    gold = _gold()
    run = _run(gold)
    # c1 (2 citations) supported; c2 (1 citation) unsupported -> 2/3.
    run = RunAttributionOutput(
        case_id=gold.case_id,
        output_status=gold.oracle_status,
        attribution_type=gold.expected_attribution_type or "EVIDENCE_BACKED_CAUSAL",
        refusal_reason=gold.expected_refusal_reason,
        claims=(
            RunClaimOutput(claim_id="c1", material=True,
                           citation_ids=("fixture-ev-001", "fixture-ev-002"),
                           role="PRIMARY"),
            RunClaimOutput(claim_id="c2", material=False,
                           citation_ids=("fixture-ev-003",), role="CONTEXT"),
        ),
    )
    audit = _audit(
        gold, run,
        decisions=(
            AuditClaimDecision(
                claim_id="c1", material=True,
                citation_ids=("fixture-ev-001", "fixture-ev-002"),
                decision="SUPPORT", reason_code="citations_resolve",
            ),
            AuditClaimDecision(
                claim_id="c2", material=False,
                citation_ids=("fixture-ev-003",),
                decision="UNSUPPORTED", reason_code="no_causal_support",
            ),
        ),
    )
    metrics = compute_attribution_metrics([run], [audit], [gold])
    assert metrics.citation_correctness.denominator == 3  # three citation units
    assert metrics.citation_correctness.numerator == 2
    assert metrics.citation_correctness.value == pytest.approx(2 / 3)
