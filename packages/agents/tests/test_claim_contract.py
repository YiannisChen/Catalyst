"""V1.1 claim boundary contract tests (M2-7).

ClaimPlan/ValidatedClaimPlan/WriterInput plus the two ClaimValidator failure
classes (Final Migration TSD §13; Frozen §6.5). AGENT-01 lock: ABSTAIN uses
the fixed abstention path regardless of attribution_type.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.claim_validation import (
    AttributionSupportFailure,
    ClaimValidatorFailureType,
    IntegritySystemFailure,
)
from catalyst_agents.attribution.claims import (
    Claim,
    ClaimPlan,
    ClaimRole,
    ValidatedClaimPlan,
    WriterInput,
    is_fixed_abstention,
)
from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
)


def _claim(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "claim_id": "claim-1",
        "role": "PRIMARY",
        "statement": "AAPL rose on record guidance.",
        "mechanism": None,
        "support_evidence_ids": ("corpus:chunk:0001",),
        "counter_evidence_ids": (),
        "limitations": (),
    }
    base.update(overrides)
    return base


def _plan(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "PARTIAL",
        "attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "claims": (Claim(**_claim()),),
    }
    base.update(overrides)
    return base


def _writer_input(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "final_status": "PARTIAL",
        "attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "observed_move": "AAPL +3.2%",
        "validated_claim_plan": ValidatedClaimPlan(**_plan()),
        "narrowly_bound_supporting_snippets": {
            "corpus:chunk:0001": "AAPL reported record quarterly revenue."
        },
        "citation_map": {"claim-1": ("corpus:chunk:0001",)},
        "required_limitations": ("magnitude coverage is partial",),
        "format_style_constraints": {"tone": "neutral"},
    }
    base.update(overrides)
    return base


def test_claim_plan_is_strict_and_frozen() -> None:
    plan = ClaimPlan(**_plan())
    with pytest.raises(ValidationError):
        plan.status = "ABSTAIN"  # frozen
    with pytest.raises(ValidationError):
        ClaimPlan(**_plan(), unknown_field=True)  # extra forbidden


def test_at_most_one_primary_claim() -> None:
    with pytest.raises(ValidationError):
        ClaimPlan(
            **_plan(
                claims=(
                    Claim(**_claim()),
                    Claim(**_claim(claim_id="claim-2", role="PRIMARY")),
                )
            )
        )


def test_claim_roles_are_closed() -> None:
    assert [member.value for member in ClaimRole] == [
        "PRIMARY",
        "SECONDARY",
        "CONTEXT",
        "LIMITATION",
    ]
    with pytest.raises(ValidationError):
        Claim(**_claim(role="DRIVER"))


def test_validated_claim_plan_is_maximum_public_surface_without_confidence() -> None:
    fields = set(ValidatedClaimPlan.model_fields)
    assert fields == {"status", "attribution_type", "claims"}
    assert "confidence" not in fields
    assert "probability" not in fields
    validated = ValidatedClaimPlan(**_plan())
    assert validated.status is AttributionStatus.PARTIAL


def test_writer_input_carries_abstention_marker_instead_of_causal_language() -> None:
    fields = set(WriterInput.model_fields)
    assert "abstention_marker" in fields
    abstain = WriterInput(
        **_writer_input(
            final_status="ABSTAIN",
            attribution_type="EVIDENCE_BACKED_CAUSAL",
        )
    )
    assert abstain.abstention_marker is True
    partial = WriterInput(**_writer_input())
    assert partial.abstention_marker is False
    # ABSTAIN without the marker is inconsistent.
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                final_status="ABSTAIN",
                abstention_marker=False,
            )
        )


def test_is_fixed_abstention_ignores_attribution_type() -> None:
    abstain = WriterInput(
        **_writer_input(
            final_status="ABSTAIN",
            attribution_type="EVIDENCE_BACKED_CAUSAL",
        )
    )
    assert is_fixed_abstention(abstain) is True
    partial = WriterInput(
        **_writer_input(
            final_status="PARTIAL",
            attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
        )
    )
    assert is_fixed_abstention(partial) is False


def test_claim_validator_failure_classes_are_distinct() -> None:
    support = AttributionSupportFailure(code="insufficient_material_support")
    integrity = IntegritySystemFailure(code="missing_evidence_id")
    assert support.failure_type is ClaimValidatorFailureType.ATTRIBUTION_SUPPORT
    assert integrity.failure_type is ClaimValidatorFailureType.INTEGRITY_SYSTEM
    assert (
        ClaimValidatorFailureType.ATTRIBUTION_SUPPORT.value
        == "ATTRIBUTION_SUPPORT"
    )
    assert (
        ClaimValidatorFailureType.INTEGRITY_SYSTEM.value
        == "INTEGRITY_SYSTEM"
    )
