"""M5-11: V1.1 thin orchestration state and cost tracking.

The sealed MCJ AttributionState was archived; FoundationGraphState is the only
new-write state: refs/hashes and counters, never copied evidence/prose.
"""
from __future__ import annotations

from catalyst_agents.cost_tracker import track_cost, MODEL_PRICING
from catalyst_agents.state import FoundationGraphState, FoundationStage


class MockUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = input_tokens + output_tokens


class MockResponse:
    def __init__(self, input_tokens: int = 3000, output_tokens: int = 500) -> None:
        self.usage = MockUsage(input_tokens, output_tokens)


def test_foundation_graph_state_is_typeddict() -> None:
    assert hasattr(FoundationGraphState, "__annotations__")
    assert hasattr(FoundationGraphState, "__required_keys__")


def test_foundation_graph_state_is_thin_orchestration() -> None:
    state: FoundationGraphState = {
        "run_id": "run:1",
        "stage": FoundationStage.TERMINAL,
        "round": 1,
        "attempt": 1,
        "deadline_epoch_ms": 1_700_000_000_000,
        "cancel_requested": False,
        "terminal_error": None,
        "evidence_state_ref": "artifact:evidence_state:run:1",
        "evidence_state_hash": "e" * 64,
        "context_pack_ref": "artifact:context_pack:run:1",
        "context_pack_hash": "c" * 64,
    }
    assert state["run_id"] == "run:1"
    assert state["stage"] is FoundationStage.TERMINAL
    # No copied evidence, prose, causes, or reasoning in the thin state.
    for forbidden in ("retrieved_chunks", "graded_evidence", "critic_reasoning", "causes", "summary_md"):
        assert forbidden not in state


def test_track_cost_appends_breakdown() -> None:
    state = {
        "model_id": "claude-sonnet-4-20250514",
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }
    track_cost(state, "evidence_analyst", MockResponse())
    assert len(state["cost_breakdown"]) == 1
    assert state["cost_breakdown"][0]["node"] == "evidence_analyst"
    assert state["total_cost_usd"] > 0
    assert state["total_tokens"] == 3500


def test_track_cost_computes_correct_amount() -> None:
    state = {
        "model_id": "claude-sonnet-4-20250514",
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }
    track_cost(state, "streaming_writer", MockResponse(input_tokens=3000, output_tokens=500))
    assert abs(state["total_cost_usd"] - 0.0165) < 1e-6


def test_model_pricing_has_expected_models() -> None:
    expected_models = {
        "claude-sonnet-4-20250514",
        "gpt-4o",
        "claude-haiku-4-5-20251001",
    }
    assert expected_models.issubset(set(MODEL_PRICING.keys()))


def test_foundation_stage_covers_v1_state_machine() -> None:
    stages = [stage.value for stage in FoundationStage]
    for expected in (
        "run_admission",
        "observation_build",
        "research_execution",
        "evidence_state",
        "coverage_summary",
        "context_pack_build",
        "analyst_boundary",
        "claim_plan_build",
        "claim_validation",
        "streaming_answer_writer",
        "post_stream_assurance",
        "finalizer",
        "terminal",
    ):
        assert expected in stages
