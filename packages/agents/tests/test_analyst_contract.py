"""V1.1 AnalystDecision contract tests (M2-6).

Raw model decision surface with CLAIM-01/AGENT-01 locks: no status_ceiling,
no executable batch, proposal_ref is not a runtime gap ID, CauseType is
restricted to the six causal values, at most three hypotheses.
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
    MagnitudeFit,
    HypothesisRole,
    ProposedCorrectiveIntent,
    ProposedMissingEvidence,
    ResearchDecision,
)


def _hypothesis(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "hypothesis_id": "h-1",
        "cause_type": "COMPANY_SPECIFIC_CATALYST",
        "statement": "AAPL rose on record guidance.",
        "mechanism": None,
        "supporting_evidence_ids": ("corpus:chunk:0001",),
        "contradicting_evidence_ids": (),
        "magnitude_fit": "STRONG",
        "proposed_role": "PRIMARY",
        "unresolved_gap_ids": (),
    }
    base.update(overrides)
    return base


def _decision(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "evidence_decisions": (
            EvidenceDecision(evidence_id="corpus:chunk:0001", decision="SUPPORT"),
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
    with pytest.raises(ValidationError):
        decision.research_decision = "FOLLOW_UP"  # frozen
    with pytest.raises(ValidationError):
        AnalystDecision(**_decision(), unknown_field=True)  # extra forbidden


def test_evidence_decisions_dispositions_are_closed() -> None:
    for value in ("SUPPORT", "CONTRADICT", "WEAK", "LEAD_ONLY", "IRRELEVANT"):
        assert EvidenceDecision(evidence_id="e1", decision=value).decision == value
    with pytest.raises(ValidationError):
        EvidenceDecision(evidence_id="e1", decision="RELEVANT")


def test_at_most_three_candidate_hypotheses() -> None:
    hypotheses = tuple(
        CandidateHypothesis(**_hypothesis(hypothesis_id=f"h-{i}")) for i in range(4)
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
    with pytest.raises(ValidationError):
        AnalystDecision(**_decision(status_ceiling="PARTIAL"))
    with pytest.raises(ValidationError):
        AnalystDecision(**_decision(corrective_batch={"batch_id": "b1"}))


def test_proposal_ref_is_not_a_runtime_gap_id() -> None:
    fields = set(ProposedMissingEvidence.model_fields)
    assert fields == {
        "proposal_ref",
        "evidence_need",
        "time_scope",
        "expected_information",
        "reason_code",
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


def test_duplicate_evidence_refs_rejected() -> None:
    with pytest.raises(ValidationError):
        AnalystDecision(
            **_decision(
                evidence_decisions=(
                    EvidenceDecision(evidence_id="e1", decision="SUPPORT"),
                    EvidenceDecision(evidence_id="e1", decision="WEAK"),
                )
            )
        )
    with pytest.raises(ValidationError):
        CandidateHypothesis(
            **_hypothesis(
                supporting_evidence_ids=("corpus:chunk:0001", "corpus:chunk:0001")
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


def test_ontology_enums_are_distinct() -> None:
    # ResearchDecision is workflow-only; status/type are result ontology only.
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
