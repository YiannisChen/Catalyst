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


def test_deepseek_chat_alias_has_no_production_pricing_lock():
    """deepseek-chat is retired (2026-07-24) and must not carry a pricing lock."""
    est = CostEstimate(model_id="deepseek-chat", tokens_prompt=1_000_000, tokens_completion=1_000_000)
    assert est.cost_status == "unknown"
    assert est.cost_usd is None


def test_deepseek_v4_flash_pricing_is_known_and_bounded():
    """deepseek-v4-flash is the Wave 3 production model; cost must be known and <= USD 5."""
    est = CostEstimate(model_id="deepseek-v4-flash", tokens_prompt=1_000_000, tokens_completion=1_000_000)
    assert est.cost_status == "known"
    assert est.cost_usd == 0.20 + 0.60
    assert est.cost_usd <= 5.0
