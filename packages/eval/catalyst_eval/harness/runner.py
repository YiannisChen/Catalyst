"""
Harness runner for evaluating an agent predict function against a golden set.

Supports both float-returning metrics (CategoryAccuracy, ConfidenceCalibration)
and dict-returning metrics (CauseMatch, CitationFaithfulness, RefusalCorrectness).
Dict metrics are flattened: metric_name + "_" + key → value.
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
    per_event: list[dict]          # per-event breakdown
    avg_cost_usd: float
    avg_tokens: float


def evaluate(
    predict_fn: Callable[[str, str], AttributionResult],
    golden_set: list[GoldenEvent],
    metrics: list,
) -> EvalReport:
    """Evaluate *predict_fn* over *golden_set* using the provided *metrics*.

    Metrics may return float (simple) or dict (compound). Dict results are
    flattened: metric.name + "_" + key → value for each numeric leaf.
    Skipped events (dict with "skipped": True) are excluded from averages.
    """
    if not golden_set:
        zero_scores: dict[str, float] = {}
        for m in metrics:
            zero_scores.update(_metric_keys_zero(m))
        return EvalReport(scores=zero_scores, per_event=[], avg_cost_usd=0.0, avg_tokens=0.0)

    accumulated: dict[str, list[float]] = {}
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
            result = metric.compute(prediction, event)
            flat = _flatten_metric_result(metric.name, result)
            for key, value in flat.items():
                if isinstance(value, (int, float)):
                    accumulated.setdefault(key, []).append(value)
                event_record[key] = value

        total_cost.append(prediction.total_cost_usd)
        total_tokens_list.append(float(prediction.total_tokens))
        per_event.append(event_record)

    averaged_scores = {
        name: sum(values) / len(values)
        for name, values in accumulated.items()
        if values
    }

    return EvalReport(
        scores=averaged_scores,
        per_event=per_event,
        avg_cost_usd=sum(total_cost) / len(total_cost),
        avg_tokens=sum(total_tokens_list) / len(total_tokens_list),
    )


def _flatten_metric_result(name: str, result: float | dict) -> dict[str, float | None]:
    """Flatten a metric result into {key: value} dict.

    Float results map to {name: float}.
    Dict results are flattened: skipped dicts return empty; numeric leaves get
    name + "_" + key.
    """
    if isinstance(result, (int, float)):
        return {name: float(result)}

    if not isinstance(result, dict):
        return {}

    # Skipped events contribute nothing to averages
    if result.get("skipped"):
        return {}

    flat: dict[str, float | None] = {}
    for key, value in result.items():
        if key == "skipped":
            continue
        if isinstance(value, (int, float)):
            flat[f"{name}_{key}"] = float(value)
        elif isinstance(value, list):
            flat[f"{name}_{key}"] = None  # non-scalar → not averaged
        else:
            flat[f"{name}_{key}"] = None
    return flat


def _metric_keys_zero(metric) -> dict[str, float]:
    """Return zero-initialized keys for a metric (used for empty golden sets)."""
    name = metric.name
    if name in ("cause_match",):
        return {
            f"{name}_precision": 0.0,
            f"{name}_recall": 0.0,
            f"{name}_f1": 0.0,
            f"{name}_direction_accuracy": 0.0,
        }
    if name in ("citation_faithfulness",):
        return {f"{name}_score": 0.0}
    if name in ("refusal_correctness",):
        return {f"{name}_score": 0.0}
    if name in ("direction_accuracy",):
        return {f"{name}_direction_accuracy": 0.0}
    return {name: 0.0}
