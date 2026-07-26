from __future__ import annotations

from catalyst_agents.cost_tracker import CostEstimate, track_cost


class Usage:
    input_tokens = 1000
    output_tokens = 200
    total_tokens = 1200


class Response:
    usage = Usage()


def test_known_cost_estimate():
    est = CostEstimate(model_id="gpt-4o", tokens_prompt=1000, tokens_completion=200)
    assert est.cost_status == "known"
    assert est.cost_usd == (1000 * 2.5 + 200 * 10.0) / 1_000_000


def test_unknown_cost_estimate_is_nullable():
    est = CostEstimate(model_id="unknown-model", tokens_prompt=1000, tokens_completion=200)
    assert est.cost_status == "unknown"
    assert est.cost_usd is None


def test_unknown_cost_contaminates_run_aggregate():
    state = {"model_id": "unknown-model", "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    track_cost(state, "judge", Response())
    assert state["cost_breakdown"][0]["cost_status"] == "unknown"
    assert state["cost_breakdown"][0]["cost_usd"] is None
    assert state["cost_status"] == "unknown"
    assert state["total_cost_usd"] is None


def test_known_cost_keeps_known_aggregate():
    state = {"model_id": "gpt-4o", "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    track_cost(state, "critic", Response())
    assert state["cost_status"] == "known"
    assert state["total_cost_usd"] is not None
