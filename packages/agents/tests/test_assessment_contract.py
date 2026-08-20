"""V1.1 EvidenceAssessment contract tests (M2-6).

Code-normalized assessment binds the raw decision and pack/render hashes,
carries the code-owned status ceiling, and may hold a code-owned corrective
batch whose actions reference distinct recoverable validated gaps.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    CandidateHypothesis,
    EvidenceDecision,
)
from catalyst_agents.attribution.assessment import EvidenceAssessment
from catalyst_agents.retrieval.corrective import (
    CorrectiveResearchAction,
    CorrectiveResearchBatch,
    MissingEvidence,
)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _decision() -> AnalystDecision:
    return AnalystDecision(
        evidence_decisions=(
            EvidenceDecision(evidence_id="corpus:chunk:0001", decision="SUPPORT"),
        ),
        candidate_hypotheses=(
            CandidateHypothesis(
                hypothesis_id="h-1",
                cause_type="COMPANY_SPECIFIC_CATALYST",
                statement="AAPL rose on record guidance.",
                supporting_evidence_ids=("corpus:chunk:0001",),
                magnitude_fit="STRONG",
                proposed_role="PRIMARY",
            ),
        ),
        research_decision="FOLLOW_UP",
        recommended_status="PARTIAL",
        proposed_attribution_type="EVIDENCE_BACKED_CAUSAL",
        proposed_missing_evidence=(),
        proposed_corrective_intents=(),
    )


def _gap(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "gap_id": "gap:1",
        "evidence_need": "COMPANY_PRIMARY",
        "time_scope": "PRIOR_SESSION",
        "expected_information": "issuer filing confirmation",
        "reason_code": "MISSING_PRIMARY_CONFIRMATION",
        "recoverable": True,
    }
    base.update(overrides)
    return base


def _assessment(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "analyst_decision": _decision(),
        "validated_missing_evidence": (MissingEvidence(**_gap()),),
        "status_ceiling": "PARTIAL",
        "corrective_batch": CorrectiveResearchBatch(
            batch_id="batch:1",
            actions=(
                CorrectiveResearchAction(
                    action_id="action:1",
                    gap_id="gap:1",
                    evidence_need="COMPANY_PRIMARY",
                    time_scope="PRIOR_SESSION",
                    query_hints=("8-K",),
                    research_fingerprint="fp:1",
                ),
            ),
            shared_deadline=_utc("2026-01-06T21:00:00Z"),
            total_result_budget=20,
        ),
        "normalization_violations": (),
        "decision_hash": "d" * 64,
        "context_pack_sha256": "a" * 64,
        "rendered_messages_sha256": "b" * 64,
        "normalization_policy_version": "n1",
    }
    base.update(overrides)
    return base


def test_evidence_assessment_fields_and_hash_binding() -> None:
    assessment = EvidenceAssessment(**_assessment())
    assert assessment.status_ceiling.value == "PARTIAL"
    assert assessment.analyst_decision is not None
    assert assessment.decision_hash == "d" * 64
    assert assessment.context_pack_sha256 == "a" * 64
    assert assessment.rendered_messages_sha256 == "b" * 64
    assert assessment.normalization_policy_version == "n1"
    assert assessment.corrective_batch is not None


def test_evidence_assessment_is_strict_and_frozen() -> None:
    assessment = EvidenceAssessment(**_assessment())
    with pytest.raises(ValidationError):
        assessment.status_ceiling = "ABSTAIN"  # frozen
    with pytest.raises(ValidationError):
        EvidenceAssessment(**_assessment(), unknown_field=True)  # extra forbidden


def test_hashes_must_be_hex_strings() -> None:
    with pytest.raises(ValidationError):
        EvidenceAssessment(**_assessment(decision_hash="not-hex"))
    with pytest.raises(ValidationError):
        EvidenceAssessment(**_assessment(context_pack_sha256="x" * 63))


def test_corrective_action_must_reference_validated_recoverable_gap() -> None:
    with pytest.raises(ValidationError):
        EvidenceAssessment(
            **_assessment(
                validated_missing_evidence=(),
            )
        )
    with pytest.raises(ValidationError):
        EvidenceAssessment(
            **_assessment(
                validated_missing_evidence=(
                    MissingEvidence(**_gap(recoverable=False)),
                ),
            )
        )
    with pytest.raises(ValidationError):
        EvidenceAssessment(
            **_assessment(
                validated_missing_evidence=(
                    MissingEvidence(
                        **_gap(evidence_need="COMPANY_NEWS", time_scope="SESSION_INFORMATION_WINDOW")
                    ),
                ),
            )
        )
