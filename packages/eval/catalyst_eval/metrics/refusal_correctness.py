"""
RefusalCorrectness — deterministic refusal behavior check.

Decision table:
| golden.should_refuse | predicted.causes | predicted.summary | score |
|---|---|---|---|
| True | [] | null/empty | 1.0 |
| True | non-empty | any | 0.0 |
| True | [] | non-empty | 0.0 |
| False | any | any | null (N/A, skipped) |

Design refs: §0.4, §0.5.
"""
from __future__ import annotations

from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult


class RefusalCorrectness:
    """Deterministic refusal behavior check — no LLM needed."""

    name: str = "refusal_correctness"

    def compute(self, predicted: AttributionResult, golden: GoldenEvent) -> dict:
        """Compute refusal correctness per the decision table.

        Returns:
            dict with: score (float|None), skipped (bool).
        """
        if not golden.should_refuse:
            return {"skipped": True, "score": None}

        has_causes = len(predicted.causes) > 0
        has_summary = bool(predicted.summary and predicted.summary.strip())

        if has_causes:
            return {"skipped": False, "score": 0.0}

        if has_summary:
            return {"skipped": False, "score": 0.0}

        # should_refuse=True, causes=[], summary empty → correct refusal
        return {"skipped": False, "score": 1.0}
