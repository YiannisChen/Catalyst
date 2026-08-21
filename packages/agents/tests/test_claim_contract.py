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
    WriterFormatStyleContract,
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


def _validated_plan(**overrides: Any) -> dict[str, Any]:
    """A ValidatedClaimPlan fixture carrying the Phase 4 §27 permitted-ID
    surface consistent with its (possibly overridden) claims."""
    data = {**_plan(), **overrides}
    claims = data["claims"]
    if "permitted_claim_ids" not in data:
        data["permitted_claim_ids"] = tuple(claim.claim_id for claim in claims)
    if "permitted_evidence_ids" not in data:
        evidence_ids: set[str] = set()
        for claim in claims:
            evidence_ids.update(claim.support_evidence_ids)
            evidence_ids.update(claim.counter_evidence_ids)
            evidence_ids.update(claim.citation_evidence_ids)
        data["permitted_evidence_ids"] = tuple(sorted(evidence_ids))
    return data


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
        "validated_claim_plan": ValidatedClaimPlan(
            **_validated_plan(citation_map={"claim-1": ("corpus:chunk:0001",)})
        ),
        "narrowly_bound_supporting_snippets": {
            "corpus:chunk:0001": "AAPL reported record quarterly revenue."
        },
        "citation_map": {"claim-1": ("corpus:chunk:0001",)},
        "required_limitations": ("magnitude coverage is partial",),
        "format_style_contract": WriterFormatStyleContract(
            required_sections=("summary",),
            style_instructions=("neutral tone",),
        ),
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
    assert fields == {
        "status",
        "attribution_type",
        "claims",
        "assessment_hash",
        "context_pack_sha256",
        "evidence_state_hash",
        "required_limitations",
        "citation_map",
        "permitted_claim_ids",
        "permitted_evidence_ids",
        "source_role_independence_summary",
        "ordering_policy_version",
        "plan_hash",
    }
    assert "confidence" not in fields
    assert "probability" not in fields
    validated = ValidatedClaimPlan(**_validated_plan())
    assert validated.status is AttributionStatus.PARTIAL


def test_writer_input_final_status_and_type_match_validated_plan() -> None:
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                final_status="ABSTAIN",
                validated_claim_plan=ValidatedClaimPlan(**_validated_plan()),
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
                    **_validated_plan(status="ABSTAIN", claims=(Claim(**_claim()),))
                ),
            )
        )
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                final_status="ABSTAIN",
                attribution_type="EVIDENCE_BACKED_CAUSAL",
                validated_claim_plan=ValidatedClaimPlan(
                    **_validated_plan(
                        status="ABSTAIN",
                        attribution_type="EVIDENCE_BACKED_CAUSAL",
                        claims=(
                            Claim(
                                claim_id="claim-2",
                                role="SECONDARY",
                                statement="Secondary factor.",
                                support_evidence_ids=("corpus:chunk:0001",),
                            ),
                        ),
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
                    **_validated_plan(
                        status="ABSTAIN",
                        attribution_type="NO_MATERIAL_PUBLIC_CATALYST",
                        claims=(),
                    )
                ),
            )
        )


