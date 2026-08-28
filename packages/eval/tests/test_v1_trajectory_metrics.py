"""M7-6: Stage-1 trajectory metrics.

Corrective trigger precision/recall, gap/action match, stop correctness,
useful/unnecessary corrective rates (denominator = not-required cases),
zero-new-structure rate, and paired attribution delta (no-corrective vs
one-action on the A3 eligible subset). Below-minimum eligible denominators
report diagnostics only and can never satisfy retention/promotion.
"""
from __future__ import annotations

import pytest

from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.manifest import EligibleExperiment
from catalyst_eval.v1_1.trajectory_metrics import (
    RunTrajectoryFacts,
    compute_trajectory_metrics,
    below_minimum_denominator,
)

from tests.v1_1_fixtures import make_case, make_stage1_cases


def _gold_rows() -> list[GoldenCase]:
    return [GoldenCase.model_validate(row) for row in make_stage1_cases()]


def _facts(
    gold: GoldenCase,
    *,
    corrective_triggered: bool = False,
    gap_ids: tuple[str, ...] = (),
    actions: tuple[str, ...] = (),
    stop_correct: bool = True,
    new_structure_created: bool = False,
    corrected: bool = False,
) -> RunTrajectoryFacts:
    return RunTrajectoryFacts(
        case_id=gold.case_id,
        corrective_triggered=corrective_triggered,
        gap_ids=gap_ids,
        corrective_actions=actions,
        stop_correct=stop_correct,
        new_structure_created=new_structure_created,
        corrected=corrected,
    )


def test_trigger_recall_over_required_cases():
    gold_cases = _gold_rows()
    required = [g for g in gold_cases if g.expected_research_behavior.corrective_required]
    assert required, "fixture must include a required corrective case"
    facts = [_facts(g, corrective_triggered=True, stop_correct=True) for g in gold_cases]
    metrics = compute_trajectory_metrics(facts, gold_cases)
    assert metrics.corrective_trigger_recall.denominator == len(required)
    assert metrics.corrective_trigger_recall.numerator == len(required)
    assert metrics.corrective_trigger_recall.value == pytest.approx(1.0)


def test_unnecessary_corrective_rate_uses_not_required_denominator():
    gold_cases = _gold_rows()
    not_required = [
        g for g in gold_cases if not g.expected_research_behavior.corrective_required
    ]
    facts = []
    flagged = False
    for gold in gold_cases:
        triggered = (
            not gold.expected_research_behavior.corrective_required
            and not flagged
        )
        if triggered:
            flagged = True
        facts.append(
            _facts(gold, corrective_triggered=triggered, stop_correct=not triggered)
        )
    metrics = compute_trajectory_metrics(facts, gold_cases)
    assert metrics.unnecessary_corrective_rate.denominator == len(not_required)
    assert metrics.unnecessary_corrective_rate.numerator == 1


def test_gap_action_match_and_stop_correctness():
    gold_cases = _gold_rows()
    facts = [_facts(g, stop_correct=True) for g in gold_cases]
    metrics = compute_trajectory_metrics(facts, gold_cases)
    assert metrics.stop_correctness.denominator == len(gold_cases)
    assert metrics.stop_correctness.value == pytest.approx(1.0)
    assert metrics.gap_action_match.denominator == len(gold_cases)


def test_zero_new_structure_rate():
    gold_cases = _gold_rows()
    facts = [_facts(g) for g in gold_cases]
    metrics = compute_trajectory_metrics(facts, gold_cases)
    assert metrics.zero_new_structure_rate.value == pytest.approx(1.0)


def test_paired_attribution_delta_is_diagnostic_only():
    no_corrective = [0.4, 0.6, 0.8]
    one_action = [0.5, 0.7, 0.9]
    metrics = compute_trajectory_metrics(
        [_facts(g) for g in _gold_rows()], _gold_rows()
    )
    delta = metrics.paired_attribution_delta(no_corrective, one_action)
    assert delta == pytest.approx(0.1)


def test_below_minimum_denominator_blocks_promotion():
    experiment = EligibleExperiment(
        experiment_id="A3",
        eligibility_predicate="human-labelled-recoverable",
        ordered_eligible_ids=("v1f-008",),
        eligibility_hash="a" * 64,
        minimum_eligible_denominator=8,
        observed_denominator=None,
    )
    assert below_minimum_denominator(experiment, ("v1f-008",)) is True
    assert below_minimum_denominator(experiment, tuple(experiment.ordered_eligible_ids * 8)) is False


def test_useful_corrective_rate_uses_required_and_recoverable_denominator():
    """Batch-B amendment: useful-corrective rate denominator is the
    predeclared human-labelled corrective_required=true AND
    corrective_recoverable=true eligible subset, not not-required cases."""
    gold_cases = _gold_rows()
    eligible = [
        g
        for g in gold_cases
        if g.expected_research_behavior.corrective_required
        and g.expected_research_behavior.corrective_recoverable
    ]
    assert eligible, "fixture must include a required+recoverable corrective case"
    # Every eligible corrective fires and corrects -> useful for all eligible.
    facts = [
        _facts(
            g,
            corrective_triggered=g in eligible,
            corrected=g in eligible,
        )
        for g in gold_cases
    ]
    metrics = compute_trajectory_metrics(facts, gold_cases)
    assert metrics.useful_corrective_rate.denominator == len(eligible)
    assert metrics.useful_corrective_rate.numerator == len(eligible)
    assert metrics.useful_corrective_rate.value == pytest.approx(1.0)
    # A corrective that fires on the eligible subset but does not correct is
    # NOT useful and must reduce the numerator, not the denominator.
    facts_bad = [
        _facts(
            g,
            corrective_triggered=g in eligible,
            corrected=False,
        )
        for g in gold_cases
    ]
    metrics_bad = compute_trajectory_metrics(facts_bad, gold_cases)
    assert metrics_bad.useful_corrective_rate.denominator == len(eligible)
    assert metrics_bad.useful_corrective_rate.numerator == 0
