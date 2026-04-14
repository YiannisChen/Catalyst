"""
GroundingRate — fraction of predicted causes that cite at least one evidence ID
present in the result's retrieved_evidence list.

A cause is "grounded" when the intersection of its evidence_ids and the
set of asset_ids from retrieved_evidence is non-empty.
"""
from __future__ import annotations

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult


class GroundingRate:
    """
    Score = |{predicted causes with ≥1 evidence_id matching a retrieved asset_id}| / |predicted causes|

    Returns 0.0 when there are no predicted causes.
    """

    name: str = "grounding_rate"

    def compute(self, predicted: AttributionResult, golden: GoldenEvent) -> float:  # noqa: ARG002
        if not predicted.causes:
            return 0.0

        retrieved_ids = {ev.asset_id for ev in predicted.retrieved_evidence}

        grounded = sum(
            1
            for cause in predicted.causes
            if retrieved_ids.intersection(cause.evidence_ids)
        )
        return grounded / len(predicted.causes)
