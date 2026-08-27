"""C2: StructuralAssurance must genuinely fail closed.

The corrective review found the hash checks accepted synthetic placeholder
values, never recomputed input/output hashes, allowed claim-requiring answers
to pass with all markers omitted, and never bound plan/evidence/runtime
identity to the authoritative plan. These tests force real canonical hash
derivation, hex64 validation, marker requirements (with legitimate ABSTAIN
exemption), and identity binding.
"""
from __future__ import annotations

from typing import Any

from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
    MagnitudeFit,
)
from catalyst_agents.attribution.claims import (
    CitationMapEntry,
    Claim,
    ClaimRole,
    SourceRoleIndependenceSummary,
    ValidatedClaimPlan,
    WriterFormatKind,
    WriterFormatStyleContract,
    WriterSection,
)
from catalyst_agents.nodes.writer import build_writer_input
from catalyst_agents.runtime.assurance.checks import (
    derive_answer_output_hash,
    derive_writer_input_hash,
    run_structural_assurance,
)

_HEX = "abcdef0123456789"


def _validated_plan(*, status: AttributionStatus = AttributionStatus.SUFFICIENT) -> ValidatedClaimPlan:
    if status is AttributionStatus.ABSTAIN:
        claims = (
            Claim(
                claim_id="claim:ctx",
                role=ClaimRole.CONTEXT,
                statement="Evidence was bounded.",
                support_evidence_ids=(),
                counter_evidence_ids=(),
                limitations=(),
                source_hypothesis_id=None,
                magnitude_fit=None,
                conflict_refs=(),
                citation_evidence_ids=(),
                order_index=0,
            ),
            Claim(
                claim_id="claim:lim",
                role=ClaimRole.LIMITATION,
                statement="No causal explanation was established from the available evidence.",
                support_evidence_ids=(),
                counter_evidence_ids=(),
                limitations=(),
                source_hypothesis_id=None,
                magnitude_fit=None,
                conflict_refs=(),
                citation_evidence_ids=(),
                order_index=1,
            ),
        )
        required_limitations = ("No causal explanation was established from the available evidence.",)
    else:
        claims = (
            Claim(
                claim_id="claim:1",
                role=ClaimRole.PRIMARY,
                statement="AAPL rose on record guidance.",
                support_evidence_ids=("e1",),
                counter_evidence_ids=(),
                limitations=(),
                source_hypothesis_id="hyp:run:1:abc",
                magnitude_fit=MagnitudeFit.STRONG,
                conflict_refs=(),
                citation_evidence_ids=("e1",),
                order_index=0,
            ),
        )
        required_limitations = ()
    return ValidatedClaimPlan(
        status=status,
        attribution_type=AttributionType.EVIDENCE_BACKED_CAUSAL,
        claims=claims,
        assessment_hash="a" * 64,
        context_pack_sha256="c" * 64,
        evidence_state_hash="e" * 64,
        required_limitations=required_limitations,
        citation_map=tuple(
            CitationMapEntry(claim_id=claim.claim_id, citation_evidence_ids=claim.citation_evidence_ids)
            for claim in claims
        ),
        permitted_claim_ids=tuple(claim.claim_id for claim in claims),
        permitted_evidence_ids=tuple(
            sorted({eid for claim in claims for eid in claim.citation_evidence_ids})
        ),
        source_role_independence_summary=SourceRoleIndependenceSummary(direct_primary_support_count=1),
        ordering_policy_version="claim-ordering-v1",
        plan_hash="f" * 64,
    )


def _writer_input(plan: ValidatedClaimPlan) -> Any:
    if plan.status is AttributionStatus.ABSTAIN:
        contract = WriterFormatStyleContract(
            format_kind=WriterFormatKind.FIXED_ABSTENTION,
            required_sections=(WriterSection.OBSERVED_MOVE, WriterSection.LIMITATIONS),
            style_instructions=(),
        )
    else:
        contract = WriterFormatStyleContract(
            format_kind=WriterFormatKind.CAUSAL,
            required_sections=(
                WriterSection.SUMMARY,
                WriterSection.CAUSAL_EXPLANATION,
                WriterSection.LIMITATIONS,
            ),
            style_instructions=("concise",),
        )
    return build_writer_input(
        plan,
        observed_move="AAPL +9.5% on 2026-01-15",
        format_style_contract=contract,
    )


