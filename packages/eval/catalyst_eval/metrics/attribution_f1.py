"""
AttributionF1 — semantic F1 over predicted vs. golden cause texts.

Matching strategy: Jaccard similarity on lowercased whitespace tokens.
A predicted/golden pair is considered a match when Jaccard > MATCH_THRESHOLD.
Greedy assignment: each golden cause is consumed at most once.

Order-dependent: the first predicted cause to claim a golden cause wins.
For small cause lists (≤5) this is acceptable; replace with Hungarian
algorithm assignment if list sizes grow.
"""
from __future__ import annotations

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult


_MATCH_THRESHOLD = 0.2


def _jaccard(text_a: str, text_b: str) -> float:
    """Jaccard similarity on word-token sets (lowercase, whitespace-split)."""
    tokens_a = set(text_a.lower().split())
    tokens_b = set(text_b.lower().split())
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


class AttributionF1:
    """
    Token-level semantic F1 between predicted and golden cause texts.

    Precision = matched_count / predicted_count
    Recall    = matched_count / golden_count
    F1        = 2 * P * R / (P + R)
    """

    name: str = "attribution_f1"

    def compute(self, predicted: AttributionResult, golden: GoldenEvent) -> float:
        predicted_causes = predicted.causes
        golden_causes = golden.causes

        if not predicted_causes or not golden_causes:
            return 0.0

        # Greedy matching: for each predicted cause, find the best unmatched golden cause.
        available = list(range(len(golden_causes)))
        matched = 0

        for pred in predicted_causes:
            best_score = 0.0
            best_idx: int | None = None
            for idx in available:
                score = _jaccard(pred.text, golden_causes[idx].text)
                if score > best_score:
                    best_score = score
                    best_idx = idx
            if best_score > _MATCH_THRESHOLD and best_idx is not None:
                matched += 1
                available.remove(best_idx)

        precision = matched / len(predicted_causes)
        recall = matched / len(golden_causes)

        if precision + recall == 0.0:
            return 0.0
        return 2 * precision * recall / (precision + recall)
