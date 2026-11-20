"""DecisionRouter node — deterministic V1.1 routing (M5-9/M5-11).

Routes the normalized EvidenceAssessment to READY | FOLLOW_UP | ABSTAIN. A
FOLLOW_UP only routes to corrective execution when a code-owned executable
batch exists; otherwise normalization has already produced READY. The legacy
Critic-action router was archived at M5-11.
"""
from __future__ import annotations

from catalyst_agents.attribution.analyst import ResearchDecision


def route_assessment(assessment: object) -> str:
    """Route the normalized EvidenceAssessment to READY | FOLLOW_UP | ABSTAIN."""
    decision = getattr(assessment, "research_decision", ResearchDecision.READY)
    if decision is ResearchDecision.FOLLOW_UP:
        if getattr(assessment, "corrective_batch", None) is not None:
            return "follow_up"
        return "ready"
    if decision is ResearchDecision.ABSTAIN:
        return "abstain"
    return "ready"


__all__ = ["route_assessment"]
