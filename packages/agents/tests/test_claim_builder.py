"""M5-5: deterministic ClaimPlan construction.

Frozen §6.5; Phase 4 TSD §§23–24; M5 plan M5-5. build_claim_plan copies or
mechanically normalizes only Analyst-approved hypotheses/statements/
mechanisms/roles/evidence links/conflict refs and fixed limitation templates.
At most one PRIMARY claim; no confidence; no semantic prose parsing
(CLAIM-01); rejected hypotheses are never promoted; evidence refs are exactly
the typed bindings in the normalized assessment.
"""
from __future__ import annotations

from typing import Any

import pytest

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    AttributionStatus,
    CandidateHypothesis,
    EvidenceDecision,
    EvidenceDisposition,
    HypothesisRole,
    MagnitudeFit,
)
from catalyst_agents.attribution.assessment import (
    EvidenceAssessment,
    normalize_decision,
)
from catalyst_agents.attribution.claims import (
    ClaimPlan,
    ClaimRole,
    build_claim_plan,
)
from catalyst_agents.retrieval.task import EvidenceNeed


class FakeInventoryItem:
    def __init__(self, *, evidence_id: str, material_capability: str = "MATERIAL_CAPABLE",
                 evidence_role: str = "DIRECT_PRIMARY", independence_status: str = "KNOWN_GROUP",
                 independence_group_id: str | None = None):
        self.evidence_id = evidence_id
        self.material_capability = material_capability
        self.evidence_role = evidence_role
        self.independence_status = independence_status
        self.independence_group_id = independence_group_id


class FakeContextPack:
    def __init__(self, *, run_id: str = "run:1", round: int = 1,
                 included_evidence_ids: tuple[str, ...] = ("e1", "e2"),
                 evidence_inventory: tuple[Any, ...] = (), research_history: tuple[Any, ...] = (),
                 coverage_summary: Any = None, data_coverage_gaps: tuple[Any, ...] = (),
                 capability_gaps: tuple[Any, ...] = (), retrieval_degradations: tuple[Any, ...] = (),
                 context_pack_sha256: str = "a" * 64, rendered_messages_sha256: str = "b" * 64):
        self.run_id = run_id
        self.round = round
        self.included_evidence_ids = included_evidence_ids
        self.evidence_inventory = evidence_inventory
        self.research_history = research_history
        self.coverage_summary = coverage_summary
        self.data_coverage_gaps = data_coverage_gaps
        self.capability_gaps = capability_gaps
        self.retrieval_degradations = retrieval_degradations
        self.context_pack_sha256 = context_pack_sha256
        self.rendered_messages_sha256 = rendered_messages_sha256


class FakeRegistry:
    def is_recoverable(self, need: EvidenceNeed) -> bool:
        return False


def _decision(**overrides: Any) -> AnalystDecision:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "evidence_decisions": (
            {
                "evidence_id": "e1",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
            {
                "evidence_id": "e2",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h2",),
                "reason_code": "material_support",
            },
        ),
        "candidate_hypotheses": (
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "AAPL rose on record guidance.",
                "mechanism": "Guidance raised forward revenue.",
                "supporting_evidence_ids": ("e1",),
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            },
            {
                "hypothesis_ref": "h2",
                "cause_type": "SECTOR_MOVE",
                "statement": "The sector rallied in sympathy.",
                "supporting_evidence_ids": ("e2",),
                "magnitude_fit": "PLAUSIBLE",
                "proposed_role": "SECONDARY",
            },
        ),
        "research_decision": "READY",
        "recommended_status": "SUFFICIENT",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
    }
    base.update(overrides)
    return AnalystDecision.model_validate(base)


def _assessment(*, decision: AnalystDecision | None = None, **pack_kwargs: Any) -> EvidenceAssessment:
    return normalize_decision(
        decision or _decision(),
        context_pack=FakeContextPack(**pack_kwargs),
        capability_registry=FakeRegistry(),
        policy_version="norm:v1",
        evidence_state_hash="e" * 64,
    )


def test_build_claim_plan_orders_primary_secondary_context_limitations() -> None:
    assessment = _assessment(
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
            FakeInventoryItem(evidence_id="e2", evidence_role="INDEPENDENT_REPORT"),
        ),
    )
    plan = build_claim_plan(assessment, observed_move="AAPL +9.5% on 2026-01-15")
    assert isinstance(plan, ClaimPlan)
    roles = [claim.role for claim in plan.claims if claim.role in {ClaimRole.PRIMARY, ClaimRole.SECONDARY, ClaimRole.CONTEXT}]
    assert roles[0] is ClaimRole.PRIMARY
    assert ClaimRole.SECONDARY in roles
    assert [claim.order_index for claim in plan.claims] == list(range(len(plan.claims)))
    assert sum(1 for claim in plan.claims if claim.role is ClaimRole.PRIMARY) <= 1
    assert plan.status is AttributionStatus.SUFFICIENT


def test_claim_copies_approved_semantics_without_inventing_mechanism(CLAIM01: None = None) -> None:
    assessment = _assessment(
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
            FakeInventoryItem(evidence_id="e2", evidence_role="INDEPENDENT_REPORT"),
        ),
    )
    plan = build_claim_plan(assessment, observed_move="AAPL +9.5% on 2026-01-15")
    primary = next(claim for claim in plan.claims if claim.role is ClaimRole.PRIMARY)
    assert primary.statement == "AAPL rose on record guidance."
    assert primary.mechanism == "Guidance raised forward revenue."
    # No invented causal tokens; no confidence field exists.
    assert not hasattr(primary, "confidence")
    # No asserted_direction/time-scope fields are derived from prose.
    assert not hasattr(primary, "asserted_direction")
    assert not hasattr(primary, "time_scope")


