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


def _zero_recoverable_gold_rows() -> list[GoldenCase]:
    """The Q-011 Option B shape: no corrective_required/recoverable case."""
    rows = make_stage1_cases()
    for row in rows:
        behavior = row["expected_research_behavior"]
        behavior["corrective_required"] = False
        behavior["corrective_recoverable"] = False
        behavior["expected_gap_reason_codes"] = []
        behavior["acceptable_corrective_actions"] = []
    return [GoldenCase.model_validate(row) for row in rows]


def _facts(
    gold: GoldenCase,
    *,
    corrective_triggered: bool = False,
    gap_ids: tuple[str, ...] = (),
    actions: tuple[str, ...] = (),
    rounds_executed: int | None = None,
    produced_structure: bool = False,
) -> RunTrajectoryFacts:
    """Observed runtime facts only; stop/usefulness/recovery are derived by the
    metric against the predeclared ExpectedResearchBehavior."""
    return RunTrajectoryFacts(
        case_id=gold.case_id,
        corrective_triggered=corrective_triggered,
        gap_ids=gap_ids,
        corrective_actions=actions,
        rounds_executed=(
            (1 if corrective_triggered else 0)
            if rounds_executed is None
            else rounds_executed
        ),
        produced_structure=produced_structure,
    )


def test_trigger_recall_over_required_cases():
    gold_cases = _gold_rows()
    required = [g for g in gold_cases if g.expected_research_behavior.corrective_required]
    assert required, "fixture must include a required corrective case"
    facts = [
        _facts(
            g,
            corrective_triggered=True,
            gap_ids=tuple(g.expected_research_behavior.expected_gap_reason_codes),
        )
        for g in gold_cases
    ]
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
        facts.append(_facts(gold, corrective_triggered=triggered))
    metrics = compute_trajectory_metrics(facts, gold_cases)
    assert metrics.unnecessary_corrective_rate.denominator == len(not_required)
    assert metrics.unnecessary_corrective_rate.numerator == 1


def test_gap_action_match_and_stop_correctness():
    gold_cases = _gold_rows()
    facts = [_facts(g) for g in gold_cases]
    metrics = compute_trajectory_metrics(facts, gold_cases)
    required_count = sum(
        1 for g in gold_cases if g.expected_research_behavior.corrective_required
    )
    assert metrics.stop_correctness.denominator == len(gold_cases)
    # Derived: with no corrective round on any case, exactly the required cases
    # stopped incorrectly.
    assert metrics.stop_correctness.numerator == len(gold_cases) - required_count
    assert metrics.stop_correctness.value == pytest.approx(
        (len(gold_cases) - required_count) / len(gold_cases)
    )
    assert metrics.gap_action_match.denominator == len(gold_cases)


def test_stop_correctness_is_derived_from_the_predeclared_requirement():
    """A required corrective case that never triggered is an incorrect stop,
    even though the runtime reported no self-judgement."""
    gold_cases = _gold_rows()
    required = next(
        g for g in gold_cases if g.expected_research_behavior.corrective_required
    )
    facts = [
        _facts(g, corrective_triggered=(g.case_id != required.case_id))
        for g in gold_cases
    ]
    metrics = compute_trajectory_metrics(facts, gold_cases)
    assert metrics.stop_correctness.numerator == len(gold_cases) - 1
    assert metrics.stop_correctness.value < 1.0


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


def test_recovery_is_derived_from_expected_gap_coverage():
    """Recovery is an eval judgement: the round must address at least one
    predeclared expected gap reason code."""
    gold_cases = _gold_rows()
    recoverable = next(
        g
        for g in gold_cases
        if g.expected_research_behavior.corrective_required
        and g.expected_research_behavior.corrective_recoverable
    )
    expected = tuple(
        recoverable.expected_research_behavior.expected_gap_reason_codes
    )
    assert expected, "fixture must predeclare gap reason codes for a recoverable case"

    matching = compute_trajectory_metrics(
        [
            _facts(
                g,
                corrective_triggered=True,
                gap_ids=expected if g.case_id == recoverable.case_id else (),
                actions=("a1",),
            )
            for g in gold_cases
        ],
        gold_cases,
    )
    assert matching.useful_corrective_rate.value == pytest.approx(1.0)

    unrelated = compute_trajectory_metrics(
        [
            _facts(
                g,
                corrective_triggered=True,
                gap_ids=("SOMETHING_ELSE",) if g.case_id == recoverable.case_id else (),
                actions=("a1",),
            )
            for g in gold_cases
        ],
        gold_cases,
    )
    assert unrelated.useful_corrective_rate.value == pytest.approx(0.0)


def test_zero_required_or_recoverable_cases_yield_null_corrective_metrics():
    """Q-011 Option B (2026-09-11): with no corrective_required/recoverable
    case, corrective trigger recall and useful-corrective rate have denominator
    0 and value None, and neither is a hard gate."""
    gold_cases = _zero_recoverable_gold_rows()
    facts = [_facts(g) for g in gold_cases]
    metrics = compute_trajectory_metrics(facts, gold_cases)
    assert metrics.corrective_trigger_recall.denominator == 0
    assert metrics.corrective_trigger_recall.value is None
    assert metrics.corrective_trigger_recall.hard_gate is False
    assert metrics.useful_corrective_rate.denominator == 0
    assert metrics.useful_corrective_rate.value is None
    assert metrics.useful_corrective_rate.hard_gate is False
    # Non-corrective trajectory metrics still compute over the whole set.
    assert metrics.gap_action_match.denominator == len(gold_cases)
    assert metrics.stop_correctness.value == pytest.approx(1.0)


def test_empty_a3_a4_eligible_subset_is_readiness_only():
    """Empty A3/A4 eligible subsets are N/A/readiness-only: below-minimum
    (blocked from retention/promotion) and never a zero-efficacy claim."""
    a3 = EligibleExperiment(
        experiment_id="A3",
        eligibility_predicate="human-labelled-recoverable",
        ordered_eligible_ids=(),
        eligibility_hash="a" * 64,
        minimum_eligible_denominator=1,
        observed_denominator=0,
    )
    a4 = EligibleExperiment(
        experiment_id="A4",
        eligibility_predicate="multi-gap-recoverable-v1",
        ordered_eligible_ids=(),
        eligibility_hash="b" * 64,
        minimum_eligible_denominator=1,
        observed_denominator=0,
    )
    assert below_minimum_denominator(a3, ()) is True
    assert below_minimum_denominator(a4, ()) is True
    assert a3.ordered_eligible_ids == () and a4.ordered_eligible_ids == ()


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
            gap_ids=(
                tuple(g.expected_research_behavior.expected_gap_reason_codes)
                if g in eligible
                else ()
            ),
        )
        for g in gold_cases
    ]
    metrics = compute_trajectory_metrics(facts, gold_cases)
    assert metrics.useful_corrective_rate.denominator == len(eligible)
    assert metrics.useful_corrective_rate.numerator == len(eligible)
    assert metrics.useful_corrective_rate.value == pytest.approx(1.0)
    # A corrective that fires on the eligible subset but addresses none of the
    # predeclared expected gaps is NOT useful: the numerator falls, the
    # denominator does not.
    facts_bad = [
        _facts(
            g,
            corrective_triggered=g in eligible,
            gap_ids=("UNRELATED_GAP",) if g in eligible else (),
        )
        for g in gold_cases
    ]
    metrics_bad = compute_trajectory_metrics(facts_bad, gold_cases)
    assert metrics_bad.useful_corrective_rate.denominator == len(eligible)
    assert metrics_bad.useful_corrective_rate.numerator == 0
