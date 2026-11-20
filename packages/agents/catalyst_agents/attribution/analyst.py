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
from typing import Any, Literal

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


# The two ``EvidenceDecision`` relation fields are a *mirror* derived in code
# from the canonical hypothesis-side ledger. They stay on the model (the
# normalized assessment, the claim builder and CLAIM-01 all read them) but they
# are removed from the model-facing JSON schema and prompt field contract so the
# model can never supply them (Frozen-A3 §8, C1b).
MODEL_FACING_DERIVED_RELATION_FIELDS: tuple[str, ...] = (
    "supports_hypothesis_refs",
    "contradicts_hypothesis_refs",
)


class EvidenceDecision(BaseModel):
    """One evidence item decision with typed hypothesis bindings (Phase 4 §9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    disposition: EvidenceDisposition
    supports_hypothesis_refs: tuple[str, ...] = ()
    contradicts_hypothesis_refs: tuple[str, ...] = ()
    reason_code: str = Field(default="", max_length=64)
    note: str | None = Field(default=None, max_length=2000)

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

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """Emit the model-facing schema without the code-derived mirror fields.

        ``supports_hypothesis_refs`` and ``contradicts_hypothesis_refs`` are
        derived in code (see ``normalize_decision_relation_mirror``); advertising
        them in the tool/JSON schema would invite the model to supply a second,
        possibly-conflicting source of truth. They remain declared model fields
        because the downstream claim path reads them.
        """
        json_schema = handler(core_schema)
        properties = json_schema.get("properties")
        if isinstance(properties, dict):
            for name in MODEL_FACING_DERIVED_RELATION_FIELDS:
                properties.pop(name, None)
        required = json_schema.get("required")
        if isinstance(required, list):
            json_schema["required"] = [
                name
                for name in required
                if name not in MODEL_FACING_DERIVED_RELATION_FIELDS
            ]
        return json_schema


class SourceQuote(BaseModel):
    """One verbatim source sentence (Q1c); code checks it before any use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    text: str = Field(max_length=1200)


class EventTimeSourceQuote(SourceQuote):
    """Quote contract enabled only by event_time_v1."""

    event_time_quote: str | None = Field(default=None, max_length=500)
    event_relation: Literal["BEFORE_OPEN", "INTRADAY", "PRIOR_DAY", "AFTER_CLOSE", "UNKNOWN"] | None = None
    event_date: str | None = Field(default=None, max_length=10)


