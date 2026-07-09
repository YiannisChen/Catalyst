"""
CitationFaithfulness — per-cause LLM judge for evidence support.

Per-cause unit (NOT summary-level decomposition). Three-level verdict:
- supported (1.0): evidence fully supports the cause claim
- partial (0.5): evidence partially supports
- unsupported (0.0): evidence does not support

Score = mean verdict across all causes.
Unresolvable cited evidence_id → auto-unsupported (citation quality gate).

Skip rules: refusal cases and empty-output cases are SKIPPED (NOT scored 1.0).
RAGAS-style decomposition is DEFERRABLE.

Design refs: §0.4, §0.5.
"""
from __future__ import annotations

from catalyst_eval.metrics.judge_base import JudgeBase
from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult

_VERDICT_MAP = {"supported": 1.0, "partial": 0.5, "unsupported": 0.0}


class CitationFaithfulness(JudgeBase):
    """LLM judge: per-cause evidence support — three-level verdict."""

    name: str = "citation_faithfulness"
    criteria: str = (
        "Determine whether the cited evidence supports each predicted cause claim."
    )
    evaluation_steps: list[str] = [
        "For each predicted cause, read the cited evidence content.",
        "If a cited evidence_id cannot be resolved to content → auto-unsupported.",
        "Judge: supported (evidence fully backs claim), partial (partially backs), unsupported (does not back).",
    ]
    rubric: dict[str, float] = {
        "supported": 1.0,
        "partial": 0.5,
        "unsupported": 0.0,
    }

    def compute(self, predicted: AttributionResult, golden: GoldenEvent) -> dict:
        """Compute per-cause citation faithfulness.

        Returns:
            dict with: score (float), per_cause_verdicts (list), skipped (bool).
        """
        # Skip rules
        if golden.should_refuse:
            return {"skipped": True, "score": None, "per_cause_verdicts": []}

        if len(predicted.causes) == 0:
            return {"skipped": True, "score": None, "per_cause_verdicts": []}

        # Build evidence lookup
        evidence_lookup: dict[str, str] = {}
        for ev in predicted.retrieved_evidence:
            evidence_lookup[ev.asset_id] = ev.content_md

        prompt = self._build_faithfulness_prompt(predicted, evidence_lookup)
        verdict = self._cached_judge(prompt, case_id=golden.id)

        per_cause = verdict.get("per_cause_verdicts", [])
        scores = []
        validated = []
        for i, v in enumerate(per_cause):
            vtype = v.get("verdict", "unsupported")
            if vtype not in _VERDICT_MAP:
                vtype = "unsupported"
            scores.append(_VERDICT_MAP[vtype])
            validated.append({
                "cause_idx": i,
                "verdict": vtype,
                "reason": v.get("reason", ""),
            })

        avg_score = sum(scores) / len(scores) if scores else 0.0
        return {
            "skipped": False,
            "score": avg_score,
            "per_cause_verdicts": validated,
        }

    def _build_faithfulness_prompt(
        self, predicted: AttributionResult, evidence_lookup: dict[str, str]
    ) -> str:
        """Build per-cause faithfulness prompt with evidence blocks."""
        parts = []
        for i, cause in enumerate(predicted.causes):
            parts.append(f"Cause {i}: {cause.text}")
            parts.append(f"  Cited evidence_ids: {cause.evidence_ids}")
            parts.append("  Evidence content:")
            for eid in cause.evidence_ids:
                content = evidence_lookup.get(eid, "[UNRESOLVABLE — auto-unsupported]")
                parts.append(f"    [{eid}]: {content[:500]}")
            parts.append("")

        return (
            "\n".join(parts) + "\n"
            "For each cause above, judge whether the cited evidence supports it.\n"
            "Return JSON: {\"per_cause_verdicts\": ["
            "{\"verdict\": \"supported|partial|unsupported\", \"reason\": \"...\"}]}"
        )
