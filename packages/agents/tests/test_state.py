"""TDD tests for AttributionState schema and cost tracking logic.

Written before implementation per TDD discipline.
"""
from dataclasses import fields
import pytest
from typing import get_type_hints

from catalyst_agents.cost_tracker import track_cost, MODEL_PRICING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = input_tokens + output_tokens


class MockResponse:
    def __init__(self, input_tokens: int = 3000, output_tokens: int = 500) -> None:
        self.usage = MockUsage(input_tokens, output_tokens)


# ---------------------------------------------------------------------------
# cost_tracker tests
# ---------------------------------------------------------------------------

def test_track_cost_appends_breakdown():
    state = {
        "model_id": "claude-sonnet-4-20250514",
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }
    track_cost(state, "critic", MockResponse())
    assert len(state["cost_breakdown"]) == 1
    assert state["cost_breakdown"][0]["node"] == "critic"
    assert state["total_cost_usd"] > 0
    assert state["total_tokens"] == 3500


def test_track_cost_computes_correct_amount():
    """claude-sonnet-4-20250514 at 3000 input + 500 output:
    cost = (3000 * 3.0 + 500 * 15.0) / 1_000_000
         = (9000 + 7500) / 1_000_000
         = 16500 / 1_000_000
         = 0.0165
    """
    state = {
        "model_id": "claude-sonnet-4-20250514",
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }
    track_cost(state, "judge", MockResponse(input_tokens=3000, output_tokens=500))
    assert pytest.approx(state["total_cost_usd"], rel=1e-6) == 0.0165


def test_track_cost_accumulates():
    """Two successive calls must produce 2 breakdown entries and a summed total."""
    state = {
        "model_id": "gpt-4o",
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }
    track_cost(state, "miner", MockResponse(input_tokens=1000, output_tokens=200))
    first_cost = state["total_cost_usd"]
    track_cost(state, "critic", MockResponse(input_tokens=2000, output_tokens=400))
    assert len(state["cost_breakdown"]) == 2
    assert state["cost_breakdown"][0]["node"] == "miner"
    assert state["cost_breakdown"][1]["node"] == "critic"
    assert pytest.approx(state["total_cost_usd"], rel=1e-9) == pytest.approx(
        first_cost
        + (2000 * MODEL_PRICING["gpt-4o"]["input"] + 400 * MODEL_PRICING["gpt-4o"]["output"])
        / 1_000_000,
        rel=1e-9,
    )


def test_model_pricing_has_expected_models():
    expected_models = {
        "claude-sonnet-4-20250514",
        "gpt-4o",
        "claude-haiku-4-5-20251001",
    }
    assert expected_models.issubset(set(MODEL_PRICING.keys()))


def test_model_pricing_keys_have_input_output():
    for model, pricing in MODEL_PRICING.items():
        assert "input" in pricing, f"{model} missing 'input' key"
        assert "output" in pricing, f"{model} missing 'output' key"
        assert pricing["input"] > 0
        assert pricing["output"] > 0


# ---------------------------------------------------------------------------
# AttributionState schema tests
# ---------------------------------------------------------------------------

def test_attribution_state_is_typeddict():
    """AttributionState must be a TypedDict (has __annotations__ and __required_keys__)."""
    from catalyst_agents.state import AttributionState

    # TypedDict classes expose __annotations__ and __required_keys__
    assert hasattr(AttributionState, "__annotations__"), "Missing __annotations__"
    assert hasattr(AttributionState, "__required_keys__"), "Missing __required_keys__"


def test_attribution_state_required_fields():
    """Spot-check that key fields from spec Section 4.2 are present."""
    from catalyst_agents.state import AttributionState

    annotations = AttributionState.__annotations__
    required_fields = [
        "ticker",
        "trade_date",
        "query",
        "price_move_pct",
        "query_ticker_raw",
        "ticker_consistent",
        "market_session_valid",
        "magnitude_plausible",
        "retrieved_chunks",
        "reranked_chunks",
        "graded_evidence",
        "critic_reasoning",
        "causes",
        "summary_md",
        "grounding_rate",
        "cost_breakdown",
        "total_cost_usd",
        "total_tokens",
        "model_id",
    ]
    for field in required_fields:
        assert field in annotations, f"AttributionState missing field: '{field}'"


def test_attribution_state_instantiation():
    """A dict matching the TypedDict shape must be constructable without errors."""
    from catalyst_agents.state import AttributionState

    instance: AttributionState = {
        "ticker": "AAPL",
        "trade_date": "2025-01-15",
        "query": None,
        "price_move_pct": -2.4,
        "query_ticker_raw": None,
        "ticker_consistent": None,
        "market_session_valid": None,
        "magnitude_plausible": None,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "graded_evidence": [],
        "critic_reasoning": "",
        "causes": [],
        "summary_md": "",
        "grounding_rate": None,
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
        "model_id": "claude-sonnet-4-20250514",
    }
    assert instance["ticker"] == "AAPL"
    assert instance["model_id"] == "claude-sonnet-4-20250514"


def test_output_status_enum_has_expected_names():
    from catalyst_agents.state import OutputStatus

    assert {"SUFFICIENT", "PARTIAL", "INSUFFICIENT", "SYSTEM_ERROR"} == {
        status.name for status in OutputStatus
    }


def test_critic_decision_dataclass_has_required_fields():
    from catalyst_agents.state import CriticDecision

    assert [field.name for field in fields(CriticDecision)] == [
        "sufficiency",
        "next_action",
        "magnitude_coverage",
        "reasoning",
    ]