def test_fixed_abstention_accepts_only_limitation_and_context_surface() -> None:
    abstain = WriterInput(
        **_writer_input(
            final_status="ABSTAIN",
            attribution_type="EVIDENCE_BACKED_CAUSAL",
            validated_claim_plan=ValidatedClaimPlan(
                **_validated_plan(
                    status="ABSTAIN",
                    attribution_type="EVIDENCE_BACKED_CAUSAL",
                    claims=(
                        Claim(
                            claim_id="limit-1",
                            role="LIMITATION",
                            statement="No accepted cause under eligible evidence.",
                            limitations=(),
                        ),
                    ),
                    citation_map={"limit-1": ()},
                    permitted_claim_ids=("limit-1",),
                    permitted_evidence_ids=(),
                )
            ),
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


def test_claim_plan_includes_phase_4_23_contract_fields() -> None:
    """Detailed Phase 4 §23 fields govern over the abbreviated M2-7 shape:
    source hypothesis identity, magnitude fit, conflict refs, citation IDs,
    ordering metadata, input artifact hashes, required limitations, plan hash."""
    fields = set(ClaimPlan.model_fields)
    for required in (
        "status",
        "attribution_type",
        "claims",
        "assessment_hash",
        "context_pack_sha256",
        "evidence_state_hash",
        "required_limitations",
        "ordering_policy_version",
        "plan_hash",
    ):
        assert required in fields, f"ClaimPlan missing {required}"
    claim_fields = set(Claim.model_fields)
    for required in (
        "claim_id",
        "role",
        "statement",
        "mechanism",
        "support_evidence_ids",
        "counter_evidence_ids",
        "limitations",
        "source_hypothesis_id",
        "magnitude_fit",
        "conflict_refs",
        "citation_evidence_ids",
        "order_index",
    ):
        assert required in claim_fields, f"Claim missing {required}"


def test_claim_plan_hash_fields_are_sha256_hex() -> None:
    with pytest.raises(ValidationError):
        ClaimPlan(**_plan(assessment_hash="not-hex"))
    with pytest.raises(ValidationError):
        ClaimPlan(**_plan(context_pack_sha256="x" * 63))
    with pytest.raises(ValidationError):
        ClaimPlan(**_plan(evidence_state_hash="y" * 65))
    with pytest.raises(ValidationError):
        ClaimPlan(**_plan(plan_hash="short"))


def test_claim_citation_ids_resolve_to_bound_evidence() -> None:
    with pytest.raises(ValidationError):
        Claim(
            **_claim(
                support_evidence_ids=("corpus:chunk:0001",),
                citation_evidence_ids=("corpus:chunk:9999",),
            )
        )
    claim = Claim(
        **_claim(
            support_evidence_ids=("corpus:chunk:0001",),
            citation_evidence_ids=("corpus:chunk:0001",),
        )
    )
    assert claim.citation_evidence_ids == ("corpus:chunk:0001",)


def test_claim_order_index_must_be_non_negative_and_unique_in_plan() -> None:
    with pytest.raises(ValidationError):
        Claim(**_claim(order_index=-1))
    with pytest.raises(ValidationError):
        ClaimPlan(
            **_plan(
                claims=(
                    Claim(**_claim(order_index=0)),
                    Claim(
                        **_claim(
                            claim_id="claim-2",
                            role="SECONDARY",
                            order_index=0,
                        )
                    ),
                )
            )
        )


def test_validated_claim_plan_includes_phase_4_27_contract_fields() -> None:
    fields = set(ValidatedClaimPlan.model_fields)
    for required in (
        "status",
        "attribution_type",
        "claims",
        "assessment_hash",
        "context_pack_sha256",
        "evidence_state_hash",
        "required_limitations",
        "citation_map",
        "permitted_claim_ids",
        "permitted_evidence_ids",
        "source_role_independence_summary",
        "ordering_policy_version",
        "plan_hash",
    ):
        assert required in fields, f"ValidatedClaimPlan missing {required}"


def test_validated_claim_plan_permitted_ids_are_consistent() -> None:
    claim = Claim(
        **_claim(
            support_evidence_ids=("corpus:chunk:0001",),
            citation_evidence_ids=("corpus:chunk:0001",),
        )
    )
    with pytest.raises(ValidationError):
        ValidatedClaimPlan(
            **_plan(
                claims=(claim,),
                permitted_claim_ids=("claim-other",),
            )
        )
    with pytest.raises(ValidationError):
        ValidatedClaimPlan(
            **_plan(
                claims=(claim,),
                permitted_evidence_ids=(),
            )
        )
    plan = ValidatedClaimPlan(
        **_plan(
            claims=(claim,),
            permitted_claim_ids=("claim-1",),
            permitted_evidence_ids=("corpus:chunk:0001",),
            citation_map={"claim-1": ("corpus:chunk:0001",)},
        )
    )
    assert plan.permitted_claim_ids == ("claim-1",)


def test_writer_input_uses_typed_format_style_contract() -> None:
    from catalyst_agents.attribution.claims import WriterFormatStyleContract

    writer = WriterInput(
        **_writer_input(
            format_style_contract=WriterFormatStyleContract(
                required_sections=("summary", "limitations"),
                style_instructions=("neutral tone",),
            )
        )
    )
    assert writer.format_style_contract.required_sections == ("summary", "limitations")
    with pytest.raises(ValidationError):
        WriterInput(**_writer_input(format_style_constraints={"tone": "neutral"}))
    with pytest.raises(ValidationError):
        WriterFormatStyleContract(
            required_sections=("x" * 500,),
        )


def test_writer_input_citation_map_matches_validated_plan() -> None:
    claim = Claim(
        **_claim(
            support_evidence_ids=("corpus:chunk:0001",),
            citation_evidence_ids=("corpus:chunk:0001",),
        )
    )
    plan = ValidatedClaimPlan(
        **_plan(
            claims=(claim,),
            citation_map={"claim-1": ("corpus:chunk:0001",)},
            permitted_claim_ids=("claim-1",),
            permitted_evidence_ids=("corpus:chunk:0001",),
        )
    )
    with pytest.raises(ValidationError):
        WriterInput(
            **_writer_input(
                validated_claim_plan=plan,
                citation_map={"claim-1": ("corpus:chunk:0001", "corpus:chunk:9999")},
            )
        )
    ok = WriterInput(
        **_writer_input(
            validated_claim_plan=plan,
            citation_map={"claim-1": ("corpus:chunk:0001",)},
            format_style_contract=None,
        )
    )
    assert ok.citation_map == {"claim-1": ("corpus:chunk:0001",)}
