"""M5-11: V1.1 DecisionRouter over the normalized EvidenceAssessment."""
from __future__ import annotations

from catalyst_agents.attribution.analyst import ResearchDecision
from catalyst_agents.nodes.decision_router import route_assessment


class _Assessment:
    def __init__(self, decision: ResearchDecision, batch: object | None = None):
        self.research_decision = decision
        self.corrective_batch = batch


def test_ready_routes_ready() -> None:
    assert route_assessment(_Assessment(ResearchDecision.READY)) == "ready"


def test_abstain_routes_abstain() -> None:
    assert route_assessment(_Assessment(ResearchDecision.ABSTAIN)) == "abstain"


def test_follow_up_with_executable_batch_routes_follow_up() -> None:
    assert route_assessment(_Assessment(ResearchDecision.FOLLOW_UP, batch=object())) == "follow_up"


def test_follow_up_without_batch_normalizes_to_ready() -> None:
    assert route_assessment(_Assessment(ResearchDecision.FOLLOW_UP, batch=None)) == "ready"
