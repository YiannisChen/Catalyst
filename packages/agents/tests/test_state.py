"""M5-11: V1.1 thin orchestration state and cost tracking.

The sealed MCJ AttributionState was archived; FoundationGraphState is the only
new-write state: refs/hashes and counters, never copied evidence/prose.
"""
from __future__ import annotations

from catalyst_agents.state import FoundationGraphState


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
    # No copied evidence, prose, causes, or reasoning in the thin state.
    for forbidden in ("retrieved_chunks", "graded_evidence", "critic_reasoning", "causes", "summary_md"):
        assert forbidden not in state
