"""M7-7: A1-A5 experiment seams + Stage-1 development signals.

A1/A2/A3/A5 arms change exactly one policy seam with all other fixed
controls equal; artifacts record policy versions. A4 is schema +
eligibility-manifest readiness only (multi-gap subset, predicate version,
minimum denominator, refusal reasons) and never introduces or executes a
max_actions_per_batch=2 arm in M7. Stage-1 outputs are development signals
only; a strong A3 efficacy claim requires Stage-2-or-later.
"""
from __future__ import annotations

import pytest

from catalyst_agents.runtime.experiment import (
    EVAL_CAPABILITY_TOKEN,
    ExperimentPolicyOverride,
)
from catalyst_eval.v1_1.ablation import (
    a1_arm,
    a2_arm,
    a3_arm,
    a5_arm,
    stage1_development_signals,
    validate_a4_readiness,
)

from tests.v1_1_fixtures import make_stage1_cases


def _base() -> ExperimentPolicyOverride:
    return ExperimentPolicyOverride(eval_capability_token=EVAL_CAPABILITY_TOKEN)


def test_a1_arm_changes_only_packing_seam():
    arm = a1_arm("critic_prefix_600chars_v1")
    base = _base()
    assert arm.a1_packing_policy_version == "critic_prefix_600chars_v1"
    assert arm.a2_hypothesis_policy_version == base.a2_hypothesis_policy_version
    assert arm.a3_max_corrective_rounds == base.a3_max_corrective_rounds
    assert arm.a5_observation_policy_version == base.a5_observation_policy_version


def test_a3_arm_changes_only_corrective_seam():
    arm = a3_arm(0)
    assert arm.a3_max_corrective_rounds == 0
    assert arm.a1_packing_policy_version is None
    assert arm.a2_hypothesis_policy_version is None
    assert arm.a5_observation_policy_version is None


def test_arms_carry_eval_capability_token():
    for arm in (a1_arm("evidence_context_pack_v1"), a2_arm("bounded_competition_v1"),
                a3_arm(1), a5_arm("move_profile_v1")):
        assert arm.eval_capability_token == EVAL_CAPABILITY_TOKEN


def test_a4_readiness_validates_multi_gap_subset_and_minimum():
    cases = [row["case_id"] for row in make_stage1_cases()]
    diagnostics = validate_a4_readiness(
        eligible_case_ids=("v1f-008",),
        eligibility_predicate_version="multi-gap-recoverable-v1",
        minimum_eligible_denominator=1,
        refusal_reason_codes=("insufficient_public_evidence",),
        dataset_case_ids=cases,
    )
    assert diagnostics == []


def test_a4_readiness_rejects_below_minimum_or_unknown_ids():
    cases = [row["case_id"] for row in make_stage1_cases()]
    diagnostics = validate_a4_readiness(
        eligible_case_ids=("v1f-008",),
        eligibility_predicate_version="multi-gap-recoverable-v1",
        minimum_eligible_denominator=8,
        refusal_reason_codes=("insufficient_public_evidence",),
        dataset_case_ids=cases,
    )
    assert diagnostics, "below-minimum A4 denominator must be reported"
    unknown = validate_a4_readiness(
        eligible_case_ids=("not-a-case",),
        eligibility_predicate_version="multi-gap-recoverable-v1",
        minimum_eligible_denominator=1,
        refusal_reason_codes=(),
        dataset_case_ids=cases,
    )
    assert unknown


def test_a4_readiness_has_no_actions_per_batch_arm():
    # M7 A4 is readiness-only: it must not expose or execute an executable
    # max_actions_per_batch=2 arm (only the prohibition is documented).
    import inspect

    from catalyst_eval.v1_1 import ablation

    source = inspect.getsource(ablation)
    assert "max_actions_per_batch =" not in source
    assert "max_actions_per_batch=2" not in source


def test_stage1_signals_are_development_only():
    signals = stage1_development_signals(
        arm_results={"A1": {"value": 0.8}, "A2": {"value": 0.7},
                     "A3": {"value": 0.6}, "A5": {"value": 0.9}},
    )
    assert signals["development_signal_only"] is True
    assert signals["strong_efficacy_claim"] is False
    assert signals["arms"] == ["A1", "A2", "A3", "A5"]
