"""
ConfidenceCalibration — measures how well an agent's confidence scores align
with actual category accuracy across confidence buckets.

Buckets: [0, 0.25), [0.25, 0.5), [0.5, 0.75), [0.75, 1.0]

For each non-empty bucket:
    - mean_confidence = average confidence of causes in the bucket
    - accuracy        = fraction of those causes whose category matches a golden category
    - calibration_error = |mean_confidence - accuracy|

Final score = 1.0 - mean_absolute_calibration_error across non-empty buckets.
Higher score = better calibration.
"""
from __future__ import annotations

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult, PredictedCause

# Bucket edges: inclusive lower bound, exclusive upper bound (last bucket includes 1.0)
_BUCKETS: list[tuple[float, float]] = [
    (0.0, 0.25),
    (0.25, 0.5),
    (0.5, 0.75),
    (0.75, 1.0001),  # 1.0001 so that confidence == 1.0 is included
]


def _in_bucket(confidence: float, lo: float, hi: float) -> bool:
    return lo <= confidence < hi


class ConfidenceCalibration:
    """
    Score = 1.0 - MACE  (Mean Absolute Calibration Error)

    Returns 0.0 when there are no predicted causes.
    """

    name: str = "confidence_calibration"

    def compute(self, predicted: AttributionResult, golden: GoldenEvent) -> float:
        if not predicted.causes:
            return 0.0

        golden_categories = {cause.category.value.lower() for cause in golden.causes}

        bucket_errors: list[float] = []

        for lo, hi in _BUCKETS:
            causes_in_bucket: list[PredictedCause] = [
                c for c in predicted.causes if _in_bucket(c.confidence, lo, hi)
            ]
            if not causes_in_bucket:
                continue

            mean_conf = sum(c.confidence for c in causes_in_bucket) / len(causes_in_bucket)
            accuracy = sum(
                1 for c in causes_in_bucket if c.category.lower() in golden_categories
            ) / len(causes_in_bucket)

            bucket_errors.append(abs(mean_conf - accuracy))

        if not bucket_errors:
            return 0.0

        mace = sum(bucket_errors) / len(bucket_errors)
        return max(0.0, 1.0 - mace)
