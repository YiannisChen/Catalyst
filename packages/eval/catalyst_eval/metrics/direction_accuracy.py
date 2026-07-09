"""
DirectionAccuracy — derived from cause_match pairings.

direction_accuracy = |same_event_same_direction| / |matched pairings|
null if matched_count == 0.

Design refs: §0.4.
"""
from __future__ import annotations


class DirectionAccuracy:
    """Derived metric from cause_match pairings — no LLM needed."""

    name: str = "direction_accuracy"

    @staticmethod
    def compute_from_pairings(pairings: list[dict]) -> dict:
        """Compute direction accuracy from cause_match pairings.

        Args:
            pairings: List of {"pred_idx": int, "golden_idx": int, "verdict": str}
                      where verdict ∈ {same_event_same_direction, same_event_wrong_direction, unrelated}.

        Returns:
            dict with: direction_accuracy (float|None), matched_count, correct_direction_count.
        """
        matched = [p for p in pairings if p.get("verdict") != "unrelated"]
        matched_count = len(matched)
        correct = sum(1 for p in matched if p.get("verdict") == "same_event_same_direction")

        if matched_count == 0:
            return {
                "direction_accuracy": None,
                "matched_count": 0,
                "correct_direction_count": 0,
            }

        return {
            "direction_accuracy": correct / matched_count,
            "matched_count": matched_count,
            "correct_direction_count": correct,
        }
