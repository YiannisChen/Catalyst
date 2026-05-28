"""Tests for validator/finalizer control-plane behavior."""
from __future__ import annotations

import json

import pytest

from catalyst_agents.nodes.validator import validator
from catalyst_agents.state import CriticDecision, OutputStatus


class MockUsage:
    def __init__(self, input_tokens: int = 2000, output_tokens: int = 300) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = input_tokens + output_tokens


class MockResponse:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = MockUsage()


class SequenceLLM:
    def __init__(self, responses: list[str] | None = None, *, error: Exception | None = None) -> None:
        self._responses = responses or []
        self._error = error
        self.calls = 0

    def invoke(self, prompt: str) -> MockResponse:
        self.calls += 1
        if self._error is not None:
            raise self._error
        idx = min(self.calls - 1, len(self._responses) - 1)
        return MockResponse(self._responses[idx])


def _base_state() -> dict:
    return {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": None,
        "price_move_pct": -4.2,
        "retrieved_chunks": [],
        "reranked_chunks": [
            {
                "asset_id": "c1",
                "reference_date": "2026-01-14",
                "content_md": "China export restrictions tightened.",
                "source_type": "polygon_news",
            },
            {
                "asset_id": "c2",
                "reference_date": "2026-01-15",
                "content_md": "Apple suppliers warned on demand.",
                "source_type": "polygon_news",
            },
        ],
        "graded_evidence": [
            {
                "chunk_id": "c1",
                "relevance": 0.9,
                "category": "geopolitical",
                "temporal_match": True,
                "reasoning": "Direct cause",
            },
            {
                "chunk_id": "c2",
                "relevance": 0.8,
                "category": "sector",
                "temporal_match": True,
                "reasoning": "Supporting context",
            },
        ],
        "critic_reasoning": "Enough evidence to proceed, but not complete coverage.",
        "critic_decision": CriticDecision(
            sufficiency="partial",
            next_action="proceed",
            magnitude_coverage=0.65,
            reasoning="Two grounded pieces of evidence support a partial answer.",
        ),
        "causes": [
            {
                "text": "China export restrictions hurt sentiment",
                "category": "geopolitical",
                "confidence": 0.8,
                "evidence_ids": ["c1"],
                "direction": "negative",
            }
        ],
        "summary_md": "AAPL fell after [c1] export restrictions tightened.",
        "grounding_rate": 1.0,
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
        "model_id": "claude-sonnet-4-20250514",
    }


def test_validator_passes_valid_output_without_regeneration():
    state = _base_state()

    result = validator(state)

    assert result["output_status"] == OutputStatus.PARTIAL
    assert result["validation_error"] is None
    assert result["validator_attempts"] == 0
    assert result["causes"] == state["causes"]


def test_validator_retries_once_then_downgrades_partial_for_missing_evidence_id():
    state = _base_state()
    state["causes"] = [
        {
            "text": "Invented citation",
            "category": "geopolitical",
            "confidence": 0.8,
            "evidence_ids": ["missing-id"],
            "direction": "negative",
        }
    ]
    invalid_retry = json.dumps(
        {
            "causes": state["causes"],
            "summary_md": "AAPL fell due to [missing-id] unsupported evidence.",
            "self_grounding_check": {"total_claims": 1, "grounded_claims": 0, "ungrounded_claims": 1},
        }
    )

    result = validator(state, llm=SequenceLLM([invalid_retry]))

    assert result["output_status"] == OutputStatus.PARTIAL
    assert result["validation_error"] == "evidence_id_missing"
    assert result["validator_attempts"] == 1


def test_validator_retries_once_then_downgrades_partial_for_time_window_violation():
    state = _base_state()
    state["reranked_chunks"][0]["reference_date"] = "2026-01-25"
    retry_output = json.dumps(
        {
            "causes": state["causes"],
            "summary_md": state["summary_md"],
            "self_grounding_check": {"total_claims": 1, "grounded_claims": 1, "ungrounded_claims": 0},
        }
    )

    result = validator(state, llm=SequenceLLM([retry_output]))

    assert result["output_status"] == OutputStatus.PARTIAL
    assert result["validation_error"] == "time_window_violation"
    assert result["validator_attempts"] == 1


def test_validator_retries_once_then_downgrades_partial_for_schema_failure():
    state = _base_state()
    state["causes"][0]["confidence"] = 1.5
    retry_output = json.dumps(
        {
            "causes": state["causes"],
            "summary_md": state["summary_md"],
            "self_grounding_check": {"total_claims": 1, "grounded_claims": 1, "ungrounded_claims": 0},
        }
    )

    result = validator(state, llm=SequenceLLM([retry_output]))

    assert result["output_status"] == OutputStatus.PARTIAL
    assert result["validation_error"] == "schema_invalid"
    assert result["validator_attempts"] == 1


def test_validator_retries_once_then_downgrades_partial_for_magnitude_failure():
    state = _base_state()
    state["critic_decision"] = CriticDecision(
        sufficiency="sufficient",
        next_action="proceed",
        magnitude_coverage=0.45,
        reasoning="Claimed complete coverage, but support is weak.",
    )

    corrected_partial = json.dumps(
        {
            "causes": state["causes"],
            "summary_md": state["summary_md"],
            "self_grounding_check": {"total_claims": 1, "grounded_claims": 1, "ungrounded_claims": 0},
        }
    )

    result = validator(state, llm=SequenceLLM([corrected_partial]))

    assert result["output_status"] == OutputStatus.PARTIAL
    assert result["validation_error"] == "magnitude_sanity_failed"
    assert result["validator_attempts"] == 1


def test_validator_returns_system_error_when_correction_call_fails():
    state = _base_state()
    state["causes"][0]["evidence_ids"] = ["missing-id"]

    result = validator(state, llm=SequenceLLM(error=RuntimeError("validator timeout")))

    assert result["output_status"] == OutputStatus.SYSTEM_ERROR
    assert result["validation_error"] == "model_timeout"
    assert result["validator_attempts"] == 1


def test_validator_returns_raw_llm_response_on_successful_correction():
    state = _base_state()
    state["causes"][0]["evidence_ids"] = ["missing-id"]
    corrected = json.dumps(
        {
            "causes": [
                {
                    "text": "China export restrictions hurt sentiment",
                    "category": "geopolitical",
                    "confidence": 0.8,
                    "evidence_ids": ["c1"],
                    "direction": "negative",
                }
            ],
            "summary_md": "AAPL fell after [c1] export restrictions tightened.",
            "self_grounding_check": {"total_claims": 1, "grounded_claims": 1, "ungrounded_claims": 0},
        }
    )

    result = validator(state, llm=SequenceLLM([corrected]))

    assert result["validator_raw_llm_response"] == corrected
