"""
CategoryAccuracy — fraction of predicted causes whose category matches
any golden cause category (case-insensitive).
"""
from __future__ import annotations

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult


class CategoryAccuracy:
    """
    Score = |{predicted causes whose category ∈ golden categories}| / |predicted causes|

    Comparison is case-insensitive to tolerate agent capitalisation variance.
    Returns 0.0 when there are no predicted causes.
    """

    name: str = "category_accuracy"

    def compute(self, predicted: AttributionResult, golden: GoldenEvent) -> float:
        if not predicted.causes:
            return 0.0

        golden_categories = {cause.category.value.lower() for cause in golden.causes}

        matches = sum(
            1
            for pred in predicted.causes
            if pred.category.lower() in golden_categories
        )
        return matches / len(predicted.causes)
