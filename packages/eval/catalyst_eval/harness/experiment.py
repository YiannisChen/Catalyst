"""
Experiment comparator — runs multiple agent configurations against the same
golden set and produces a side-by-side ComparisonReport.

Usage:
    comparison = compare(
        configs={"baseline": agent_v1, "improved": agent_v2},
        golden_set=events,
        metrics=[AttributionF1(), CategoryAccuracy()],
    )
    print(comparison.to_markdown())
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult
from catalyst_eval.harness.runner import evaluate


@dataclass
class ComparisonReport:
    """
    Side-by-side evaluation results for multiple agent configurations.

    Attributes
    ----------
    results:
        Mapping of config_name -> {metric_name: averaged_score}.
    costs:
        Mapping of config_name -> {avg_cost_usd: float, avg_tokens: float}.
    """

    results: dict[str, dict[str, float]]
    costs: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_markdown(self) -> str:
        """Delegate to the reports.markdown renderer."""
        from catalyst_eval.reports.markdown import render_comparison

        return render_comparison(self)


def compare(
    configs: dict[str, Callable[[str, str], AttributionResult]],
    golden_set: list[GoldenEvent],
    metrics: list,
) -> ComparisonReport:
    """
    Evaluate each agent configuration in *configs* against *golden_set*.

    Parameters
    ----------
    configs:
        Mapping of config_name -> predict_fn, where predict_fn accepts
        (ticker: str, trade_date: str) and returns AttributionResult.
    golden_set:
        Shared list of GoldenEvent ground-truth records used for all configs.
    metrics:
        List of metric instances (BaseMetric protocol) applied to every config.

    Returns
    -------
    ComparisonReport with per-config scores and cost statistics.
    """
    results: dict[str, dict[str, float]] = {}
    costs: dict[str, dict[str, float]] = {}

    for config_name, predict_fn in configs.items():
        report = evaluate(predict_fn=predict_fn, golden_set=golden_set, metrics=metrics)
        results[config_name] = report.scores
        costs[config_name] = {
            "avg_cost_usd": report.avg_cost_usd,
            "avg_tokens": report.avg_tokens,
        }

    return ComparisonReport(results=results, costs=costs)