def _artifacts(**overrides: Any) -> dict[str, Any]:
    plan = _validated_plan()
    writer_input = _writer_input(plan)
    answer_text = (
        "SUMMARY\nAAPL rose on record guidance. [claim:1] (e1)\n"
        "CAUSAL_EXPLANATION\nGuidance raised revenue.\nLIMITATIONS\nNone."
    )
    base: dict[str, Any] = {
        "stream_complete": True,
        "answer_text": answer_text,
        "answer_text_sha256": derive_answer_output_hash(answer_text),
        "emitted_citations": ("e1",),
        "emitted_claim_markers": ("claim:1",),
        "permitted_claim_ids": plan.permitted_claim_ids,
        "permitted_evidence_ids": plan.permitted_evidence_ids,
        "required_sections": ("SUMMARY", "CAUSAL_EXPLANATION", "LIMITATIONS"),
        "required_limitations": plan.required_limitations,
        "emitted_status": plan.status.value,
        "emitted_attribution_type": plan.attribution_type.value,
        "validated_status": plan.status.value,
        "validated_attribution_type": plan.attribution_type.value,
        "writer_input": writer_input,
        "validated_plan": plan,
        "input_hash": derive_writer_input_hash(writer_input),
        "plan_hash": plan.plan_hash,
        "evidence_state_hash": plan.evidence_state_hash,
        "runtime_identity": "runtime:1",
        "bound_runtime_identity": "runtime:1",
        "output_hash": derive_answer_output_hash(answer_text),
        "cancellation_requested": False,
        "timed_out": False,
        "input_tokens": 10,
        "output_tokens": 20,
        "completion_state": "completed",
    }
    base.update(overrides)
    return base


def _failing_checks(artifacts: dict[str, Any]) -> set[str]:
    return {
        check.check_name
        for check in run_structural_assurance("run:1", artifacts)
        if check.status == "fail"
    }


def test_valid_artifacts_pass() -> None:
    assert _failing_checks(_artifacts()) == set()


def test_arbitrary_non_empty_hash_strings_fail() -> None:
    failed = _failing_checks(_artifacts(input_hash="not-a-hash"))
    assert "hash_coherence" in failed
    failed = _failing_checks(_artifacts(output_hash="arbitrary non-empty string"))
    assert "hash_coherence" in failed


def test_wrong_run_input_hash_fails() -> None:
    failed = _failing_checks(_artifacts(input_hash="9" * 64))
    assert "hash_coherence" in failed


def test_wrong_output_text_hash_fails() -> None:
    # output_hash claims a different text than answer_text.
    other = derive_answer_output_hash("completely different answer")
    failed = _failing_checks(_artifacts(output_hash=other))
    assert "hash_coherence" in failed
    # answer_text_sha256 must bind the persisted artifact hash too.
    failed = _failing_checks(_artifacts(answer_text_sha256="1" * 64))
    assert "hash_coherence" in failed


def test_plan_hash_mismatch_fails() -> None:
    failed = _failing_checks(_artifacts(plan_hash="1" * 64))
    assert "hash_coherence" in failed


def test_evidence_state_hash_mismatch_fails() -> None:
    failed = _failing_checks(_artifacts(evidence_state_hash="2" * 64))
    assert "hash_coherence" in failed


def test_runtime_identity_mismatch_fails() -> None:
    failed = _failing_checks(_artifacts(bound_runtime_identity="other-runtime"))
    assert "hash_coherence" in failed


def test_claim_requiring_answer_without_markers_fails() -> None:
    failed = _failing_checks(_artifacts(emitted_citations=(), emitted_claim_markers=()))
    assert "citation_resolution" in failed
    assert "claim_markers_subset" in failed


def test_claim_requiring_answer_with_partial_markers_fails() -> None:
    failed = _failing_checks(_artifacts(emitted_claim_markers=()))
    assert "claim_markers_subset" in failed


def test_abstain_answer_without_markers_passes() -> None:
    plan = _validated_plan(status=AttributionStatus.ABSTAIN)
    writer_input = _writer_input(plan)
    answer_text = (
        "OBSERVED_MOVE\nAAPL +9.5%.\n"
        "LIMITATIONS\nNo causal explanation was established from the available evidence."
    )
    artifacts = _artifacts(
        validated_plan=plan,
        writer_input=writer_input,
        required_sections=("OBSERVED_MOVE", "LIMITATIONS"),
        emitted_citations=(),
        emitted_claim_markers=(),
        permitted_claim_ids=plan.permitted_claim_ids,
        permitted_evidence_ids=plan.permitted_evidence_ids,
        required_limitations=plan.required_limitations,
        validated_status="ABSTAIN",
        emitted_status="ABSTAIN",
        answer_text=answer_text,
        answer_text_sha256=derive_answer_output_hash(answer_text),
        output_hash=derive_answer_output_hash(answer_text),
        input_hash=derive_writer_input_hash(writer_input),
    )
    assert _failing_checks(artifacts) == set()
