"""V1.1 GoldenCase contract tests (M2-10, corrective).

Eval-owned human truth per eval TSD §6 / Frozen §7.2. Judgments preserve
canonical asset + content/chunk/fact + evidence-group identity; materiality
and temporal eligibility stay separate; typed human rationale/lineage fields
are retained; all gold fields live in catalyst_eval only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_eval.v1_1.case import (
    AcceptableCauseLabel,
    EvidenceJudgmentV1,
    ExpectedResearchBehavior,
    GoldenCase,
    GoldenCaseLineage,
)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _cause_label(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "cause_type": "COMPANY_SPECIFIC_CATALYST",
        "label": "AAPL record quarterly guidance",
        "direction": "positive",
        "materiality": "material",
    }
    base.update(overrides)
    return base


def _judgment(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "evidence_id": "corpus:chunk:0001",
        "canonical_asset_id": "issuer:AAPL:news:0001",
        "canonical_content_version_id": "content:v1:0001",
        "chunk_id": "corpus:chunk:0001",
        "fact_id": None,
        "role": "primary_support",
        "support": True,
        "materiality": "material",
        "temporal_eligible": True,
        "independence_group": None,
        "rationale": "Direct issuer disclosure within cutoff.",
        "annotator": "reviewer-1",
        "annotated_at": _utc("2026-01-07T10:00:00Z"),
    }
    base.update(overrides)
    return base


def _lineage(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "t4-0001",
        "model_assisted_fields": (),
        "human_confirmed_fields": ("oracle_status", "evidence_judgments"),
        "annotated_at": _utc("2026-01-07T10:00:00Z"),
        "adjudication_state": "resolved",
    }
    base.update(overrides)
    return base


def _research_behavior(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "acceptable_initial_tasks": ("COMPANY_PRIMARY", "COMPANY_NEWS"),
        "expected_gap_reason_codes": ("MISSING_PRIMARY_CONFIRMATION",),
        "acceptable_corrective_actions": (
            {
                "evidence_need": "COMPANY_PRIMARY",
                "time_scope": "PRIOR_SESSION",
            },
        ),
        "corrective_recoverable": True,
        "corrective_required": True,
    }
    base.update(overrides)
    return base


def _case(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "case_id": "stage1-001",
        "ticker": "AAPL",
        "session_date": "2026-01-06",
        "cutoff": _utc("2026-01-06T21:00:00Z"),
        "question": "Why did AAPL rise on 2026-01-06?",
        "oracle_status": "PARTIAL",
        "acceptable_cause_labels": (AcceptableCauseLabel(**_cause_label()),),
        "evidence_judgments": (EvidenceJudgmentV1(**_judgment()),),
        "expected_primary_evidence": ("corpus:chunk:0001",),
        "expected_refusal_reason": None,
        "expected_attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "expected_research_behavior": ExpectedResearchBehavior(**_research_behavior()),
        "notes": None,
        "dataset_version": "v1",
        "lineage": GoldenCaseLineage(**_lineage()),
    }
    base.update(overrides)
    return base


def test_golden_case_fields_match_eval_tsd_6() -> None:
    case = GoldenCase(**_case())
    assert set(GoldenCase.model_fields) == {
        "case_id",
        "ticker",
        "session_date",
        "cutoff",
        "question",
        "oracle_status",
        "acceptable_cause_labels",
        "evidence_judgments",
        "expected_primary_evidence",
        "expected_refusal_reason",
        "expected_attribution_type",
        "expected_research_behavior",
        "notes",
        "dataset_version",
        "lineage",
    }
    assert case.cutoff == _utc("2026-01-06T21:00:00Z")
    assert case.lineage.adjudication_state == "resolved"


def test_golden_case_is_strict_and_frozen() -> None:
    case = GoldenCase(**_case())
    with pytest.raises(ValidationError):
        case.case_id = "other"  # frozen
    with pytest.raises(ValidationError):
        GoldenCase(**_case(), unknown_field=True)  # extra forbidden


def test_question_has_no_answer_hint_field() -> None:
    fields = set(GoldenCase.model_fields)
    assert "question" in fields
    assert "gold_answer" not in fields
    assert "answer" not in fields


def test_evidence_judgment_roles_are_closed() -> None:
    for role in (
        "primary_support",
        "secondary_support",
        "contradiction",
        "lead_only",
        "irrelevant",
        "ineligible",
    ):
        assert EvidenceJudgmentV1(**_judgment(role=role)).role == role
    with pytest.raises(ValidationError):
        EvidenceJudgmentV1(**_judgment(role="supporting"))


def test_evidence_judgment_preserves_content_and_evidence_group_identity() -> None:
    judgment = EvidenceJudgmentV1(**_judgment())
    assert judgment.canonical_content_version_id == "content:v1:0001"
    assert judgment.chunk_id == "corpus:chunk:0001"
    assert judgment.evidence_id == judgment.chunk_id
    with pytest.raises(ValidationError):
        EvidenceJudgmentV1(**_judgment(fact_id="fact:1"))  # chunk+fact conflict
    structured = EvidenceJudgmentV1(
        **_judgment(
            evidence_id="fact:42",
            canonical_asset_id="issuer:AAPL:struct:1",
            chunk_id=None,
            fact_id="fact:42",
            role="secondary_support",
        )
    )
    assert structured.evidence_id == structured.fact_id
    grouped = EvidenceJudgmentV1(**_judgment(independence_group="syndication:g1"))
    assert grouped.independence_group == "syndication:g1"


def test_evidence_judgment_materiality_and_temporal_eligibility_stay_separate() -> None:
    judgment = EvidenceJudgmentV1(
        **_judgment(
            materiality="non_material",
            temporal_eligible=False,
        )
    )
    assert judgment.materiality == "non_material"
    assert judgment.temporal_eligible is False


def test_evidence_judgment_rationale_is_bounded() -> None:
    with pytest.raises(ValidationError):
        EvidenceJudgmentV1(**_judgment(rationale="x" * 501))


def test_acceptable_cause_labels_use_only_causal_types() -> None:
    with pytest.raises(ValidationError):
        AcceptableCauseLabel(**_cause_label(cause_type="NO_MATERIAL_PUBLIC_CATALYST"))
    with pytest.raises(ValidationError):
        AcceptableCauseLabel(**_cause_label(cause_type="MARKET_STRUCTURE_UNSUPPORTED"))
    with pytest.raises(ValidationError):
        AcceptableCauseLabel(**_cause_label(direction="up"))
    with pytest.raises(ValidationError):
        AcceptableCauseLabel(**_cause_label(materiality="huge"))


def test_expected_research_behavior_is_typed() -> None:
    behavior = ExpectedResearchBehavior(**_research_behavior())
    assert behavior.acceptable_initial_tasks == ("COMPANY_PRIMARY", "COMPANY_NEWS")
    assert behavior.corrective_recoverable is True
    assert behavior.corrective_required is True
    assert behavior.acceptable_corrective_actions[0].evidence_need == "COMPANY_PRIMARY"
    with pytest.raises(ValidationError):
        ExpectedResearchBehavior(
            **_research_behavior(
                expected_gap_reason_codes=("NOT_A_REASON_CODE",)
            )
        )


def test_golden_case_lives_in_eval_package() -> None:
    assert GoldenCase.__module__.startswith("catalyst_eval.")
