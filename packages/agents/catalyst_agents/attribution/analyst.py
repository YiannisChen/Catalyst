"""V1.1 AnalystDecision contract (M2-6, corrective).

The only structured object emitted directly by the Evidence Analyst model
(Phase 4 TSD §8–10; Frozen §6.3). Candidate hypotheses use model-local
hypothesis_ref; unresolved_gap_refs point to local missing-evidence proposal
refs. Runtime hypothesis_id/gap_id/action_id/batch_id, status_ceiling,
recoverability and executable batches are code-owned and are structurally
impossible here (CLAIM-01/AGENT-01 locks).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from catalyst_agents.retrieval.corrective import GapReasonCode
from catalyst_agents.retrieval.task import EvidenceNeed, TimeScope


class AttributionStatus(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    ABSTAIN = "ABSTAIN"


class AttributionType(str, Enum):
    EVIDENCE_BACKED_CAUSAL = "EVIDENCE_BACKED_CAUSAL"
    NO_MATERIAL_PUBLIC_CATALYST = "NO_MATERIAL_PUBLIC_CATALYST"


class CauseType(str, Enum):
    """Causal hypothesis taxonomy only (Frozen §6.3)."""

    COMPANY_SPECIFIC_CATALYST = "COMPANY_SPECIFIC_CATALYST"
    CONTINUATION = "CONTINUATION"
    SECTOR_MOVE = "SECTOR_MOVE"
    MACRO_EVENT = "MACRO_EVENT"
    FUNDAMENTAL_REPRICING = "FUNDAMENTAL_REPRICING"
    REPORTING_OR_ANALYST_CONTINUATION = "REPORTING_OR_ANALYST_CONTINUATION"


class ResearchDecision(str, Enum):
    READY = "READY"
    FOLLOW_UP = "FOLLOW_UP"
    ABSTAIN = "ABSTAIN"


class EvidenceDisposition(str, Enum):
    SUPPORT = "SUPPORT"
    CONTRADICT = "CONTRADICT"
    WEAK = "WEAK"
    LEAD_ONLY = "LEAD_ONLY"
    IRRELEVANT = "IRRELEVANT"


class MagnitudeFit(str, Enum):
    STRONG = "STRONG"
    PLAUSIBLE = "PLAUSIBLE"
    WEAK = "WEAK"
    UNKNOWN = "UNKNOWN"


class HypothesisRole(str, Enum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    CONTEXT = "CONTEXT"
    REJECTED = "REJECTED"


class EvidenceDecision(BaseModel):
    """One evidence item decision with typed hypothesis bindings (Phase 4 §9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    disposition: EvidenceDisposition
    supports_hypothesis_refs: tuple[str, ...] = ()
    contradicts_hypothesis_refs: tuple[str, ...] = ()
    reason_code: str = Field(default="", max_length=64)
    note: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _no_ambiguous_same_pair(self) -> "EvidenceDecision":
        overlap = set(self.supports_hypothesis_refs) & set(
            self.contradicts_hypothesis_refs
        )
        if overlap:
            raise ValueError(
                "same evidence/hypothesis pair cannot be both support and "
                f"contradiction: {sorted(overlap)}"
            )
        if len(self.supports_hypothesis_refs) != len(set(self.supports_hypothesis_refs)):
            raise ValueError("supports_hypothesis_refs must be unique")
        if len(self.contradicts_hypothesis_refs) != len(
            set(self.contradicts_hypothesis_refs)
        ):
            raise ValueError("contradicts_hypothesis_refs must be unique")
        return self


class CandidateHypothesis(BaseModel):
    """Raw competing candidate keyed by a model-local ref (Phase 4 §8)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_ref: str = Field(max_length=64)
    cause_type: CauseType
    statement: str = Field(max_length=2000)
    mechanism: str | None = Field(default=None, max_length=2000)
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    magnitude_fit: MagnitudeFit
    proposed_role: HypothesisRole
    unresolved_gap_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _unique_evidence_refs(self) -> "CandidateHypothesis":
        if len(self.supporting_evidence_ids) != len(set(self.supporting_evidence_ids)):
            raise ValueError("supporting_evidence_ids must be unique")
        if len(self.contradicting_evidence_ids) != len(
            set(self.contradicting_evidence_ids)
        ):
            raise ValueError("contradicting_evidence_ids must be unique")
        if len(self.unresolved_gap_refs) != len(set(self.unresolved_gap_refs)):
            raise ValueError("unresolved_gap_refs must be unique")
        return self


class ProposedMissingEvidence(BaseModel):
    """Model-proposed gap; proposal_ref is not a runtime gap ID (Phase 4 §16)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_ref: str = Field(max_length=64)
    evidence_need: EvidenceNeed
    time_scope: TimeScope
    lookback_sessions: int | None = None
    expected_information: str = Field(max_length=500)
    reason_code: GapReasonCode
    related_hypothesis_refs: tuple[str, ...] = ()
    related_conflict_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _lookback(self) -> "ProposedMissingEvidence":
        if (
            self.lookback_sessions is not None
            and self.time_scope is not TimeScope.LOOKBACK_SESSIONS
        ):
            raise ValueError("lookback_sessions requires LOOKBACK_SESSIONS time scope")
        if self.lookback_sessions is not None and self.lookback_sessions < 1:
            raise ValueError("lookback_sessions must be a positive session count")
        if len(self.related_hypothesis_refs) != len(set(self.related_hypothesis_refs)):
            raise ValueError("related_hypothesis_refs must be unique")
        if len(self.related_conflict_refs) != len(set(self.related_conflict_refs)):
            raise ValueError("related_conflict_refs must be unique")
        return self


class ProposedCorrectiveIntent(BaseModel):
    """Bounded corrective intent: proposal ref plus at most three hints."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_ref: str = Field(max_length=64)
    query_hints: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _bounded_hints(self) -> "ProposedCorrectiveIntent":
        if len(self.query_hints) > 3:
            raise ValueError("at most three query hints are permitted per intent")
        if any(len(hint) > 200 for hint in self.query_hints):
            raise ValueError("query hints must be length-bounded")
        return self


class AnalystDecision(BaseModel):
    """Raw Evidence Analyst output (Phase 4 §10).

    MUST NOT contain status_ceiling, an executable CorrectiveResearchBatch,
    runtime hypothesis/gap/action/batch IDs, recoverability, limits, or
    deadlines; those are code-owned and computed during normalization.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    evidence_decisions: tuple[EvidenceDecision, ...] = ()
    candidate_hypotheses: tuple[CandidateHypothesis, ...] = ()
    conflicts: tuple[str, ...] = ()
    proposed_missing_evidence: tuple[ProposedMissingEvidence, ...] = ()
    research_decision: ResearchDecision
    recommended_status: AttributionStatus
    proposed_attribution_type: AttributionType
    proposed_corrective_intents: tuple[ProposedCorrectiveIntent, ...] = ()

    @model_validator(mode="after")
    def _bounded_hypothesis_competition(self) -> "AnalystDecision":
        if len(self.candidate_hypotheses) > 3:
            raise ValueError("at most three candidate hypotheses are permitted")
        return self

    @model_validator(mode="after")
    def _unique_references(self) -> "AnalystDecision":
        evidence_ids = [item.evidence_id for item in self.evidence_decisions]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_decisions must reference unique evidence ids")
        hypothesis_refs = [item.hypothesis_ref for item in self.candidate_hypotheses]
        if len(hypothesis_refs) != len(set(hypothesis_refs)):
            raise ValueError("candidate_hypotheses must have unique hypothesis refs")
        proposal_refs = [item.proposal_ref for item in self.proposed_missing_evidence]
        if len(proposal_refs) != len(set(proposal_refs)):
            raise ValueError("proposed_missing_evidence must have unique proposal refs")
        return self

    @model_validator(mode="after")
    def _local_references_resolve(self) -> "AnalystDecision":
        hypothesis_refs = {item.hypothesis_ref for item in self.candidate_hypotheses}
        proposal_refs = {item.proposal_ref for item in self.proposed_missing_evidence}
        for decision in self.evidence_decisions:
            unknown = (
                set(decision.supports_hypothesis_refs)
                | set(decision.contradicts_hypothesis_refs)
            ) - hypothesis_refs
            if unknown:
                raise ValueError(
                    "evidence decision references unknown hypothesis refs: "
                    f"{sorted(unknown)}"
                )
        for hypothesis in self.candidate_hypotheses:
            unknown = set(hypothesis.unresolved_gap_refs) - proposal_refs
            if unknown:
                raise ValueError(
                    "candidate references unknown missing-evidence proposal refs: "
                    f"{sorted(unknown)}"
                )
        for intent in self.proposed_corrective_intents:
            if intent.proposal_ref not in proposal_refs:
                raise ValueError(
                    "proposed_corrective_intents must reference a proposed "
                    "missing-evidence record"
                )
        intent_refs = [intent.proposal_ref for intent in self.proposed_corrective_intents]
        if len(intent_refs) != len(set(intent_refs)):
            raise ValueError("proposed_corrective_intents must have unique proposal refs")
        return self


__all__ = [
    "AnalystDecision",
    "AttributionStatus",
    "AttributionType",
    "CandidateHypothesis",
    "CauseType",
    "EvidenceDecision",
    "EvidenceDisposition",
    "HypothesisRole",
    "MagnitudeFit",
    "ProposedCorrectiveIntent",
    "ProposedMissingEvidence",
    "ResearchDecision",
]
