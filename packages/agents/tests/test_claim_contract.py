"""V1.1 claim boundary contract tests (M2-7, corrective).

ClaimPlan/ValidatedClaimPlan/WriterInput plus the two ClaimValidator failure
classes (Final Migration TSD §13; Frozen §6.5). AGENT-01 lock: an ABSTAIN
WriterInput follows the fixed abstention path — no PRIMARY/SECONDARY causal
claims, no NO_MATERIAL attribution type, and is_fixed_abstention validates the
actual structure rather than a boolean marker.
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


def _abstention_plan(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "ABSTAIN",
        "attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "claims": (
            Claim(
                claim_id="limit-1",
                role="LIMITATION",
                statement="No accepted cause under eligible evidence.",
                limitations=(),
            ),
        ),
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


def test_claim_ids_and_evidence_refs_are_unique() -> None:
    with pytest.raises(ValidationError):
        ClaimPlan(
            **_plan(
                claims=(
                    Claim(**_claim()),
                    Claim(**_claim(claim_id="claim-1", role="SECONDARY")),
                )
            )
        )
    with pytest.raises(ValidationError):
        Claim(
            **_claim(support_evidence_ids=("e1", "e1"))
        )
    with pytest.raises(ValidationError):
        Claim(
            **_claim(counter_evidence_ids=("e2", "e2"))
        )


def test_support_and_counter_refs_cannot_overlap() -> None:
    with pytest.raises(ValidationError):
        Claim(
            **_claim(
                support_evidence_ids=("e1",),
                counter_evidence_ids=("e1",),
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


def test_writer_input_final_status_and_type_match_validated_plan() -> None:
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                final_status="ABSTAIN",
                validated_claim_plan=ValidatedClaimPlan(**_plan()),
            )
        )
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
                validated_claim_plan=ValidatedClaimPlan(
                    **_plan(attribution_type="EVIDENCE_BACKED_CAUSAL")
                ),
            )
        )


def test_writer_input_has_no_boolean_abstention_marker() -> None:
    assert "abstention_marker" not in WriterInput.model_fields
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                final_status="PARTIAL",
                abstention_marker=True,
            )
        )


def test_abstain_writer_input_rejects_primary_and_secondary_causal_claims() -> None:
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                final_status="ABSTAIN",
                attribution_type="EVIDENCE_BACKED_CAUSAL",
                validated_claim_plan=ValidatedClaimPlan(
                    **_plan(status="ABSTAIN", claims=(Claim(**_claim()),))
                ),
            )
        )
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                final_status="ABSTAIN",
                attribution_type="EVIDENCE_BACKED_CAUSAL",
                validated_claim_plan=ValidatedClaimPlan(
                    **_abstention_plan(
                        claims=(
                            Claim(
                                claim_id="claim-2",
                                role="SECONDARY",
                                statement="Secondary factor.",
                                support_evidence_ids=("corpus:chunk:0001",),
                            ),
                        )
                    )
                ),
            )
        )


def test_abstain_writer_input_rejects_no_material_type() -> None:
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                final_status="ABSTAIN",
                attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
                validated_claim_plan=ValidatedClaimPlan(
                    **_abstention_plan(
                        attribution_type="NO_MATERIAL_PUBLIC_CATALYST"
                    )
                ),
            )
        )


def test_fixed_abstention_accepts_only_limitation_and_context_surface() -> None:
    abstain = WriterInput(
        **_writer_input(
            final_status="ABSTAIN",
            attribution_type="EVIDENCE_BACKED_CAUSAL",
            validated_claim_plan=ValidatedClaimPlan(**_abstention_plan()),
            citation_map={"limit-1": ()},
            narrowly_bound_supporting_snippets={},
            required_limitations=("No accepted cause under eligible evidence.",),
        )
    )
    assert is_fixed_abstention(abstain) is True
    partial = WriterInput(**_writer_input())
    assert is_fixed_abstention(partial) is False


def test_citation_map_must_reference_known_claims_and_evidence() -> None:
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                citation_map={"claim-unknown": ("corpus:chunk:0001",)}
            )
        )
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                citation_map={"claim-1": ("corpus:chunk:9999",)}
            )
        )


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