def test_claim_without_mechanism_stays_none() -> None:
    decision = _decision(
        candidate_hypotheses=(
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "AAPL rose on record guidance.",
                "supporting_evidence_ids": ("e1",),
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            },
        ),
        evidence_decisions=(
            {
                "evidence_id": "e1",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
        ),
        recommended_status="SUFFICIENT",
    )
    assessment = _assessment(
        decision=decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),),
    )
    plan = build_claim_plan(assessment, observed_move="AAPL +9.5% on 2026-01-15")
    primary = next(claim for claim in plan.claims if claim.role is ClaimRole.PRIMARY)
    assert primary.mechanism is None


def test_rejected_hypothesis_never_promoted() -> None:
    decision = _decision(
        candidate_hypotheses=(
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "Rejected idea.",
                "supporting_evidence_ids": ("e1",),
                "magnitude_fit": "WEAK",
                "proposed_role": "REJECTED",
            },
        ),
        evidence_decisions=(
            {
                "evidence_id": "e1",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
        ),
        recommended_status="ABSTAIN",
    )
    assessment = _assessment(
        decision=decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),),
    )
    plan = build_claim_plan(assessment, observed_move="AAPL +9.5% on 2026-01-15")
    assert not any(claim.role in {ClaimRole.PRIMARY, ClaimRole.SECONDARY} for claim in plan.claims)


def test_evidence_refs_are_exactly_typed_bindings() -> None:
    assessment = _assessment(
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
            FakeInventoryItem(evidence_id="e2", evidence_role="INDEPENDENT_REPORT"),
        ),
    )
    plan = build_claim_plan(assessment, observed_move="AAPL +9.5% on 2026-01-15")
    by_role: dict[ClaimRole, list[str]] = {}
    for claim in plan.claims:
        by_role.setdefault(claim.role, []).append(claim.claim_id)
        assert set(claim.citation_evidence_ids) == set(claim.support_evidence_ids) | set(claim.counter_evidence_ids)
        allowed = {
            decision.evidence_id
            for decision in assessment.normalized_evidence_decisions
            if decision.disposition is EvidenceDisposition.SUPPORT
        }
        assert set(claim.support_evidence_ids) <= allowed
        assert set(claim.counter_evidence_ids) <= {
            decision.evidence_id
            for decision in assessment.normalized_evidence_decisions
            if decision.disposition is EvidenceDisposition.CONTRADICT
        }
    assert set(by_role) >= {ClaimRole.PRIMARY, ClaimRole.SECONDARY}


def test_plan_hash_and_artifact_identity_fields() -> None:
    assessment = _assessment(
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
            FakeInventoryItem(evidence_id="e2", evidence_role="INDEPENDENT_REPORT"),
        ),
    )
    plan = build_claim_plan(assessment, observed_move="AAPL +9.5% on 2026-01-15")
    assert plan.assessment_hash == assessment.assessment_hash
    assert plan.context_pack_sha256 == assessment.context_pack_sha256
    assert plan.evidence_state_hash == "e" * 64
    assert len(plan.plan_hash) == 64
    assert plan.ordering_policy_version


def test_abstain_plan_has_no_causal_claims_and_required_limitations() -> None:
    decision = _decision(
        research_decision="ABSTAIN",
        recommended_status="ABSTAIN",
        evidence_decisions=(
            {
                "evidence_id": "e1",
                "disposition": "WEAK",
                "reason_code": "weak",
            },
        ),
        candidate_hypotheses=(),
        proposed_missing_evidence=(
            {
                "proposal_ref": "p1",
                "evidence_need": "MARKET_STRUCTURE",
                "time_scope": "SESSION_INFORMATION_WINDOW",
                "expected_information": "flow data",
                "reason_code": "MARKET_STRUCTURE_UNSUPPORTED",
            },
        ),
        proposed_corrective_intents=(),
    )
    assessment = _assessment(
        decision=decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(FakeInventoryItem(evidence_id="e1", material_capability="LEAD_ONLY", evidence_role="COMMENTARY_LEAD"),),
    )
    plan = build_claim_plan(assessment, observed_move="AAPL +9.5% on 2026-01-15")
    assert not any(claim.role in {ClaimRole.PRIMARY, ClaimRole.SECONDARY} for claim in plan.claims)
    assert plan.required_limitations
    assert any("MARKET_STRUCTURE" in limitation for limitation in plan.required_limitations)


def test_weak_evidence_supports_only_context_role() -> None:
    decision = _decision(
        evidence_decisions=(
            {
                "evidence_id": "e1",
                "disposition": "WEAK",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "weak_support",
            },
        ),
        candidate_hypotheses=(
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "Weak lead only.",
                "supporting_evidence_ids": ("e1",),
                "magnitude_fit": "WEAK",
                "proposed_role": "PRIMARY",
            },
        ),
        recommended_status="PARTIAL",
    )
    assessment = _assessment(
        decision=decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(FakeInventoryItem(evidence_id="e1", evidence_role="REPORTED_NEWS"),),
    )
    plan = build_claim_plan(assessment, observed_move="AAPL +9.5% on 2026-01-15")
    assert not any(claim.role in {ClaimRole.PRIMARY, ClaimRole.SECONDARY} for claim in plan.claims)
    assert any(claim.role is ClaimRole.CONTEXT for claim in plan.claims)
