"""
TemporalPrecision — binary check that the predicted trade_date matches the golden event date.

Returns 1.0 if dates resolve to the same calendar day, 0.0 otherwise.
Parses dates to handle format variations (e.g. "2026-1-15" vs "2026-01-15").
"""
from __future__ import annotations

from datetime import date, datetime

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult


def _parse_date(value: str) -> date | None:
    """Best-effort parse of an ISO-ish date string to a date object."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


class TemporalPrecision:
    """
    Score = 1.0 if predicted.trade_date == golden.trade_date else 0.0.

    Compares parsed date objects to tolerate format variations.
    Returns 0.0 if either date cannot be parsed.
    """

    name: str = "temporal_precision"

    def compute(self, predicted: AttributionResult, golden: GoldenEvent) -> float:
        pred_date = _parse_date(predicted.trade_date)
        gold_date = _parse_date(golden.trade_date)
        if pred_date is None or gold_date is None:
            return 0.0
        return 1.0 if pred_date == gold_date else 0.0
