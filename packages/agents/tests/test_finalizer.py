from __future__ import annotations

from attribution_fixtures import make_hypothesis
from catalyst_agents.nodes.finalizer import finalizer
from catalyst_agents.state import OutputStatus


def test_finalizer_ranks_hypotheses_with_single_ranking_body():
    h1 = make_hypothesis(cause="market", gate_passed=True, direct_support=True, dedup_clusters=1, max_relevance=1.0)
    h2 = make_hypothesis(cause="earnings_guidance", gate_passed=True, direct_support=True, dedup_clusters=2, max_relevance=0.9)
    out = finalizer({"hypotheses": [h1.model_dump(mode="json"), h2.model_dump(mode="json")], "context_artifact": {"ok": True}})
    assert [h["cause_label"] for h in out["ranked_hypotheses"]] == ["earnings_guidance", "market"]
    assert out["output_status"] == OutputStatus.SUFFICIENT


def test_finalizer_gate_failed_hypotheses_abstain():
    h = make_hypothesis(cause="market", gate_passed=False, direct_support=True, dedup_clusters=2, max_relevance=1.0)
    out = finalizer({"hypotheses": [h.model_dump(mode="json")], "context_artifact": {"ok": True}})
    assert out["output_status"] == OutputStatus.ABSTAIN


def test_finalizer_preserves_system_error():
    out = finalizer({"output_status": OutputStatus.SYSTEM_ERROR, "hypotheses": []})
    assert out["output_status"] == OutputStatus.SYSTEM_ERROR
