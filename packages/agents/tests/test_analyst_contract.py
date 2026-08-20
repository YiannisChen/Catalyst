"""V1.1 AnalystDecision contract tests (M2-6, corrective).

Phase 4 TSD §8–10 raw model decision surface. CandidateHypothesis uses
model-local hypothesis_ref (never hypothesis_id); unresolved_gap_refs point to
local missing-evidence proposal refs; EvidenceDecision carries disposition +
support/contradict hypothesis refs + bounded reason code/note; runtime IDs and
status_ceiling remain code-owned and are structurally impossible.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

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
    ProposedCorrectiveIntent,
    ProposedMissingEvidence,
    ResearchDecision,
)


def _hypothesis(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "hypothesis_ref": "h1",
        "cause_type": "COMPANY_SPECIFIC_CATALYST",
        "statement": "AAPL rose on record guidance.",
        "mechanism": None,
        "supporting_evidence_ids": ("corpus:chunk:0001",),
        "contradicting_evidence_ids": (),
        "magnitude_fit": "STRONG",
        "proposed_role": "PRIMARY",
        "unresolved_gap_refs": (),
    }
    base.update(overrides)
    return base


def _decision(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "evidence_decisions": (
            EvidenceDecision(
                evidence_id="corpus:chunk:0001",
                disposition="SUPPORT",
                supports_hypothesis_refs=("h1",),
                contradicts_hypothesis_refs=(),
                reason_code="material_support",
                note=None,
            ),
        ),
        "candidate_hypotheses": (CandidateHypothesis(**_hypothesis()),),
        "conflicts": (),
        "proposed_missing_evidence": (),
        "research_decision": "READY",
        "recommended_status": "SUFFICIENT",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "proposed_corrective_intents": (),
    }
    base.update(overrides)
    return base


def test_analyst_decision_is_strict_and_frozen() -> None:
    decision = AnalystDecision(**_decision())
    assert decision.schema_version == "1.0"
    with pytest.raises(ValidationError):
        decision.research_decision = "FOLLOW_UP"  # frozen
    with pytest.raises(ValidationError):
        AnalystDecision(**_decision(), unknown_field=True)  # extra forbidden


def test_analyst_decision_requires_schema_version() -> None:
    data = _decision()
    del data["schema_version"]
    with pytest.raises(ValidationError):
        AnalystDecision(**data)


def test_evidence_decisions_dispositions_are_closed() -> None:
    for value in ("SUPPORT", "CONTRADICT", "WEAK", "LEAD_ONLY", "IRRELEVANT"):
        assert (
            EvidenceDecision(
                evidence_id="e1", disposition=value, reason_code="r1"
            ).disposition
            == value
        )
    with pytest.raises(ValidationError):
        EvidenceDecision(evidence_id="e1", disposition="RELEVANT", reason_code="r1")


def test_evidence_decision_reason_code_and_note_are_bounded() -> None:
    with pytest.raises(ValidationError):
        EvidenceDecision(
            evidence_id="e1",
            disposition="SUPPORT",
            reason_code="x" * 200,
        )
    with pytest.raises(ValidationError):
        EvidenceDecision(
            evidence_id="e1",
            disposition="SUPPORT",
            reason_code="r1",
            note="y" * 500,
        )


def test_at_most_three_candidate_hypotheses() -> None:
    hypotheses = tuple(
        CandidateHypothesis(**_hypothesis(hypothesis_ref=f"h{i}")) for i in range(4)
    )
    with pytest.raises(ValidationError):
        AnalystDecision(**_decision(candidate_hypotheses=hypotheses))


def test_cause_type_is_exactly_the_six_causal_values() -> None:
    assert [member.value for member in CauseType] == [
        "COMPANY_SPECIFIC_CATALYST",
        "CONTINUATION",
        "SECTOR_MOVE",
        "MACRO_EVENT",
        "FUNDAMENTAL_REPRICING",
        "REPORTING_OR_ANALYST_CONTINUATION",
    ]


def test_no_material_and_market_structure_unsupported_are_not_causes() -> None:
    with pytest.raises(ValidationError):
        CandidateHypothesis(
            **_hypothesis(cause_type="NO_MATERIAL_PUBLIC_CATALYST")
        )
    with pytest.raises(ValidationError):
        CandidateHypothesis(
            **_hypothesis(cause_type="MARKET_STRUCTURE_UNSUPPORTED")
        )
    assert "NO_MATERIAL_PUBLIC_CATALYST" not in CauseType.__members__
    assert "MARKET_STRUCTURE_UNSUPPORTED" not in CauseType.__members__


def test_schema_has_no_status_ceiling_or_executable_batch() -> None:
    fields = set(AnalystDecision.model_fields)
    assert "status_ceiling" not in fields
    assert "corrective_batch" not in fields
    assert "hypothesis_id" not in fields
    with pytest.raises(ValidationError):
        AnalystDecision(**_decision(status_ceiling="PARTIAL"))
    with pytest.raises(ValidationError):
        AnalystDecision(**_decision(corrective_batch={"batch_id": "b1"}))


def test_candidate_hypothesis_uses_model_local_hypothesis_ref() -> None:
    assert "hypothesis_ref" in CandidateHypothesis.model_fields
    assert "hypothesis_id" not in CandidateHypothesis.model_fields
    with pytest.raises(ValidationError):
        CandidateHypothesis(**_hypothesis(hypothesis_id="hyp:1"))
    hypothesis = CandidateHypothesis(**_hypothesis())
    assert hypothesis.hypothesis_ref == "h1"


def test_candidate_hypothesis_uses_unresolved_gap_refs_not_gap_ids() -> None:
    assert "unresolved_gap_refs" in CandidateHypothesis.model_fields
    assert "unresolved_gap_ids" not in CandidateHypothesis.model_fields
    with pytest.raises(ValidationError):
        CandidateHypothesis(**_hypothesis(unresolved_gap_ids=("gap:1",)))


def test_proposal_ref_is_not_a_runtime_gap_id() -> None:
    fields = set(ProposedMissingEvidence.model_fields)
    assert fields == {
        "proposal_ref",
        "evidence_need",
        "time_scope",
        "lookback_sessions",
        "expected_information",
        "reason_code",
        "related_hypothesis_refs",
        "related_conflict_refs",
    }
    with pytest.raises(ValidationError):
        ProposedMissingEvidence(
            proposal_ref="p-1",
            evidence_need="COMPANY_PRIMARY",
            time_scope="SESSION_INFORMATION_WINDOW",
            expected_information="confirmation",
            reason_code="MISSING_PRIMARY_CONFIRMATION",
            gap_id="gap:1",
        )


def test_proposed_missing_evidence_lookback_requires_lookback_scope() -> None:
    with pytest.raises(ValidationError):
        ProposedMissingEvidence(
            proposal_ref="p-1",
            evidence_need="COMPANY_PRIMARY",
            time_scope="PRIOR_SESSION",
            lookback_sessions=5,
            expected_information="confirmation",
            reason_code="MISSING_PRIMARY_CONFIRMATION",
        )
    proposal = ProposedMissingEvidence(
        proposal_ref="p-1",
        evidence_need="COMPANY_PRIMARY",
        time_scope="LOOKBACK_SESSIONS",
        lookback_sessions=5,
        expected_information="confirmation",
        reason_code="MISSING_PRIMARY_CONFIRMATION",
    )
    assert proposal.lookback_sessions == 5
    with pytest.raises(ValidationError):
        ProposedMissingEvidence(
            proposal_ref="p-1",
            evidence_need="COMPANY_PRIMARY",
            time_scope="LOOKBACK_SESSIONS",
            lookback_sessions=0,
            expected_information="confirmation",
            reason_code="MISSING_PRIMARY_CONFIRMATION",
        )


def test_duplicate_evidence_refs_rejected() -> None:
    with pytest.raises(ValidationError):
        AnalystDecision(
            **_decision(
                evidence_decisions=(
                    EvidenceDecision(
                        evidence_id="e1",
                        disposition="SUPPORT",
                        supports_hypothesis_refs=("h1",),
                        reason_code="r1",
                    ),
                    EvidenceDecision(
                        evidence_id="e1",
                        disposition="WEAK",
                        reason_code="r1",
                    ),
                )
            )
        )
    with pytest.raises(ValidationError):
        CandidateHypothesis(
            **_hypothesis(
                supporting_evidence_ids=("corpus:chunk:0001", "corpus:chunk:0001")
            )
        )


def test_same_evidence_hypothesis_pair_cannot_be_support_and_contradiction() -> None:
    with pytest.raises(ValidationError):
        EvidenceDecision(
            evidence_id="e1",
            disposition="SUPPORT",
            supports_hypothesis_refs=("h1",),
            contradicts_hypothesis_refs=("h1",),
            reason_code="r1",
        )


def test_local_hypothesis_and_proposal_references_are_validated() -> None:
    # Evidence decision referencing an unknown hypothesis ref is rejected.
    with pytest.raises(ValidationError):
        AnalystDecision(
            **_decision(
                evidence_decisions=(
                    EvidenceDecision(
                        evidence_id="corpus:chunk:0001",
                        disposition="SUPPORT",
                        supports_hypothesis_refs=("h-unknown",),
                        reason_code="material_support",
                    ),
                )
            )
        )
    # Candidate unresolved_gap_refs must resolve to a proposed missing evidence.
    with pytest.raises(ValidationError):
        AnalystDecision(
            **_decision(
                candidate_hypotheses=(
                    CandidateHypothesis(**_hypothesis(unresolved_gap_refs=("p-unknown",))),
                )
            )
        )


def test_proposed_corrective_intents_must_reference_a_proposal() -> None:
    with pytest.raises(ValidationError):
        AnalystDecision(
            **_decision(
                proposed_corrective_intents=(
                    ProposedCorrectiveIntent(proposal_ref="p-unknown", query_hints=("hint",)),
                )
            )
        )
    decision = AnalystDecision(
        **_decision(
            proposed_missing_evidence=(
                ProposedMissingEvidence(
                    proposal_ref="p-1",
                    evidence_need="COMPANY_PRIMARY",
                    time_scope="PRIOR_SESSION",
                    expected_information="issuer filing",
                    reason_code="MISSING_PRIMARY_CONFIRMATION",
                ),
            ),
            proposed_corrective_intents=(
                ProposedCorrectiveIntent(proposal_ref="p-1", query_hints=("8-K",)),
            ),
        )
    )
    assert decision.proposed_corrective_intents[0].proposal_ref == "p-1"


def test_corrective_intent_has_at_most_three_bounded_hints() -> None:
    with pytest.raises(ValidationError):
        ProposedCorrectiveIntent(
            proposal_ref="p-1",
            query_hints=("a", "b", "c", "d"),
        )
    with pytest.raises(ValidationError):
        ProposedCorrectiveIntent(
            proposal_ref="p-1",
            query_hints=("x" * 500,),
        )


def test_corrective_intent_proposal_refs_are_unique() -> None:
    with pytest.raises(ValidationError):
        AnalystDecision(
            **_decision(
                proposed_missing_evidence=(
                    ProposedMissingEvidence(
                        proposal_ref="p-1",
                        evidence_need="COMPANY_PRIMARY",
                        time_scope="PRIOR_SESSION",
                        expected_information="a",
                        reason_code="MISSING_PRIMARY_CONFIRMATION",
                    ),
                    ProposedMissingEvidence(
                        proposal_ref="p-2",
                        evidence_need="COMPANY_NEWS",
                        time_scope="SESSION_INFORMATION_WINDOW",
                        expected_information="b",
                        reason_code="MISSING_INDEPENDENT_CORROBORATION",
                    ),
                ),
                proposed_corrective_intents=(
                    ProposedCorrectiveIntent(proposal_ref="p-1", query_hints=("a",)),
                    ProposedCorrectiveIntent(proposal_ref="p-1", query_hints=("b",)),
                ),
            )
        )


def test_ontology_enums_are_distinct() -> None:
    assert set(ResearchDecision.__members__) == {"READY", "FOLLOW_UP", "ABSTAIN"}
    assert set(AttributionStatus.__members__) == {"SUFFICIENT", "PARTIAL", "ABSTAIN"}
    assert set(AttributionType.__members__) == {
        "EVIDENCE_BACKED_CAUSAL",
        "NO_MATERIAL_PUBLIC_CATALYST",
    }
    assert set(EvidenceDisposition.__members__) == {
        "SUPPORT",
        "CONTRADICT",
        "WEAK",
        "LEAD_ONLY",
        "IRRELEVANT",
    }
    assert set(MagnitudeFit.__members__) == {"STRONG", "PLAUSIBLE", "WEAK", "UNKNOWN"}
    assert set(HypothesisRole.__members__) == {
        "PRIMARY",
        "SECONDARY",
        "CONTEXT",
        "REJECTED",
    }
