"""C1b (Frozen-A3 §8): the EvidenceDecision relation mirror is code-derived.

The hypothesis-side ledger (``CandidateHypothesis.supporting_evidence_ids`` /
``contradicting_evidence_ids``) is canonical; CLAIM-01 binds to it
(``claim_validation.py:194-212``). The ``EvidenceDecision`` side
(``supports_hypothesis_refs`` / ``contradicts_hypothesis_refs``) is a *mirror*
derived in code from the raw JSON before schema validation.

Rules under test:

* the derived refs equal the hypothesis-side ledger;
* a supplied mirror that differs from the derived refs fails;
* an empty hypothesis-side ledger with a filled mirror fails;
* the two sides are never unioned;
* the same (evidence, hypothesis) pair as support and contradiction still fails;
* ``note`` accepts 1,999 characters and rejects 2,001;
* the relation-mirror fields are gone from the model-facing schema/contract;
* a persisted packing A/B arm-B mirror-failure payload now validates iff its
  hypothesis-side ledger is internally consistent.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    CandidateHypothesis,
    EvidenceDecision,
    normalize_decision_relation_mirror,
)
from pydantic import ValidationError


def _hypothesis(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "hypothesis_ref": "h1",
        "cause_type": "COMPANY_SPECIFIC_CATALYST",
        "statement": "AAPL rose on record guidance.",
        "mechanism": None,
        "supporting_evidence_ids": ("corpus:chunk:0001",),
        "contradicting_evidence_ids": (),
        "magnitude_fit": "STRONG",
        "proposed_role": "PRIMARY",
        "unresolved_gap_refs": (),
    }
    base.update(overrides)
    return base


def _raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "evidence_decisions": (
            {
                "evidence_id": "corpus:chunk:0001",
                "disposition": "SUPPORT",
                "reason_code": "material_support",
            },
        ),
        "candidate_hypotheses": (_hypothesis(),),
        "conflicts": (),
        "proposed_missing_evidence": (),
        "research_decision": "READY",
        "recommended_status": "SUFFICIENT",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "proposed_corrective_intents": (),
    }
    base.update(overrides)
    return base


# --- the derived mirror -----------------------------------------------------


def test_derived_refs_equal_the_hypothesis_lists() -> None:
    decision = AnalystDecision.model_validate(_raw())
    item = decision.evidence_decisions[0]
    assert item.supports_hypothesis_refs == ("h1",)
    assert item.contradicts_hypothesis_refs == ()


def test_derived_refs_follow_hypothesis_order_and_dedup() -> None:
    raw = _raw(
        candidate_hypotheses=(
            _hypothesis(hypothesis_ref="h1"),
            _hypothesis(
                hypothesis_ref="h2",
                proposed_role="SECONDARY",
                supporting_evidence_ids=("corpus:chunk:0001",),
                contradicting_evidence_ids=("corpus:chunk:0002",),
            ),
        ),
        evidence_decisions=(
            {"evidence_id": "corpus:chunk:0001", "disposition": "SUPPORT", "reason_code": "r"},
            {"evidence_id": "corpus:chunk:0002", "disposition": "CONTRADICT", "reason_code": "r"},
        ),
    )
    decision = AnalystDecision.model_validate(raw)
    by_id = {item.evidence_id: item for item in decision.evidence_decisions}
    assert by_id["corpus:chunk:0001"].supports_hypothesis_refs == ("h1", "h2")
    assert by_id["corpus:chunk:0002"].contradicts_hypothesis_refs == ("h2",)


def test_equal_supplied_mirror_is_accepted() -> None:
    raw = _raw(
        evidence_decisions=(
            {
                "evidence_id": "corpus:chunk:0001",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "contradicts_hypothesis_refs": (),
                "reason_code": "material_support",
            },
        ),
    )
    decision = AnalystDecision.model_validate(raw)
    assert decision.evidence_decisions[0].supports_hypothesis_refs == ("h1",)


# --- the mirror may never contradict or extend the ledger -------------------


def test_conflicting_supplied_mirror_fails() -> None:
    raw = _raw(
        evidence_decisions=(
            {
                "evidence_id": "corpus:chunk:0001",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h-other",),
                "reason_code": "material_support",
            },
        ),
    )
    with pytest.raises(ValidationError):
        AnalystDecision.model_validate(raw)


def test_conflicting_contradiction_mirror_fails() -> None:
    raw = _raw(
        evidence_decisions=(
            {
                "evidence_id": "corpus:chunk:0001",
                "disposition": "SUPPORT",
                "contradicts_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
        ),
    )
    with pytest.raises(ValidationError):
        AnalystDecision.model_validate(raw)


def test_empty_hypothesis_lists_with_filled_mirror_fails() -> None:
    raw = _raw(
        candidate_hypotheses=(_hypothesis(supporting_evidence_ids=()),),
        evidence_decisions=(
            {
                "evidence_id": "corpus:chunk:0001",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
        ),
    )
    with pytest.raises(ValidationError):
        AnalystDecision.model_validate(raw)


def test_decision_side_refs_are_never_unioned_with_the_ledger() -> None:
    # The ledger binds e1/gap evidence to h1; a supplied decision-side ref for a
    # second hypothesis must not be added to the derived set.
    raw = _raw(
        candidate_hypotheses=(
            _hypothesis(),
            _hypothesis(hypothesis_ref="h2", proposed_role="SECONDARY", supporting_evidence_ids=()),
        ),
        evidence_decisions=(
            {
                "evidence_id": "corpus:chunk:0001",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1", "h2"),
                "reason_code": "material_support",
            },
        ),
    )
    with pytest.raises(ValidationError):
        AnalystDecision.model_validate(raw)


def test_normalizer_is_pure_and_returns_derived_refs() -> None:
    raw = _raw()
    normalized = normalize_decision_relation_mirror(raw)
    assert normalized["evidence_decisions"][0]["supports_hypothesis_refs"] == ["h1"]
    assert normalized["evidence_decisions"][0]["contradicts_hypothesis_refs"] == []
    # input is not mutated
    assert "supports_hypothesis_refs" not in raw["evidence_decisions"][0]


# --- existing structural validators stay ------------------------------------


def test_same_pair_support_and_contradiction_still_fails() -> None:
    # hypothesis side
    with pytest.raises(ValidationError):
        CandidateHypothesis(
            **_hypothesis(
                supporting_evidence_ids=("corpus:chunk:0001",),
                contradicting_evidence_ids=("corpus:chunk:0001",),
            )
        )
    # decision side structural validator (kept)
    with pytest.raises(ValidationError):
        EvidenceDecision(
            evidence_id="e1",
            disposition="SUPPORT",
            supports_hypothesis_refs=("h1",),
            contradicts_hypothesis_refs=("h1",),
            reason_code="r1",
        )


# --- note bound (C1a) -------------------------------------------------------


def test_note_length_1999_accepted_2001_rejected() -> None:
    decision = AnalystDecision.model_validate(
        _raw(
            evidence_decisions=(
                {
                    "evidence_id": "corpus:chunk:0001",
                    "disposition": "SUPPORT",
                    "reason_code": "material_support",
                    "note": "y" * 1999,
                },
            )
        )
    )
    assert len(decision.evidence_decisions[0].note or "") == 1999
    with pytest.raises(ValidationError):
        AnalystDecision.model_validate(
            _raw(
                evidence_decisions=(
                    {
                        "evidence_id": "corpus:chunk:0001",
                        "disposition": "SUPPORT",
                        "reason_code": "material_support",
                        "note": "y" * 2001,
                    },
                )
            )
        )


# --- model-facing surface excludes the derived mirror -----------------------


def test_model_facing_schema_omits_the_derived_relation_fields() -> None:
    fields = {
        "supports_hypothesis_refs",
        "contradicts_hypothesis_refs",
    }
    evidence_schema = EvidenceDecision.model_json_schema()
    assert fields.isdisjoint(evidence_schema["properties"])
    whole = json.dumps(AnalystDecision.model_json_schema())
    for name in fields:
        assert name not in whole
    # ...but the model still carries them for the downstream claim path.
    assert fields <= set(EvidenceDecision.model_fields)


def test_json_mode_field_contract_omits_the_derived_relation_fields() -> None:
    from catalyst_agents.nodes.evidence_analyst import _json_mode_field_contract

    text = _json_mode_field_contract(AnalystDecision)
    assert "supporting_evidence_ids" in text
    assert "contradicting_evidence_ids" in text
    assert "supports_hypothesis_refs" not in text
    assert "contradicts_hypothesis_refs" not in text


# --- persisted packing A/B arm-B failure payload ----------------------------

# Minimal reconstruction of the relationship-mirror failure recorded by the
# packing A/B arm B (`evidence_body_v1#run1`/`run2`, case c05):
#
#   ModelSchemaFailure("unparseable structured output JSON: 1 validation error
#   for AnalystDecision Value error, candidate support relation is inconsistent
#   with the EvidenceDecision for
#   '3d911ebd1df9f8b0a2253f00804f771aedbfd733b22df61e94c42e83129f2d3d:filing_v3'")
#
# The model listed the evidence id on the hypothesis but omitted the
# decision-side mirror. The raw payload itself was not persisted (only the
# error string was), so the minimal JSON below reproduces the recorded shape.
_ARM_B_MIRROR_EVIDENCE_ID = (
    "3d911ebd1df9f8b0a2253f00804f771aedbfd733b22df61e94c42e83129f2d3d:"
    "filing_v3"
)
_ARM_B_MIRROR_INCONSISTENT_DECISION: dict[str, Any] = {
    "schema_version": "1.0",
    "evidence_decisions": (
        {
            "evidence_id": _ARM_B_MIRROR_EVIDENCE_ID,
            "disposition": "SUPPORT",
            "reason_code": "material_support",
            # NOTE: no supports_hypothesis_refs — this is the recorded failure.
        },
    ),
    "candidate_hypotheses": (
        {
            "hypothesis_ref": "h1",
            "cause_type": "COMPANY_SPECIFIC_CATALYST",
            "statement": "A company-specific catalyst drove the move.",
            "supporting_evidence_ids": (_ARM_B_MIRROR_EVIDENCE_ID,),
            "magnitude_fit": "STRONG",
            "proposed_role": "PRIMARY",
        },
    ),
    "conflicts": (),
    "proposed_missing_evidence": (),
    "research_decision": "READY",
    "recommended_status": "SUFFICIENT",
    "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
    "proposed_corrective_intents": (),
}


def test_arm_b_mirror_failure_payload_now_validates() -> None:
    decision = AnalystDecision.model_validate(_ARM_B_MIRROR_INCONSISTENT_DECISION)
    item = decision.evidence_decisions[0]
    assert item.evidence_id == _ARM_B_MIRROR_EVIDENCE_ID
    assert item.supports_hypothesis_refs == ("h1",)
    assert item.contradicts_hypothesis_refs == ()


def test_arm_b_payload_still_fails_when_the_ledger_is_inconsistent() -> None:
    # Hypothesis-side ledger references an evidence id with no EvidenceDecision:
    # the derived mirror cannot paper over a broken ledger.
    broken = dict(_ARM_B_MIRROR_INCONSISTENT_DECISION)
    broken["evidence_decisions"] = ()
    with pytest.raises(ValidationError):
        AnalystDecision.model_validate(broken)
