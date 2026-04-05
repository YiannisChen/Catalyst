"""
Harness runner for evaluating an agent predict function against a golden set.

Usage:
    report = evaluate(predict_fn=my_agent, golden_set=events, metrics=[AttributionF1()])
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult


@dataclass
class EvalReport:
    """Aggregated result of evaluating one agent over a golden set."""

    scores: dict[str, float]       # metric_name -> averaged score across all events
    per_event: list[dict]          # per-event breakdown: {event_id, ticker, <metric>: score, ...}
    avg_cost_usd: float            # mean total_cost_usd across events
    avg_tokens: float              # mean total_tokens across events


def evaluate(
    predict_fn: Callable[[str, str], AttributionResult],
    golden_set: list[GoldenEvent],
    metrics: list,
) -> EvalReport:
    """
    Evaluate *predict_fn* over *golden_set* using the provided *metrics*.

    Parameters
    ----------
    predict_fn:
        Callable that accepts (ticker: str, trade_date: str) and returns
        an AttributionResult.
    golden_set:
        List of GoldenEvent ground-truth records.
    metrics:
        List of metric instances that implement BaseMetric protocol
        (name: str, compute(predicted, golden) -> float).

    Returns
    -------
    EvalReport with averaged scores, per-event breakdown, and cost/token stats.
    """
    if not golden_set:
        # Return zeroed report without calling predict_fn.
        zero_scores = {m.name: 0.0 for m in metrics}
        return EvalReport(
            scores=zero_scores,
            per_event=[],
            avg_cost_usd=0.0,
            avg_tokens=0.0,
        )

    accumulated: dict[str, list[float]] = {m.name: [] for m in metrics}
    total_cost: list[float] = []
    total_tokens_list: list[float] = []
    per_event: list[dict] = []

    for event in golden_set:
        prediction: AttributionResult = predict_fn(event.ticker, event.trade_date)

        event_record: dict = {
            "event_id": event.id,
            "ticker": event.ticker,
            "trade_date": event.trade_date,
        }

        for metric in metrics:
            score = metric.compute(prediction, event)
            accumulated[metric.name].append(score)
            event_record[metric.name] = score

        total_cost.append(prediction.total_cost_usd)
        total_tokens_list.append(float(prediction.total_tokens))
        per_event.append(event_record)

    averaged_scores = {
        name: sum(values) / len(values)
        for name, values in accumulated.items()
    }

    return EvalReport(
        scores=averaged_scores,
        per_event=per_event,
        avg_cost_usd=sum(total_cost) / len(total_cost),
        avg_tokens=sum(total_tokens_list) / len(total_tokens_list),
    )
