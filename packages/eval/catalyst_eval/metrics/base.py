"""
BaseMetric protocol — all evaluation metrics must satisfy this interface.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult


@runtime_checkable
class BaseMetric(Protocol):
    """Structural interface for attribution evaluation metrics."""

    name: str

    def compute(self, predicted: AttributionResult, golden: GoldenEvent) -> float:
        """
        Compute a scalar score in [0.0, 1.0] comparing predicted to golden.

        Parameters
        ----------
        predicted:
            The agent's output for a given ticker and trade date.
        golden:
            The ground-truth event with labelled causes.

        Returns
        -------
        float
            Score in [0.0, 1.0] where 1.0 is perfect.
        """
        ...
