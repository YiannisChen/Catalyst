"""M5-2: deterministic AnalystDecision normalization and status ceiling.

Final TSD §8.1; Phase 4 TSD §§13–15; M5 plan M5-2. normalize_decision assigns
deterministic IDs, normalizes dispositions/links without creating support,
validates gaps/capability, computes the code-owned status ceiling, applies the
locked status-rank formula (ABSTAIN=0, PARTIAL=1, SUFFICIENT=2;
final = lower(model recommendation, code ceiling)), and normalizes
FOLLOW_UP/no-batch and round-2 closures. AGENT-01: ABSTAIN + EVIDENCE_BACKED_CAUSAL
carries no accepted cause and never adds causal language.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    AttributionStatus,
    AttributionType,
    CandidateHypothesis,
    CauseType,
    EvidenceDecision,
    EvidenceDisposition,
    HypothesisRole,
    MagnitudeFit,
    ResearchDecision,
)
from catalyst_agents.attribution.assessment import (
    EvidenceAssessment,
    normalize_decision,
)
from catalyst_agents.retrieval.corrective import (
    GapReasonCode,
    MissingEvidence,
)
from catalyst_agents.retrieval.task import EvidenceNeed, ResearchTask, TimeScope


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Lightweight context-pack surface (mirrors the M4 EvidenceAnalystContextPack
# fields the normalizer reads; production passes the real pack).
# ---------------------------------------------------------------------------

class FakeContextPack:
    def __init__(
        self,
        *,
        run_id: str = "run:1",
        round: int = 1,
        included_evidence_ids: tuple[str, ...] = ("e1",),
        evidence_inventory: tuple[Any, ...] = (),
        research_history: tuple[ResearchTask, ...] = (),
        coverage_summary: Any = None,
        data_coverage_gaps: tuple[Any, ...] = (),
        capability_gaps: tuple[Any, ...] = (),
        retrieval_degradations: tuple[Any, ...] = (),
        context_pack_sha256: str = "a" * 64,
        rendered_messages_sha256: str = "b" * 64,
    ):
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


class FakeInventoryItem:
    """Minimal inventory item surface: materiality/source-role/independence."""

    def __init__(
        self,
        *,
        evidence_id: str,
        material_capability: str = "MATERIAL_CAPABLE",
        evidence_role: str = "DIRECT_PRIMARY",
        independence_group_id: str | None = None,
        independence_status: str = "KNOWN_GROUP",
    ):
        self.evidence_id = evidence_id
        self.material_capability = material_capability
        self.evidence_role = evidence_role
        self.independence_group_id = independence_group_id
        self.independence_status = independence_status


class FakeCapabilityRegistry:
    def __init__(self, healthy_need: EvidenceNeed | None = None):
        self.healthy_need = healthy_need

    def capability_for(self, need: EvidenceNeed) -> dict | None:
        if self.healthy_need is not None and need is self.healthy_need:
            return {"backend": "fixture", "health": "HEALTHY"}
        return None

    def is_recoverable(self, need: EvidenceNeed) -> bool:
        return self.capability_for(need) is not None


def _task(need: EvidenceNeed) -> ResearchTask:
    return ResearchTask(
        schema_version="1.0",
        task_id=f"task:{need.value}",
        round=1,
        priority=1,
        scenario="COMPANY_SPECIFIC",
        evidence_need=need,
        time_scope="SESSION_INFORMATION_WINDOW",
        retrieval_policy_id="rp:v1",
        task_fingerprint="f" * 64,
    )


def _coverage(**overrides: Any) -> Any:
    base = {
        "eligible_item_count": 2,
        "eligible_asset_count": 2,
        "eligible_full_text_item_count": 2,
        "material_capable_item_count": 2,
        "material_capable_asset_count": 2,
        "primary_authority_asset_count": 0,
        "direct_primary_asset_count": 1,
        "reported_news_asset_count": 0,
        "commentary_lead_asset_count": 0,
        "unknown_role_asset_count": 0,
        "eligible_reported_news_group_count": 0,
        "unknown_independence_asset_count": 0,
        "known_duplicate_or_syndicated_asset_count": 0,
        "parse_degraded_item_count": 0,
        "content_state_counts": (),
    }
    base.update(overrides)
    from catalyst_agents.attribution.coverage import CoverageSummary

    return CoverageSummary(**base)


def _decision(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "evidence_decisions": (
            {
                "evidence_id": "e1",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
        ),
        "candidate_hypotheses": (
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "AAPL rose on record guidance.",
                "supporting_evidence_ids": ("e1",),
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            },
        ),
        "research_decision": "READY",
        "recommended_status": "SUFFICIENT",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
    }
    base.update(overrides)
    return AnalystDecision.model_validate(base)


def _normalize(decision: AnalystDecision, **pack_kwargs: Any) -> EvidenceAssessment:
    pack = FakeContextPack(**pack_kwargs)
    registry = FakeCapabilityRegistry(healthy_need=EvidenceNeed.COMPANY_PRIMARY)
    return normalize_decision(
        decision,
        context_pack=pack,
        capability_registry=registry,
        policy_version="norm:v1",
    )


def test_normalize_decision_assigns_deterministic_ids_and_hash() -> None:
    decision = _decision()
    first = _normalize(
        decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        ),
        research_history=(_task(EvidenceNeed.COMPANY_PRIMARY), _task(EvidenceNeed.COMPANY_NEWS)),
        coverage_summary=_coverage(),
    )
    second = _normalize(
        decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        ),
        research_history=(_task(EvidenceNeed.COMPANY_PRIMARY), _task(EvidenceNeed.COMPANY_NEWS)),
        coverage_summary=_coverage(),
    )
    assert first.normalized_hypotheses[0].hypothesis_id.startswith("hyp:")
    assert first.normalized_hypotheses[0].hypothesis_id == second.normalized_hypotheses[0].hypothesis_id
    assert first.assessment_hash == second.assessment_hash
    assert len(first.assessment_hash) == 64


def test_status_rank_formula_is_lower_of_model_recommendation_and_ceiling() -> None:
    # Ceiling PARTIAL: only unknown-lineage reported-news support.
    partial_ceiling = _normalize(
        _decision(recommended_status="SUFFICIENT"),
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(
                evidence_id="e1",
                evidence_role="INDEPENDENT_REPORT",
                independence_status="UNKNOWN",
            ),
        ),
    )
    assert partial_ceiling.status_ceiling is AttributionStatus.PARTIAL
    assert partial_ceiling.final_status is AttributionStatus.PARTIAL

    # Model ABSTAIN below a SUFFICIENT ceiling stays ABSTAIN.
    abstain_model = _normalize(
        _decision(recommended_status="ABSTAIN"),
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        ),
    )
    assert abstain_model.status_ceiling is AttributionStatus.SUFFICIENT
    assert abstain_model.final_status is AttributionStatus.ABSTAIN

    # SUFFICIENT ceiling + SUFFICIENT recommendation stays SUFFICIENT.
    sufficient = _normalize(
        _decision(recommended_status="SUFFICIENT"),
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        ),
    )
    assert sufficient.status_ceiling is AttributionStatus.SUFFICIENT
    assert sufficient.final_status is AttributionStatus.SUFFICIENT


def test_no_supported_hypothesis_ceiling_is_abstain() -> None:
    assessment = _normalize(
        _decision(
            evidence_decisions=(),
            candidate_hypotheses=(),
            recommended_status="SUFFICIENT",
        ),
        included_evidence_ids=(),
        evidence_inventory=(),
    )
    assert assessment.status_ceiling is AttributionStatus.ABSTAIN
    assert assessment.final_status is AttributionStatus.ABSTAIN


def test_abstain_fixed_path_never_adds_causal_language() -> None:
    assessment = _normalize(
        _decision(
            research_decision="ABSTAIN",
            recommended_status="ABSTAIN",
            proposed_attribution_type="EVIDENCE_BACKED_CAUSAL",
            evidence_decisions=(),
            candidate_hypotheses=(),
        ),
        included_evidence_ids=(),
        evidence_inventory=(),
    )
    assert assessment.final_status is AttributionStatus.ABSTAIN
    assert assessment.final_attribution_type is AttributionType.EVIDENCE_BACKED_CAUSAL
    causal_roles = {HypothesisRole.PRIMARY, HypothesisRole.SECONDARY}
    assert not any(
        hypothesis.normalized_role in causal_roles
        for hypothesis in assessment.normalized_hypotheses
    )


def test_ambiguous_support_contradict_pair_is_dropped_and_recorded() -> None:
    # The strict schema rejects same-pair ambiguity at the node boundary; the
    # normalizer defensively drops the ambiguous relation and records the
    # violation without raising status (Final TSD §8.1 step 4).
    decision = AnalystDecision.model_construct(
        schema_version="1.0",
        evidence_decisions=(
            EvidenceDecision.model_construct(
                evidence_id="e1",
                disposition=EvidenceDisposition.SUPPORT,
                supports_hypothesis_refs=("h1",),
                contradicts_hypothesis_refs=("h1",),
                reason_code="r1",
            ),
        ),
        candidate_hypotheses=(
            CandidateHypothesis.model_construct(
                hypothesis_ref="h1",
                cause_type=CauseType.COMPANY_SPECIFIC_CATALYST,
                statement="AAPL rose on record guidance.",
                supporting_evidence_ids=("e1",),
                contradicting_evidence_ids=("e1",),
                magnitude_fit=MagnitudeFit.STRONG,
                proposed_role=HypothesisRole.PRIMARY,
            ),
        ),
        conflicts=(),
        proposed_missing_evidence=(),
        research_decision=ResearchDecision.READY,
        recommended_status=AttributionStatus.SUFFICIENT,
        proposed_attribution_type=AttributionType.EVIDENCE_BACKED_CAUSAL,
        proposed_corrective_intents=(),
    )
    assessment = _normalize(
        decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        ),
    )
    assert any("ambiguous" in violation for violation in assessment.normalization_violations)
    hypothesis = assessment.normalized_hypotheses[0]
    assert "e1" not in hypothesis.supporting_evidence_ids
    assert "e1" not in hypothesis.contradicting_evidence_ids


def test_unknown_evidence_ref_is_structural_failure() -> None:
    decision = _decision(
        evidence_decisions=(
            {
                "evidence_id": "e1",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
            {
                "evidence_id": "ghost",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
        ),
        candidate_hypotheses=(
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "AAPL rose on record guidance.",
                "supporting_evidence_ids": ("e1", "ghost"),
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            },
        ),
    )
    with pytest.raises(ValueError, match="inventory"):
        _normalize(decision, included_evidence_ids=("e1",), evidence_inventory=())


def test_follow_up_with_recoverable_gap_emits_one_action_batch() -> None:
    decision = _decision(
        research_decision="FOLLOW_UP",
        proposed_missing_evidence=(
            {
                "proposal_ref": "p1",
                "evidence_need": "COMPANY_PRIMARY",
                "time_scope": "PRIOR_SESSION",
                "expected_information": "issuer filing confirmation",
                "reason_code": "MISSING_PRIMARY_CONFIRMATION",
            },
        ),
        proposed_corrective_intents=(
            {"proposal_ref": "p1", "query_hints": ("8-K",)},
        ),
    )
    assessment = _normalize(
        decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        ),
    )
    assert assessment.research_decision is ResearchDecision.FOLLOW_UP
    assert assessment.corrective_batch is not None
    assert len(assessment.corrective_batch.actions) == 1
    gap = assessment.validated_missing_evidence[0]
    assert gap.gap_id.startswith("gap:")
    assert gap.recoverable is True
    assert assessment.corrective_batch.actions[0].gap_id == gap.gap_id
    assert assessment.corrective_batch.actions[0].research_fingerprint


def test_follow_up_without_valid_batch_normalizes_to_ready_partial_or_abstain() -> None:
    decision = _decision(
        research_decision="FOLLOW_UP",
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
    with_support = _normalize(
        decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        ),
    )
    # A publishable supported claim remains -> READY capped at PARTIAL.
    assert with_support.research_decision is ResearchDecision.READY
    assert with_support.corrective_batch is None
    assert with_support.final_status is AttributionStatus.PARTIAL
    assert "READY_WITH_PARTIAL" in with_support.normalization_diagnostics

    no_support = _normalize(
        _decision(
            research_decision="FOLLOW_UP",
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
            evidence_decisions=(),
            candidate_hypotheses=(),
        ),
        included_evidence_ids=(),
        evidence_inventory=(),
    )
    assert no_support.research_decision is ResearchDecision.READY
    assert no_support.final_status is AttributionStatus.ABSTAIN


def test_round_two_follow_up_never_creates_third_call() -> None:
    decision = _decision(
        research_decision="FOLLOW_UP",
        proposed_missing_evidence=(
            {
                "proposal_ref": "p1",
                "evidence_need": "COMPANY_PRIMARY",
                "time_scope": "PRIOR_SESSION",
                "expected_information": "issuer filing confirmation",
                "reason_code": "MISSING_PRIMARY_CONFIRMATION",
            },
        ),
        proposed_corrective_intents=(
            {"proposal_ref": "p1", "query_hints": ("8-K",)},
        ),
    )
    assessment = _normalize(
        decision,
        round=2,
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        ),
    )
    assert assessment.corrective_batch is None
    assert assessment.research_decision is ResearchDecision.READY
    assert "round_exhausted" in " ".join(assessment.normalization_diagnostics)


def test_hint_sanitization_strips_unsafe_content() -> None:
    decision = _decision(
        research_decision="FOLLOW_UP",
        proposed_missing_evidence=(
            {
                "proposal_ref": "p1",
                "evidence_need": "COMPANY_PRIMARY",
                "time_scope": "PRIOR_SESSION",
                "expected_information": "issuer filing confirmation",
                "reason_code": "MISSING_PRIMARY_CONFIRMATION",
            },
        ),
        proposed_corrective_intents=(
            {
                "proposal_ref": "p1",
                "query_hints": (
                    "  SELECT * FROM secrets WHERE ticker='AAPL' https://evil.example "
                    "source:sec  date:2026-01-06 8-K ",
                ),
            },
        ),
    )
    assessment = _normalize(
        decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        ),
    )
    action = assessment.corrective_batch.actions[0]
    hint = action.query_hints[0]
    assert "SELECT" not in hint
    assert "http" not in hint
    assert "source:" not in hint
    assert "date:" not in hint
    assert "ticker=" not in hint
    assert "8-K" in hint


def test_no_material_requires_gates_and_is_partial() -> None:
    decision = _decision(
        recommended_status="PARTIAL",
        proposed_attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
        evidence_decisions=(
            {
                "evidence_id": "e1",
                "disposition": "LEAD_ONLY",
                "reason_code": "lead",
            },
        ),
        candidate_hypotheses=(),
    )
    gated = _normalize(
        decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", material_capability="LEAD_ONLY", evidence_role="COMMENTARY_LEAD"),
        ),
        research_history=(_task(EvidenceNeed.COMPANY_PRIMARY), _task(EvidenceNeed.COMPANY_NEWS)),
        coverage_summary=_coverage(),
    )
    assert gated.final_attribution_type is AttributionType.NO_MATERIAL_PUBLIC_CATALYST
    assert gated.final_status is AttributionStatus.PARTIAL

    degraded = _normalize(
        decision,
        included_evidence_ids=("e1",),
        evidence_inventory=(
            FakeInventoryItem(evidence_id="e1", material_capability="LEAD_ONLY", evidence_role="COMMENTARY_LEAD"),
        ),
        research_history=(_task(EvidenceNeed.COMPANY_PRIMARY), _task(EvidenceNeed.COMPANY_NEWS)),
        coverage_summary=None,
        capability_gaps=({"gap_id": "g1", "evidence_need": "FUNDAMENTALS", "reason_code": "BACKEND_NOT_IMPLEMENTED"},),
    )
    assert degraded.final_attribution_type is AttributionType.EVIDENCE_BACKED_CAUSAL
    assert degraded.final_status is AttributionStatus.ABSTAIN
