"""Stage-1 trajectory metrics (M7-6).

Trajectory ground truth is ``GoldenCase.expected_research_behavior``;
execution facts come from RunManifest, research-task artifacts, corrective
artifacts, and trace events. Corrective trigger precision/recall, gap/action
match, stop correctness, useful-corrective rate (denominator = predeclared
human-labelled corrective_required=true AND corrective_recoverable=true eligible
subset), unnecessary-corrective rate (denominator = not-required cases),
zero-new-structure rate, and paired attribution delta
(no-corrective vs one-action) on the A3 eligible subset. A3/A4 subsets are
bound with minimum eligible denominators before outcomes; below-minimum runs
report diagnostics only and cannot satisfy retention/promotion.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.manifest import EligibleExperiment
from catalyst_eval.v1_1.retrieval_metrics import MetricResult


@dataclass(frozen=True)
class RunTrajectoryFacts:
    case_id: str
    corrective_triggered: bool
    gap_ids: tuple[str, ...] = ()
    corrective_actions: tuple[str, ...] = ()
    stop_correct: bool = True
    new_structure_created: bool = False
    corrected: bool = False


@dataclass(frozen=True)
class TrajectoryMetrics:
    corrective_trigger_precision: MetricResult
    corrective_trigger_recall: MetricResult
    gap_action_match: MetricResult
    stop_correctness: MetricResult
    useful_corrective_rate: MetricResult
    unnecessary_corrective_rate: MetricResult
    zero_new_structure_rate: MetricResult

    def paired_attribution_delta(
        self, no_corrective_scores: Sequence[float], one_action_scores: Sequence[float]
    ) -> float | None:
        """A3 paired delta: mean(one-action) - mean(no-corrective).

        Development signal only; a strong A3 efficacy claim requires
        Stage-2-or-later denominators.
        """
        if len(no_corrective_scores) != len(one_action_scores) or not no_corrective_scores:
            return None
        return sum(one_action_scores) / len(one_action_scores) - sum(
            no_corrective_scores
        ) / len(no_corrective_scores)

    def as_dict(self) -> dict:
        return {
            name: {
                "metric_id": getattr(self, name).metric_id,
                "numerator": getattr(self, name).numerator,
                "denominator": getattr(self, name).denominator,
                "eligible_count": getattr(self, name).eligible_count,
                "excluded_count": getattr(self, name).excluded_count,
                "non_scorable_count": getattr(self, name).non_scorable_count,
                "value": getattr(self, name).value,
                "case_ids": list(getattr(self, name).case_ids),
                "hard_gate": getattr(self, name).hard_gate,
                "gate_passed": getattr(self, name).gate_passed,
            }
            for name in (
                "corrective_trigger_precision", "corrective_trigger_recall",
                "gap_action_match", "stop_correctness", "useful_corrective_rate",
                "unnecessary_corrective_rate", "zero_new_structure_rate",
            )
        }


def below_minimum_denominator(
    experiment: EligibleExperiment, observed_case_ids: Sequence[str]
) -> bool:
    """True when the observed eligible count is below the predeclared minimum.

    Below-minimum runs are diagnostics only and cannot satisfy retention or
    promotion (A3/A4 lock).
    """
    eligible = set(experiment.ordered_eligible_ids)
    observed_count = sum(1 for case_id in observed_case_ids if case_id in eligible)
    return observed_count < experiment.minimum_eligible_denominator


def _metric(
    metric_id: str,
    *,
    numerator: int,
    denominator: int,
    eligible: int,
    non_scorable: int,
    value: float | None,
    case_ids: Sequence[str],
) -> MetricResult:
    return MetricResult(
        metric_id=metric_id,
        numerator=numerator,
        denominator=denominator,
        eligible_count=eligible,
        excluded_count=0,
        non_scorable_count=non_scorable,
        value=value,
        case_ids=tuple(case_ids),
        pool_ids=(),
        hard_gate=False,
        gate_passed=None,
    )


def compute_trajectory_metrics(
    facts: Sequence[RunTrajectoryFacts],
    gold_cases: Sequence[GoldenCase],
) -> TrajectoryMetrics:
    """Compute Stage-1 trajectory metrics over fixture/execution facts."""
    gold_by_case = {case.case_id: case for case in gold_cases}
    facts_by_case = {fact.case_id: fact for fact in facts}
    if set(gold_by_case) != set(facts_by_case):
        raise ValueError("trajectory facts must cover the gold cases one-to-one")

    required_cases = [
        case_id
        for case_id, gold in gold_by_case.items()
        if gold.expected_research_behavior.corrective_required
    ]
    not_required_cases = [
        case_id
        for case_id, gold in gold_by_case.items()
        if not gold.expected_research_behavior.corrective_required
    ]
    # Batch-B amendment: the useful-corrective rate denominator is the
    # predeclared human-labelled corrective_required=true AND
    # corrective_recoverable=true eligible subset, not the not-required cases.
    useful_eligible_ids = {
        case_id
        for case_id, gold in gold_by_case.items()
        if gold.expected_research_behavior.corrective_required
        and gold.expected_research_behavior.corrective_recoverable
    }

    trigger_required = 0
    trigger_triggered = 0
    trigger_triggered_correct = 0
    trigger_triggered_cases: list[str] = []
    gap_match = 0
    stop_ok = 0
    useful = 0
    unnecessary = 0
    zero_new_structure = 0

    for case_id, gold in gold_by_case.items():
        fact = facts_by_case[case_id]
        behavior = gold.expected_research_behavior
        if behavior.corrective_required:
            trigger_required += 1
            if fact.corrective_triggered:
                trigger_triggered_correct += 1
        if fact.corrective_triggered:
            trigger_triggered += 1
            trigger_triggered_cases.append(case_id)
        # Useful corrective: fired AND corrected on the predeclared
        # required+recoverable eligible subset.
        if case_id in useful_eligible_ids:
            if fact.corrective_triggered and fact.corrected:
                useful += 1
        elif fact.corrective_triggered and not fact.corrected:
            # A corrective on a not-required case that did not improve the
            # outcome is unnecessary (denominator = not-required cases).
            unnecessary += 1
        # Gap/action match: expected gap codes and acceptable corrective
        # actions align with the observed facts for required cases.
        expected_gaps = set(behavior.expected_gap_reason_codes)
        if (
            (not expected_gaps or expected_gaps & set(fact.gap_ids))
            and (not fact.corrective_triggered or fact.corrective_actions)
        ):
            gap_match += 1
        if fact.stop_correct:
            stop_ok += 1
        if not fact.new_structure_created:
            zero_new_structure += 1

    total = len(gold_cases)
    precision_value = (
        trigger_triggered_correct / trigger_triggered if trigger_triggered else None
    )
    recall_value = trigger_triggered_correct / trigger_required if trigger_required else None
    gap_value = gap_match / total
    stop_value = stop_ok / total
    useful_value = useful / len(useful_eligible_ids) if useful_eligible_ids else None
    unnecessary_value = (
        unnecessary / len(not_required_cases) if not_required_cases else None
    )
    zero_value = zero_new_structure / total

    return TrajectoryMetrics(
        corrective_trigger_precision=_metric(
            "corrective_trigger_precision", numerator=trigger_triggered_correct,
            denominator=trigger_triggered, eligible=trigger_triggered,
            non_scorable=total - trigger_triggered, value=precision_value,
            case_ids=trigger_triggered_cases,
        ),
        corrective_trigger_recall=_metric(
            "corrective_trigger_recall", numerator=trigger_triggered_correct,
            denominator=trigger_required, eligible=trigger_required,
            non_scorable=total - trigger_required, value=recall_value,
            case_ids=required_cases,
        ),
        gap_action_match=_metric(
            "gap_action_match", numerator=gap_match, denominator=total,
            eligible=total, non_scorable=0, value=gap_value,
            case_ids=list(gold_by_case),
        ),
        stop_correctness=_metric(
            "stop_correctness", numerator=stop_ok, denominator=total,
            eligible=total, non_scorable=0, value=stop_value,
            case_ids=list(gold_by_case),
        ),
        useful_corrective_rate=_metric(
            "useful_corrective_rate", numerator=useful,
            denominator=len(useful_eligible_ids), eligible=len(useful_eligible_ids),
            non_scorable=total - len(useful_eligible_ids), value=useful_value,
            case_ids=sorted(useful_eligible_ids),
        ),
        unnecessary_corrective_rate=_metric(
            "unnecessary_corrective_rate", numerator=unnecessary,
            denominator=len(not_required_cases), eligible=len(not_required_cases),
            non_scorable=total - len(not_required_cases), value=unnecessary_value,
            case_ids=not_required_cases,
        ),
        zero_new_structure_rate=_metric(
            "zero_new_structure_rate", numerator=zero_new_structure,
            denominator=total, eligible=total, non_scorable=0, value=zero_value,
            case_ids=list(gold_by_case),
        ),
    )


__all__ = [
    "RunTrajectoryFacts",
    "TrajectoryMetrics",
    "below_minimum_denominator",
    "compute_trajectory_metrics",
]
