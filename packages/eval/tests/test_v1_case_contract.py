"""V1.1 GoldenCase contract tests (M2-10).

Eval-owned human truth per eval TSD §6 / Frozen §7.2. GoldenCase lives in
catalyst_eval only and never enters production packages, prompts, or DTOs.
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
        "role": "primary_support",
        "support": True,
        "materiality": "material",
        "temporal_eligible": True,
        "independence_group": None,
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
        "lineage": {"source_case_id": "t4-0001"},
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