class CandidateHypothesis(BaseModel):
    """Raw competing candidate keyed by a model-local ref (Phase 4 §8).

    ``quotes`` are what gets published (Q1c): one to four complete sentences
    copied verbatim from the supporting evidence bodies. ``statement`` and
    ``mechanism`` are the model's working notes and are never published.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_ref: str = Field(max_length=64)
    cause_type: CauseType
    statement: str = Field(max_length=2000)
    mechanism: str | None = Field(default=None, max_length=2000)
    quotes: tuple[SourceQuote, ...] = Field(default=(), max_length=6)
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
        overlap = set(self.supporting_evidence_ids) & set(
            self.contradicting_evidence_ids
        )
        if overlap:
            raise ValueError(
                "same evidence cannot support and contradict one hypothesis: "
                f"{sorted(overlap)}"
            )
        if len(self.unresolved_gap_refs) != len(set(self.unresolved_gap_refs)):
            raise ValueError("unresolved_gap_refs must be unique")
        return self


class EventTimeCandidateHypothesis(CandidateHypothesis):
    quotes: tuple[EventTimeSourceQuote, ...] = Field(default=(), max_length=6)


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

    @model_validator(mode="before")
    @classmethod
    def _drop_stray_lookback(cls, data: object) -> object:
        # A research proposal, not a claim: a stray lookback on a non-lookback
        # scope is ignored rather than failing the whole Analyst decision.
        if isinstance(data, dict) and data.get("lookback_sessions") is not None:
            scope = data.get("time_scope")
            scope = getattr(scope, "value", scope)
            if scope != TimeScope.LOOKBACK_SESSIONS.value:
                data = {**data, "lookback_sessions": None}
        return data

    @model_validator(mode="after")
    def _lookback(self) -> "ProposedMissingEvidence":
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


# Matches ``_CLAIM_REFERENCE_ID_MAX_LENGTH`` in ``claims.py``: a conflict
# record travels into ``Claim.conflict_refs`` unchanged, so the two bounds
# are one bound and must not drift apart.
CONFLICT_REF_MAX_LENGTH = 256


def normalize_decision_relation_mirror(payload: Any) -> Any:
    """Derive the ``EvidenceDecision`` relation mirror from the hypothesis ledger.

    Runs on the RAW provider JSON *before* schema validation (wired as the
    ``AnalystDecision`` before-validator below). The hypothesis-side ledger
    (``CandidateHypothesis.supporting_evidence_ids`` /
    ``contradicting_evidence_ids``) is canonical; the decision-side mirror
    (``supports_hypothesis_refs`` / ``contradicts_hypothesis_refs``) is
    overwritten with the refs derived from it.

    A supplied mirror that differs from the derived refs fails: a conflicting
    supplied decision-side mirror must FAIL, and the two sides are never
    unioned. Non-dict payloads (already-constructed models) pass through
    unchanged, because the structural cross-validators inside ``AnalystDecision``
    remain the authority for those.
    """
    if not isinstance(payload, dict):
        return payload
    hypotheses = payload.get("candidate_hypotheses")
    decisions = payload.get("evidence_decisions")
    if not isinstance(hypotheses, (list, tuple)) or not isinstance(
        decisions, (list, tuple)
    ):
        return payload

    derived_support: dict[str, list[str]] = {}
    derived_contradict: dict[str, list[str]] = {}
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict):
            continue
        ref = hypothesis.get("hypothesis_ref")
        if not isinstance(ref, str):
            continue
        for evidence_id in hypothesis.get("supporting_evidence_ids") or ():
            if isinstance(evidence_id, str):
                refs = derived_support.setdefault(evidence_id, [])
                if ref not in refs:
                    refs.append(ref)
        for evidence_id in hypothesis.get("contradicting_evidence_ids") or ():
            if isinstance(evidence_id, str):
                refs = derived_contradict.setdefault(evidence_id, [])
                if ref not in refs:
                    refs.append(ref)

    normalized: list[Any] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            normalized.append(decision)
            continue
        evidence_id = decision.get("evidence_id")
        supports = tuple(derived_support.get(evidence_id, ()))
        contradicts = tuple(derived_contradict.get(evidence_id, ()))
        for field_name, derived in (
            ("supports_hypothesis_refs", supports),
            ("contradicts_hypothesis_refs", contradicts),
        ):
            if field_name not in decision:
                continue
            supplied = decision[field_name]
            supplied_values = (
                tuple(supplied) if isinstance(supplied, (list, tuple)) else None
            )
            if supplied_values is None or set(supplied_values) != set(derived):
                raise ValueError(
                    f"supplied {field_name} for {evidence_id!r} is inconsistent "
                    "with the derived hypothesis-side ledger"
                )
        updated = dict(decision)
        updated["supports_hypothesis_refs"] = list(supports)
        updated["contradicts_hypothesis_refs"] = list(contradicts)
        normalized.append(updated)

    result = dict(payload)
    result["evidence_decisions"] = normalized
    return result


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

    @model_validator(mode="before")
    @classmethod
    def _drop_duplicate_evidence_decisions(cls, data: Any) -> Any:
        """Drop a repeated decision row with the same disposition (2026-10-01).

        A verbatim repeat is a model formatting slip; hypothesis bindings come
        from the hypothesis side, so dropping it loses nothing and keeps the
        run alive (c06). Repeats with *different* dispositions are a real
        conflict and still fail the schema (unique evidence ids).
        """
        if isinstance(data, dict) and isinstance(data.get("evidence_decisions"), list):
            first: dict[str, Any] = {}
            kept = []
            for decision in data["evidence_decisions"]:
                evidence_id = decision.get("evidence_id") if isinstance(decision, dict) else None
                if evidence_id is not None and evidence_id in first:
                    if first[evidence_id].get("disposition") == decision.get("disposition"):
                        continue
                elif evidence_id is not None:
                    first[evidence_id] = decision
                kept.append(decision)
            data = {**data, "evidence_decisions": kept}
        return data

    @model_validator(mode="before")
    @classmethod
    def _derive_relation_mirror(cls, data: Any) -> Any:
        """Derive the canonical decision-side mirror from the raw JSON.

        The mirror is computed from the hypothesis-side ledger before any field
        validation, so the model never has to supply it and a conflicting
        supplied mirror fails here (Frozen-A3 §8, C1b).
        """
        return normalize_decision_relation_mirror(data)

    @model_validator(mode="after")
    def _bounded_hypothesis_competition(self) -> "AnalystDecision":
        if len(self.candidate_hypotheses) > 3:
            raise ValueError("at most three candidate hypotheses are permitted")
        return self


    @model_validator(mode="after")
    def _unique_references(self) -> "AnalystDecision":
        # A conflict record is a *reference*: ``related_conflict_refs`` points at
        # it and ``Claim.conflict_refs`` carries it downstream under a 256-char
        # id bound. Accepting an unbounded string here and rejecting it three
        # layers later in the Claim model turns an Analyst contract violation
        # into a pydantic error inside claim construction, which is where it
        # surfaced. Bound it where it enters.
        for conflict in self.conflicts:
            if not conflict or len(conflict) > CONFLICT_REF_MAX_LENGTH:
                raise ValueError(
                    "conflicts must be non-empty references of at most "
                    f"{CONFLICT_REF_MAX_LENGTH} characters, not prose"
                )
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
        conflict_records = set(self.conflicts)

        # Decisions may only bind hypotheses that exist in this response.
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

        # Candidate evidence relations must resolve to evidence decisions and
        # must agree with the decision bindings in both directions.
        decision_by_evidence = {
            item.evidence_id: item for item in self.evidence_decisions
        }
        for hypothesis in self.candidate_hypotheses:
            for evidence_id in hypothesis.supporting_evidence_ids:
                decision = decision_by_evidence.get(evidence_id)
                if decision is None:
                    raise ValueError(
                        "candidate supporting evidence must have an "
                        f"EvidenceDecision: {evidence_id!r}"
                    )
                if hypothesis.hypothesis_ref not in decision.supports_hypothesis_refs:
                    raise ValueError(
                        "candidate support relation is inconsistent with the "
                        f"EvidenceDecision for {evidence_id!r}"
                    )
            for evidence_id in hypothesis.contradicting_evidence_ids:
                decision = decision_by_evidence.get(evidence_id)
                if decision is None:
                    raise ValueError(
                        "candidate contradicting evidence must have an "
                        f"EvidenceDecision: {evidence_id!r}"
                    )
                if (
                    hypothesis.hypothesis_ref
                    not in decision.contradicts_hypothesis_refs
                ):
                    raise ValueError(
                        "candidate contradiction relation is inconsistent with "
                        f"the EvidenceDecision for {evidence_id!r}"
                    )
            unknown = set(hypothesis.unresolved_gap_refs) - proposal_refs
            if unknown:
                raise ValueError(
                    "candidate references unknown missing-evidence proposal refs: "
                    f"{sorted(unknown)}"
                )
        hypothesis_by_ref = {
            item.hypothesis_ref: item for item in self.candidate_hypotheses
        }
        for decision in self.evidence_decisions:
            for ref in decision.supports_hypothesis_refs:
                if (
                    decision.evidence_id
                    not in hypothesis_by_ref[ref].supporting_evidence_ids
                ):
                    raise ValueError(
                        "EvidenceDecision support binding is inconsistent with "
                        f"candidate {ref!r}"
                    )
            for ref in decision.contradicts_hypothesis_refs:
                if (
                    decision.evidence_id
                    not in hypothesis_by_ref[ref].contradicting_evidence_ids
                ):
                    raise ValueError(
                        "EvidenceDecision contradiction binding is inconsistent "
                        f"with candidate {ref!r}"
                    )

        # At most one candidate may be proposed as PRIMARY (Phase 4 §11).
        primary_count = sum(
            1
            for hypothesis in self.candidate_hypotheses
            if hypothesis.proposed_role is HypothesisRole.PRIMARY
        )
        if primary_count > 1:
            raise ValueError(
                "multiple incompatible PRIMARY candidate proposals are not "
                "structurally valid"
            )

        # Proposals may only reference hypotheses and conflict records that
        # exist in this response.
        for proposal in self.proposed_missing_evidence:
            unknown_hypotheses = set(proposal.related_hypothesis_refs) - hypothesis_refs
            if unknown_hypotheses:
                raise ValueError(
                    "proposal references unknown hypothesis refs: "
                    f"{sorted(unknown_hypotheses)}"
                )
            unknown_conflicts = set(proposal.related_conflict_refs) - conflict_records
            if unknown_conflicts:
                raise ValueError(
                    "proposal references unknown conflict records: "
                    f"{sorted(unknown_conflicts)}"
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


class EventTimeAnalystDecision(AnalystDecision):
    """Strict output schema for the opt-in event-time Analyst prompt."""

    candidate_hypotheses: tuple[EventTimeCandidateHypothesis, ...] = ()


__all__ = [
    "AnalystDecision",
    "AttributionStatus",
    "AttributionType",
    "CandidateHypothesis",
    "EventTimeCandidateHypothesis",
    "CauseType",
    "EvidenceDecision",
    "EventTimeAnalystDecision",
    "EventTimeSourceQuote",
    "EvidenceDisposition",
    "HypothesisRole",
    "MagnitudeFit",
    "MODEL_FACING_DERIVED_RELATION_FIELDS",
    "normalize_decision_relation_mirror",
    "ProposedCorrectiveIntent",
    "ProposedMissingEvidence",
    "ResearchDecision",
]
