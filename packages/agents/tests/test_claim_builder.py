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

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    AttributionStatus,
    EvidenceDisposition,
)
from catalyst_agents.attribution.assessment import (
    EvidenceAssessment,
    normalize_decision,
)
from catalyst_agents.attribution.claim_validation import validate_claim_plan
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



def _runtime_identity():
    import hashlib

    from catalyst_data.canonical.identity import DataRuntimeIdentity

    def h(part):
        return hashlib.sha256(f"1:{part}".encode("utf-8")).hexdigest()

    return DataRuntimeIdentity(
        data_snapshot_id=h("snapshot"), corpus_manifest_id=h("corpus"),
        fts_index_version="build:fts", dense_index_version=h("dense"),
        embedding_model_revision="emb:1", reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


class FakeContextPack:
    def __init__(self, *, run_id: str = "run:1", round: int = 1,
                 included_evidence_ids: tuple[str, ...] = ("e1", "e2"),
                 evidence_inventory: tuple[Any, ...] = (), research_history: tuple[Any, ...] = (),
                 coverage_summary: Any = None, data_coverage_gaps: tuple[Any, ...] = (),
                 capability_gaps: tuple[Any, ...] = (), retrieval_degradations: tuple[Any, ...] = (),
                 context_pack_sha256: str = "a" * 64, rendered_messages_sha256: str = "b" * 64,
                 data_runtime_identity: Any = None):
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
        self.data_runtime_identity = (
            data_runtime_identity if data_runtime_identity is not None else _runtime_identity()
        )


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


def test_one_weak_item_does_not_override_support_beside_it() -> None:
    """A hypothesis with SUPPORT keeps its causal role despite a WEAK item.

    Before this rule, ``weak_present`` was any WEAK among the supporting items
    and demoted the whole hypothesis to CONTEXT, so three primary citations
    scored the same as none. Measured on the Task 2.7b probe: c11 had three
    SUPPORT items and one WEAK cover-page chunk and still abstained. See
    docs/plans/2026-09-22-m8-attribution-abstain-root-cause.md §3.
    """
    decision = _decision(
        evidence_decisions=(
            {
                "evidence_id": "e1",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "primary_disclosure",
            },
            {
                "evidence_id": "e2",
                "disposition": "WEAK",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "cover_page_boilerplate",
            },
        ),
        candidate_hypotheses=(
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "Results beat guidance.",
                "supporting_evidence_ids": ("e1", "e2"),
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            },
        ),
        recommended_status="PARTIAL",
    )
    assessment = _assessment(
        decision=decision,
        included_evidence_ids=("e1", "e2"),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="PRIMARY_AUTHORITY"),
            FakeInventoryItem(evidence_id="e2", evidence_role="PRIMARY_AUTHORITY"),
        ),
    )
    plan = build_claim_plan(assessment, observed_move="UNH -3.1% on 2026-01-27")
    causal = [c for c in plan.claims if c.role in {ClaimRole.PRIMARY, ClaimRole.SECONDARY}]
    assert causal, "a hypothesis with SUPPORT evidence must keep a causal role"
    # CLAIM-01: the support refs stay exactly the hypothesis's typed bindings,
    # WEAK item included. Only the role rule changed, never the bindings --
    # claim_validation.py:203 compares them against the hypothesis and an
    # omitted ref is a CLAIM_INTEGRITY_FAILURE.
    assert causal[0].support_evidence_ids == ("e1", "e2")


def _issuer_pack_plan(dispositions, roles, recommended="SUFFICIENT"):
    """Build + validate a plan for one hypothesis over the given dispositions."""
    import test_claim_validator as V

    eids = [f"e{i + 1}" for i in range(len(dispositions))]
    decision = _decision(
        evidence_decisions=tuple(
            {
                "evidence_id": e,
                "disposition": d,
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "r",
            }
            for e, d in zip(eids, dispositions)
        ),
        candidate_hypotheses=(
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "The issuer reported record results.",
                "mechanism": "Results raised forward revenue.",
                "supporting_evidence_ids": tuple(eids),
                "magnitude_fit": "PLAUSIBLE",
                "proposed_role": "PRIMARY",
            },
        ),
        recommended_status=recommended,
    )
    inventory = tuple(
        FakeInventoryItem(
            evidence_id=e, evidence_role=r,
            independence_status="UNKNOWN", independence_group_id=None,
        )
        for e, r in zip(eids, roles)
    )
    assessment = _assessment(
        decision=decision, included_evidence_ids=tuple(eids),
        evidence_inventory=inventory,
    )
    plan = build_claim_plan(assessment, observed_move="XYZ +9.0% on 2026-01-15")
    validated = validate_claim_plan(
        plan, assessment,
        runtime_identity=V._runtime(), temporal_identity=V._temporal(),
        evidence_inventory=inventory,
    )
    return assessment, validated


def test_weak_support_caps_the_status_at_partial() -> None:
    """Uncorroborated issuer text that is mostly WEAK may not be SUFFICIENT.

    The veto at three strengths, on the same pack (one SUPPORT earnings chunk,
    three WEAK boilerplate chunks, zero INDEPENDENT_REPORT):

    * before 2026-09-22 any WEAK supporting item demoted the whole hypothesis to
      CONTEXT, so this pack -- and c11's three-good-citations pack -- delivered
      ABSTAIN;
    * removing that veto alone let it deliver **SUFFICIENT** while citing the
      boilerplate, which ADR-005 forbids ("support limited to ... uncorroborated
      issuer claims" is PARTIAL);
    * it now delivers a causal claim capped at PARTIAL.

    A WEAK item no longer blocks a causal claim; it caps how strongly the claim
    may be stated.
    """
    PA = "PRIMARY_AUTHORITY"
    _, validated = _issuer_pack_plan(["SUPPORT", "WEAK", "WEAK", "WEAK"], [PA] * 4)
    causal = [c for c in validated.claims if c.role in {ClaimRole.PRIMARY, ClaimRole.SECONDARY}]
    assert causal, "a hypothesis with SUPPORT evidence still earns a causal role"
    assert validated.status is AttributionStatus.PARTIAL, (
        "mostly-WEAK uncorroborated issuer support must not reach SUFFICIENT"
    )

    # An all-WEAK hypothesis carries no causal claim at all.
    _, weak_only = _issuer_pack_plan(["WEAK"] * 4, [PA] * 4)
    assert not [
        c for c in weak_only.claims if c.role in {ClaimRole.PRIMARY, ClaimRole.SECONDARY}
    ]

    # Clean support is untouched by the cap.
    _, clean = _issuer_pack_plan(["SUPPORT"] * 4, [PA] * 4)
    assert clean.status is AttributionStatus.SUFFICIENT
