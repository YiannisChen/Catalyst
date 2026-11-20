"""WP-C C2: WriterInput gate for the Frozen-A4 ABSTAIN + NO_MATERIAL pair.

Frozen-A4 §2/§4 lets ``WriterInput`` accept ``ABSTAIN +
NO_MATERIAL_PUBLIC_CATALYST`` only when the assessment records that the
NO_MATERIAL gates passed. The flag is a *recorded* fact (never inferred from
the type). The pair still carries no PRIMARY/SECONDARY causal claim, and
``SUFFICIENT + NO_MATERIAL_PUBLIC_CATALYST`` stays illegal.
"""
from __future__ import annotations

import pytest
from catalyst_agents.attribution.analyst import AttributionStatus, AttributionType
from catalyst_agents.attribution.claims import (
    CitationMapEntry,
    Claim,
    ClaimRole,
    SourceRoleIndependenceSummary,
    ValidatedClaimPlan,
    WriterFormatKind,
    WriterFormatStyleContract,
    WriterInput,
    WriterSection,
)
from pydantic import ValidationError

_H64 = "a" * 64


def _limitation_claim() -> Claim:
    return Claim(
        claim_id="limit-1",
        role=ClaimRole.LIMITATION,
        statement="No material public cause was established.",
        support_evidence_ids=(),
        counter_evidence_ids=(),
        limitations=(),
        source_hypothesis_id=None,
        magnitude_fit=None,
        conflict_refs=(),
        citation_evidence_ids=(),
        order_index=0,
    )


def _primary_claim() -> Claim:
    return Claim(
        claim_id="claim-1",
        role=ClaimRole.PRIMARY,
        statement="A company-specific catalyst drove the move.",
        support_evidence_ids=("e1",),
        counter_evidence_ids=(),
        limitations=(),
        source_hypothesis_id="hyp:1",
        magnitude_fit="STRONG",
        conflict_refs=(),
        citation_evidence_ids=("e1",),
        order_index=0,
    )


def _validated_plan(
    *,
    status: AttributionStatus,
    attribution_type: AttributionType,
    claims: tuple[Claim, ...],
    required_limitations: tuple[str, ...],
) -> ValidatedClaimPlan:
    return ValidatedClaimPlan(
        status=status,
        attribution_type=attribution_type,
        claims=claims,
        assessment_hash=_H64,
        context_pack_sha256=_H64,
        evidence_state_hash=_H64,
        required_limitations=required_limitations,
        citation_map=tuple(
            CitationMapEntry(
                claim_id=claim.claim_id,
                citation_evidence_ids=claim.citation_evidence_ids,
            )
            for claim in claims
        ),
        permitted_claim_ids=tuple(claim.claim_id for claim in claims),
        permitted_evidence_ids=tuple(
            sorted({eid for claim in claims for eid in claim.citation_evidence_ids})
        ),
        source_role_independence_summary=SourceRoleIndependenceSummary(),
        ordering_policy_version="claim-ordering-v1",
        plan_hash=_H64,
    )


def _fixed_abstention_contract() -> WriterFormatStyleContract:
    return WriterFormatStyleContract(
        format_kind=WriterFormatKind.FIXED_ABSTENTION,
        required_sections=(WriterSection.OBSERVED_MOVE, WriterSection.LIMITATIONS),
        style_instructions=(),
    )


def _causal_contract() -> WriterFormatStyleContract:
    return WriterFormatStyleContract(
        format_kind=WriterFormatKind.CAUSAL,
        required_sections=(
            WriterSection.SUMMARY,
            WriterSection.CAUSAL_EXPLANATION,
            WriterSection.LIMITATIONS,
        ),
        style_instructions=("concise",),
    )


def _abstain_no_material_input(
    *,
    no_material_gates_passed: bool,
    claims: tuple[Claim, ...] | None = None,
) -> WriterInput:
    claims = claims if claims is not None else (_limitation_claim(),)
    plan = _validated_plan(
        status=AttributionStatus.ABSTAIN,
        attribution_type=AttributionType.NO_MATERIAL_PUBLIC_CATALYST,
        claims=claims,
        required_limitations=("Coverage gap: MARKET_STRUCTURE_UNSUPPORTED.",),
    )
    return WriterInput(
        final_status=AttributionStatus.ABSTAIN,
        attribution_type=AttributionType.NO_MATERIAL_PUBLIC_CATALYST,
        observed_move="An unremarkable session move.",
        validated_claim_plan=plan,
        narrowly_bound_supporting_snippets=(),
        citation_map=plan.citation_map,
        required_limitations=plan.required_limitations,
        format_style_contract=_fixed_abstention_contract(),
        no_material_gates_passed=no_material_gates_passed,
    )


def test_writer_input_accepts_abstain_no_material_with_gates_passed_flag() -> None:
    writer_input = _abstain_no_material_input(no_material_gates_passed=True)
    assert writer_input.final_status is AttributionStatus.ABSTAIN
    assert writer_input.attribution_type is AttributionType.NO_MATERIAL_PUBLIC_CATALYST
    assert writer_input.no_material_gates_passed is True


def test_writer_input_rejects_abstain_no_material_without_gates_passed_flag() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _abstain_no_material_input(no_material_gates_passed=False)
    assert "gates" in str(excinfo.value).lower()


def test_writer_input_rejects_abstain_no_material_with_causal_claim() -> None:
    with pytest.raises(ValidationError):
        _abstain_no_material_input(
            no_material_gates_passed=True, claims=(_primary_claim(),)
        )


def test_writer_input_rejects_sufficient_no_material() -> None:
    plan = _validated_plan(
        status=AttributionStatus.SUFFICIENT,
        attribution_type=AttributionType.NO_MATERIAL_PUBLIC_CATALYST,
        claims=(_primary_claim(),),
        required_limitations=(),
    )
    with pytest.raises(ValidationError):
        WriterInput(
            final_status=AttributionStatus.SUFFICIENT,
            attribution_type=AttributionType.NO_MATERIAL_PUBLIC_CATALYST,
            observed_move="A large session move.",
            validated_claim_plan=plan,
            narrowly_bound_supporting_snippets=(),
            citation_map=plan.citation_map,
            required_limitations=plan.required_limitations,
            format_style_contract=_causal_contract(),
            no_material_gates_passed=True,
        )


def test_writer_input_rejects_abstain_evidence_backed_without_flag_still_works() -> None:
    """The pre-existing ABSTAIN + EVIDENCE_BACKED_CAUSAL path is unchanged."""
    plan = _validated_plan(
        status=AttributionStatus.ABSTAIN,
        attribution_type=AttributionType.EVIDENCE_BACKED_CAUSAL,
        claims=(_limitation_claim(),),
        required_limitations=("No accepted cause under eligible evidence.",),
    )
    writer_input = WriterInput(
        final_status=AttributionStatus.ABSTAIN,
        attribution_type=AttributionType.EVIDENCE_BACKED_CAUSAL,
        observed_move="An unremarkable session move.",
        validated_claim_plan=plan,
        narrowly_bound_supporting_snippets=(),
        citation_map=plan.citation_map,
        required_limitations=plan.required_limitations,
        format_style_contract=_fixed_abstention_contract(),
    )
    assert writer_input.no_material_gates_passed is False
