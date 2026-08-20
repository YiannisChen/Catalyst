"""V1.1 AnalystDecision contract (M2-6).

The only structured object emitted directly by the Evidence Analyst model
(Frozen §6.3). CLAIM-01/AGENT-01 locks: the raw model decision must not emit
status_ceiling, an executable CorrectiveResearchBatch, or runtime gap IDs;
proposal_ref links bounded corrective intent to a proposed missing-evidence
record and is not a runtime gap ID.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator

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
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    decision: EvidenceDisposition


class CandidateHypothesis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: str
    cause_type: CauseType
    statement: str
    mechanism: str | None = None
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    magnitude_fit: MagnitudeFit
    proposed_role: HypothesisRole
    unresolved_gap_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _unique_evidence_refs(self) -> "CandidateHypothesis":
        if len(self.supporting_evidence_ids) != len(set(self.supporting_evidence_ids)):
            raise ValueError("supporting_evidence_ids must be unique")
        if len(self.contradicting_evidence_ids) != len(set(self.contradicting_evidence_ids)):
            raise ValueError("contradicting_evidence_ids must be unique")
        return self


class ProposedMissingEvidence(BaseModel):
    """Model-proposed gap. proposal_ref is not a runtime gap ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_ref: str
    evidence_need: EvidenceNeed
    time_scope: TimeScope
    expected_information: str
    reason_code: GapReasonCode


class ProposedCorrectiveIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_ref: str
    query_hints: tuple[str, ...] = ()


class AnalystDecision(BaseModel):
    """Raw Evidence Analyst output (Frozen §6.3).

    MUST NOT contain status_ceiling, an executable CorrectiveResearchBatch, or
    runtime gap IDs; those are code-owned and computed during normalization.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        hypothesis_ids = [item.hypothesis_id for item in self.candidate_hypotheses]
        if len(hypothesis_ids) != len(set(hypothesis_ids)):
            raise ValueError("candidate_hypotheses must have unique hypothesis ids")
        proposal_refs = [item.proposal_ref for item in self.proposed_missing_evidence]
        if len(proposal_refs) != len(set(proposal_refs)):
            raise ValueError("proposed_missing_evidence must have unique proposal refs")
        return self

    @model_validator(mode="after")
    def _corrective_intents_reference_proposals(self) -> "AnalystDecision":
        proposal_refs = {item.proposal_ref for item in self.proposed_missing_evidence}
        for intent in self.proposed_corrective_intents:
            if intent.proposal_ref not in proposal_refs:
                raise ValueError(
                    "proposed_corrective_intents must reference a proposed "
                    "missing-evidence record"
                )
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
