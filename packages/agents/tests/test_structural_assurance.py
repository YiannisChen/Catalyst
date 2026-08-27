"""M5-8: StructuralAssurance + thin Finalizer.

Final TSD §13; Phase 4 TSD §31; M5 plan M5-8. StructuralAssurance guarantees
valid stream completion, allowed claim/citation marker subsets with same-run
citation resolution, required sections/limitations, status/type equality with
the ValidatedClaimPlan, input/plan/evidence/runtime/output hash coherence, and
cancellation/timeout/token/completion metadata consistency. It never rewrites
output or calls a model. Text before assurance is PROVISIONAL_RENDERING;
assurance failure invalidates the answer and fails closed with no retry. The
thin Finalizer persists only an assured terminal result envelope.
"""
from __future__ import annotations

from typing import Any

import pytest

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
)
from catalyst_agents.nodes.finalizer import PROVISIONAL_RENDERING, thin_finalizer
from catalyst_agents.runtime.assurance.checks import run_structural_assurance
from catalyst_agents.runtime.assurance.record import STRUCTURAL_CHECK_ORDER
from catalyst_agents.runtime.delta_sink import InMemoryDeltaSink


def _validated_plan() -> ValidatedClaimPlan:
    claim = Claim(
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
    )
    return ValidatedClaimPlan(
        status=AttributionStatus.SUFFICIENT,
        attribution_type=AttributionType.EVIDENCE_BACKED_CAUSAL,
        claims=(claim,),
        assessment_hash="a" * 64,
        context_pack_sha256="c" * 64,
        evidence_state_hash="e" * 64,
        required_limitations=(),
        citation_map=(CitationMapEntry(claim_id="claim:1", citation_evidence_ids=("e1",)),),
        permitted_claim_ids=("claim:1",),
        permitted_evidence_ids=("e1",),
        source_role_independence_summary=SourceRoleIndependenceSummary(direct_primary_support_count=1),
        ordering_policy_version="claim-ordering-v1",
        plan_hash="f" * 64,
    )


def _artifacts(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "stream_complete": True,
        "answer_text": "SUMMARY\nAAPL rose on record guidance. [claim:1] (e1)\nCAUSAL_EXPLANATION\nGuidance raised revenue.\nLIMITATIONS\nNone.",
        "emitted_citations": ("e1",),
        "emitted_claim_markers": ("claim:1",),
        "permitted_claim_ids": ("claim:1",),
        "permitted_evidence_ids": ("e1",),
        "required_sections": ("SUMMARY", "CAUSAL_EXPLANATION", "LIMITATIONS"),
        "required_limitations": (),
        "emitted_status": "SUFFICIENT",
        "emitted_attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "validated_status": "SUFFICIENT",
        "validated_attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "input_hash": "i" * 64,
        "plan_hash": "f" * 64,
        "evidence_state_hash": "e" * 64,
        "runtime_identity": "runtime:1",
        "output_hash": "o" * 64,
        "cancellation_requested": False,
        "timed_out": False,
        "input_tokens": 10,
        "output_tokens": 20,
        "completion_state": "completed",
    }
    base.update(overrides)
    return base


def test_structural_assurance_passes_valid_artifacts() -> None:
    checks = run_structural_assurance("run:1", _artifacts())
    assert [check.check_name for check in checks] == list(STRUCTURAL_CHECK_ORDER)
    assert all(check.status == "pass" for check in checks)


def test_incomplete_stream_fails() -> None:
    checks = run_structural_assurance("run:1", _artifacts(stream_complete=False))
    assert "stream_complete" in {
        check.check_name for check in checks if check.status == "fail"
    }


def test_unknown_citation_fails() -> None:
    checks = run_structural_assurance("run:1", _artifacts(emitted_citations=("e1", "ghost")))
    assert "citation_resolution" in {
        check.check_name for check in checks if check.status == "fail"
    }


def test_missing_required_section_or_limitation_fails() -> None:
    checks = run_structural_assurance(
        "run:1",
        _artifacts(answer_text="SUMMARY only", required_limitations=("Coverage gap required.",)),
    )
    failed = {check.check_name for check in checks if check.status == "fail"}
    assert "required_sections" in failed
    assert "required_limitations" in failed


def test_status_type_mismatch_fails() -> None:
    checks = run_structural_assurance(
        "run:1",
        _artifacts(emitted_status="SUFFICIENT", validated_status="PARTIAL"),
    )
    assert "status_type_alignment" in {
        check.check_name for check in checks if check.status == "fail"
    }


def test_hash_coherence_failure() -> None:
    checks = run_structural_assurance(
        "run:1",
        _artifacts(output_hash="different"),
    )
    assert "hash_coherence" in {
        check.check_name for check in checks if check.status == "fail"
    }


def test_metadata_inconsistency_fails() -> None:
    checks = run_structural_assurance(
        "run:1",
        _artifacts(cancellation_requested=True, completion_state="completed"),
    )
    assert "metadata_consistency" in {
        check.check_name for check in checks if check.status == "fail"
    }


def test_assurance_never_calls_model_and_never_rewrites() -> None:
    import inspect

    signature = inspect.signature(run_structural_assurance)
    assert "llm" not in signature.parameters
    checks = run_structural_assurance("run:1", _artifacts())
    assert all(check.status == "pass" for check in checks)


def test_thin_finalizer_persists_only_assured_envelope() -> None:
    sink = InMemoryDeltaSink()
    checks = run_structural_assurance("run:1", _artifacts())
    result = thin_finalizer(
        {"run_id": "run:1"},
        answer_text="assured answer",
        validated_plan=_validated_plan(),
        assurance_checks=checks,
        sink=sink,
    )
    assert result["assured"] is True
    envelope = sink.assured_envelope()
    assert envelope is not None
    assert envelope["final_status"] == "SUFFICIENT"


def test_thin_finalizer_refuses_non_assured_envelope() -> None:
    sink = InMemoryDeltaSink()
    failing = run_structural_assurance("run:1", _artifacts(stream_complete=False))
    with pytest.raises(Exception, match="ASSURANCE_FAILED"):
        thin_finalizer(
            {"run_id": "run:1"},
            answer_text="provisional",
            validated_plan=_validated_plan(),
            assurance_checks=failing,
            sink=sink,
        )
    assert sink.assured_envelope() is None


def test_provisional_rendering_label_constant() -> None:
    assert PROVISIONAL_RENDERING == "PROVISIONAL_RENDERING"
