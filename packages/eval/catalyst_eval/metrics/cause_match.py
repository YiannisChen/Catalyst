"""
CauseMatch — LLM judge for semantic event-cause matching.

Outputs PAIRINGS: list of (pred_idx, golden_idx, verdict) where
verdict ∈ {same_event_same_direction, same_event_wrong_direction, unrelated}.

Derived metrics: precision, recall, F1, direction_accuracy.

Skip rules:
- SKIP ONLY: golden.should_refuse == True OR golden.causes == [].
- Empty predicted on answerable case ⇒ precision excluded (None), recall=0, F1=0.

Design refs: §0.4, §0.5.
"""
from __future__ import annotations

from catalyst_eval.metrics.judge_base import JudgeBase
from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult

_VERDICTS = {"same_event_same_direction", "same_event_wrong_direction", "unrelated"}


class CauseMatch(JudgeBase):
    """LLM judge that pairs predicted causes to golden causes by semantic match."""

    name: str = "cause_match"
    criteria: str = (
        "Determine whether each predicted cause refers to the same real-world "
        "event as a golden cause, and whether it describes the same price direction."
    )
    evaluation_steps: list[str] = [
        "For each predicted cause, find the best-matching golden cause by semantic event identity.",
        "If the same event: check direction (positive/negative) matches.",
        "Assign verdict: same_event_same_direction, same_event_wrong_direction, or unrelated.",
        "Each golden cause may be matched at most once (greedy assignment).",
    ]
    rubric: dict[str, float] = {
        "same_event_same_direction": 1.0,
        "same_event_wrong_direction": 0.5,
        "unrelated": 0.0,
    }

    def compute(self, predicted: AttributionResult, golden: GoldenEvent) -> dict:
        """Compute cause_match pairings and derived metrics.

        Returns:
            dict with keys: pairings, precision, recall, f1, direction_accuracy,
            matched_count, predicted_count, golden_count, skipped (bool).
        """
        # Skip rules
        if golden.should_refuse or len(golden.causes) == 0:
            return {"skipped": True, "pairings": []}

        if len(predicted.causes) == 0:
            # Empty predicted on answerable case → recall=0, F1=0
            return {
                "skipped": False,
                "pairings": [],
                "precision": None,
                "recall": 0.0,
                "f1": 0.0,
                "direction_accuracy": None,
                "matched_count": 0,
                "predicted_count": 0,
                "golden_count": len(golden.causes),
            }

        # Build prompt and judge
        prompt = self._build_pairing_prompt(predicted, golden)
        verdict = self._cached_judge(prompt, case_id=golden.id)

        pairings = self._validate_pairings(verdict.get("pairings", []),
                                           len(predicted.causes), len(golden.causes))
        return self._derive_metrics(pairings, len(predicted.causes), len(golden.causes))

    def _build_pairing_prompt(self, predicted: AttributionResult, golden: GoldenEvent) -> str:
        """Build the pairing prompt — cause_match judges EVENT MATCH + DIRECTION only.
        Evidence support lives ONLY in citation_faithfulness. No evidence info in prompt.
        """
        pred_lines = [f"{i}. [{pred.direction}] {pred.text}" for i, pred in enumerate(predicted.causes)]
        gold_lines = [f"{i}. [{cause.category.value}] {cause.text}" for i, cause in enumerate(golden.causes)]

        return (
            f"Ticker: {golden.ticker} | Date: {golden.trade_date} | Move: {golden.price_move_pct:+.2f}%\n\n"
            f"Predicted Causes:\n" + "\n".join(pred_lines) + "\n\n"
            f"Golden Causes:\n" + "\n".join(gold_lines) + "\n\n"
            f"For each predicted cause, find the best-matching golden cause. "
            f"Verdict: same_event_same_direction (same event, same direction), "
            f"same_event_wrong_direction (same event, wrong direction), "
            f"or unrelated (different event).\n"
            f"Return JSON: {{\"pairings\": [{{\"pred_idx\": int, \"golden_idx\": int, \"verdict\": str}}]}}"
        )

    @staticmethod
    def _validate_pairings(pairings: list[dict], n_pred: int, n_gold: int) -> list[dict]:
        """Filter pairings to valid indices and known verdicts."""
        valid = []
        seen_golden: set[int] = set()
        for p in pairings:
            pi = p.get("pred_idx", -1)
            gi = p.get("golden_idx", -1)
            v = p.get("verdict", "")
            if (isinstance(pi, int) and 0 <= pi < n_pred
                    and isinstance(gi, int) and 0 <= gi < n_gold
                    and v in _VERDICTS
                    and gi not in seen_golden):
                valid.append({"pred_idx": pi, "golden_idx": gi, "verdict": v})
                seen_golden.add(gi)
        return valid

    @staticmethod
    def _derive_metrics(pairings: list[dict], n_pred: int, n_gold: int) -> dict:
        """Derive precision, recall, F1, direction_accuracy from pairings."""
        matched = [p for p in pairings if p["verdict"] != "unrelated"]
        matched_count = len(matched)
        correct_dir = sum(1 for p in matched if p["verdict"] == "same_event_same_direction")

        precision = matched_count / n_pred if n_pred > 0 else None
        recall = matched_count / n_gold if n_gold > 0 else 1.0
        if precision is not None and (precision + recall) > 0:
            f1 = 2 * precision * recall / (precision + recall)
        else:
            f1 = 0.0

        direction_accuracy = correct_dir / matched_count if matched_count > 0 else None

        return {
            "skipped": False,
            "pairings": pairings,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "direction_accuracy": direction_accuracy,
            "matched_count": matched_count,
            "correct_direction_count": correct_dir,
            "predicted_count": n_pred,
            "golden_count": n_gold,
        }
