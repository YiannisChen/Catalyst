"""Tests for the Critic node — evidence grading and insufficient evidence handler.

Spec reference: Section 4.3 — Critic Node.
"""
from __future__ import annotations

import json
import pytest

from catalyst_agents.nodes.critic import (
    critic,
    insufficient_handler,
    system_error_handler,
    _format_chunks,
    _parse_critic_response,
    RELEVANCE_THRESHOLD,
)
from catalyst_agents.nodes.decision_router import decision_router
from catalyst_agents.state import CriticDecision


# ---------------------------------------------------------------------------
# Mock LLM infrastructure
# ---------------------------------------------------------------------------

class MockUsage:
    def __init__(self, input_tokens=3000, output_tokens=500):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = input_tokens + output_tokens


class MockResponse:
    def __init__(self, content):
        self.content = content
        self.usage = MockUsage()


class MockLLM:
    def __init__(self, response_content):
        self._content = response_content

    def invoke(self, prompt):
        return MockResponse(self._content)


class FlakyLLM:
    def __init__(self, failures, success_content=None):
        self.failures = failures
        self.success_content = success_content or GOOD_LLM_RESPONSE
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient critic failure")
        return MockResponse(self.success_content)


class BadJsonLLM:
    def __init__(self, content="not-json"):
        self.content = content
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return MockResponse(self.content)


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

MOCK_CHUNKS = [
    {
        "asset_id": "c1",
        "ticker": "AAPL",
        "source_type": "polygon_news",
        "reference_date": "2026-01-15",
        "content_md": "Apple reported record revenue.",
    },
    {
        "asset_id": "c2",
        "ticker": "AAPL",
        "source_type": "fmp_fundamentals",
        "reference_date": "2026-01-15",
        "content_md": "Revenue: $100B, Net Income: $25B",
    },
]

GOOD_LLM_RESPONSE = json.dumps({
    "graded_chunks": [
        {
            "chunk_id": "c1",
            "relevance": 0.8,
            "category": "earnings",
            "temporal_match": True,
            "reasoning": "Directly about earnings",
        },
        {
            "chunk_id": "c2",
            "relevance": 0.3,
            "category": "earnings",
            "temporal_match": True,
            "reasoning": "Financial data, less directly relevant",
        },
    ],
    "reasoning": "Strong earnings evidence available",
})

ALL_LOW_RESPONSE = json.dumps({
    "graded_chunks": [
        {
            "chunk_id": "c1",
            "relevance": 0.2,
            "category": "earnings",
            "temporal_match": False,
            "reasoning": "Outdated",
        },
        {
            "chunk_id": "c2",
            "relevance": 0.1,
            "category": "earnings",
            "temporal_match": False,
            "reasoning": "Irrelevant",
        },
    ],
    "reasoning": "No relevant evidence",
})

BASE_STATE = {
    "ticker": "AAPL",
    "trade_date": "2026-01-15",
    "query": None,
    "price_move_pct": -4.2,
    "reranked_chunks": MOCK_CHUNKS,
    "model_id": "claude-sonnet-4-20250514",
    "cost_breakdown": [],
    "total_cost_usd": 0.0,
    "total_tokens": 0,
}


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

def test_format_chunks_includes_asset_ids():
    formatted = _format_chunks(MOCK_CHUNKS)
    assert "c1" in formatted
    assert "c2" in formatted


def test_format_chunks_includes_content():
    formatted = _format_chunks(MOCK_CHUNKS)
    assert "Apple reported" in formatted
    assert "Revenue: $100B" in formatted


def test_format_chunks_includes_source_metadata():
    formatted = _format_chunks(MOCK_CHUNKS)
    assert "polygon_news" in formatted
    assert "2026-01-15" in formatted


def test_format_chunks_empty_list():
    assert _format_chunks([]) == ""


def test_parse_critic_response_plain_json():
    result = _parse_critic_response('{"graded_chunks": [], "reasoning": "test"}')
    assert result["reasoning"] == "test"
    assert result["graded_chunks"] == []


def test_parse_critic_response_with_json_fences():
    fenced = '```json\n{"graded_chunks": [], "reasoning": "test"}\n```'
    result = _parse_critic_response(fenced)
    assert result["reasoning"] == "test"


def test_parse_critic_response_with_plain_fences():
    fenced = '```\n{"graded_chunks": [], "reasoning": "fenced"}\n```'
    result = _parse_critic_response(fenced)
    assert result["reasoning"] == "fenced"


def test_relevance_threshold_value():
    """Threshold must be 0.5 per spec Section 4.3."""
    assert RELEVANCE_THRESHOLD == 0.5


# ---------------------------------------------------------------------------
# Critic node integration tests
# ---------------------------------------------------------------------------

def test_critic_filters_by_relevance():
    """Only chunks with relevance > 0.5 survive; c1=0.8 passes, c2=0.3 is dropped."""
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=MockLLM(GOOD_LLM_RESPONSE))
    assert len(result["graded_evidence"]) == 1
    assert result["graded_evidence"][0]["chunk_id"] == "c1"


def test_critic_returns_empty_when_all_low():
    """When all chunks score below threshold, graded_evidence is empty."""
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=MockLLM(ALL_LOW_RESPONSE))
    assert len(result["graded_evidence"]) == 0


def test_critic_returns_reasoning():
    """critic_reasoning must be populated from the LLM response."""
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=MockLLM(GOOD_LLM_RESPONSE))
    assert result["critic_reasoning"] == "Strong earnings evidence available"


def test_critic_tracks_cost():
    """A single cost entry with node='critic' must appear after the call."""
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    critic(state, llm=MockLLM(GOOD_LLM_RESPONSE))
    assert len(state["cost_breakdown"]) == 1
    assert state["cost_breakdown"][0]["node"] == "critic"
    assert state["total_cost_usd"] > 0


def test_critic_cost_calculation_correct():
    """Verify cost math: claude-sonnet-4 = $3/M input, $15/M output."""
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    critic(state, llm=MockLLM(GOOD_LLM_RESPONSE))
    # MockUsage: 3000 input, 500 output
    # cost = (3000 * 3.0 + 500 * 15.0) / 1_000_000 = (9000 + 7500) / 1_000_000 = 0.0165
    assert abs(state["total_cost_usd"] - 0.0165) < 1e-9


def test_critic_empty_chunks_skips_llm():
    """With no chunks, the LLM must NOT be called and cost stays zero."""
    state = {**BASE_STATE, "reranked_chunks": [], "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=MockLLM("{}"))
    assert result["graded_evidence"] == []
    assert state["total_cost_usd"] == 0.0
    assert state["total_tokens"] == 0


def test_critic_empty_chunks_returns_no_op_reasoning():
    """Empty chunk list produces an explanatory reasoning string, not an error."""
    state = {**BASE_STATE, "reranked_chunks": [], "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=MockLLM("{}"))
    assert isinstance(result["critic_reasoning"], str)
    assert len(result["critic_reasoning"]) > 0


def test_critic_threshold_boundary_excluded():
    """A chunk with relevance == RELEVANCE_THRESHOLD (0.5) must be excluded (strict >)."""
    boundary_response = json.dumps({
        "graded_chunks": [
            {"chunk_id": "c1", "relevance": 0.5, "category": "earnings",
             "temporal_match": True, "reasoning": "Exactly at threshold"},
        ],
        "reasoning": "Boundary case",
    })
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=MockLLM(boundary_response))
    # 0.5 is NOT > 0.5 — must be filtered out
    assert len(result["graded_evidence"]) == 0


def test_critic_threshold_boundary_included():
    """A chunk with relevance slightly above threshold (0.51) must pass through."""
    just_above_response = json.dumps({
        "graded_chunks": [
            {"chunk_id": "c1", "relevance": 0.51, "category": "earnings",
             "temporal_match": True, "reasoning": "Just above threshold"},
        ],
        "reasoning": "Boundary case",
    })
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=MockLLM(just_above_response))
    assert len(result["graded_evidence"]) == 1


def test_critic_retries_invoke_exception_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", sleeps.append)

    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    llm = FlakyLLM(failures=2)

    result = critic(state, llm=llm)

    assert llm.calls == 3
    assert len(result["graded_evidence"]) == 1
    assert sleeps == [0.1, 0.2]


def test_critic_three_failures_return_empty_evidence(monkeypatch):
    sleeps = []
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", sleeps.append)

    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    llm = FlakyLLM(failures=3)

    result = critic(state, llm=llm)

    assert result["graded_evidence"] == []
    assert "failed after 3 attempts" in result["critic_reasoning"].lower()
    assert llm.calls == 3
    assert sleeps == [0.1, 0.2]


def test_critic_bad_json_returns_empty_evidence_after_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", sleeps.append)

    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    llm = BadJsonLLM()

    result = critic(state, llm=llm)

    assert result["graded_evidence"] == []
    assert "failed after 3 attempts" in result["critic_reasoning"].lower()
    assert llm.calls == 3
    assert sleeps == [0.1, 0.2]


def test_critic_schema_invalid_json_returns_empty_evidence_after_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", sleeps.append)

    invalid_schema_response = json.dumps({
        "graded_chunks": [
            {
                "chunk_id": "c1",
                "relevance": 1.2,  # invalid: must be in [0, 1]
                "category": "not-a-real-category",
                "temporal_match": True,
                "reasoning": "invalid schema fields",
            }
        ],
        "reasoning": "This payload is valid JSON but invalid schema",
    })

    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    llm = BadJsonLLM(content=invalid_schema_response)

    result = critic(state, llm=llm)

    assert result["graded_evidence"] == []
    assert "failed after 3 attempts" in result["critic_reasoning"].lower()
    assert llm.calls == 3
    assert sleeps == [0.1, 0.2]


# ---------------------------------------------------------------------------
# Insufficient evidence handler tests
# ---------------------------------------------------------------------------

def test_insufficient_handler_returns_one_cause():
    state = {**BASE_STATE, "graded_evidence": []}
    result = insufficient_handler(state)
    assert len(result["causes"]) == 1


def test_insufficient_handler_cause_category_unknown():
    state = {**BASE_STATE, "graded_evidence": []}
    result = insufficient_handler(state)
    assert result["causes"][0]["category"] == "unknown"


def test_insufficient_handler_cause_text():
    state = {**BASE_STATE, "graded_evidence": []}
    result = insufficient_handler(state)
    assert "Insufficient evidence" in result["causes"][0]["text"]


def test_insufficient_handler_grounding_rate_none():
    """grounding_rate must be None — no evidence was grounded."""
    state = {**BASE_STATE, "graded_evidence": []}
    result = insufficient_handler(state)
    assert result["grounding_rate"] is None


def test_insufficient_handler_summary_contains_ticker():
    state = {**BASE_STATE, "graded_evidence": []}
    result = insufficient_handler(state)
    assert "AAPL" in result["summary_md"]


def test_insufficient_handler_summary_contains_date():
    state = {**BASE_STATE, "graded_evidence": []}
    result = insufficient_handler(state)
    assert "2026-01-15" in result["summary_md"]


def test_insufficient_handler_cause_confidence_is_one():
    """Confidence must be 1.0 — the handler is certain about the insufficiency."""
    state = {**BASE_STATE, "graded_evidence": []}
    result = insufficient_handler(state)
    assert result["causes"][0]["confidence"] == 1.0


def test_insufficient_handler_no_llm_call():
    """Handler must not mutate cost fields — it makes no LLM call."""
    state = {**BASE_STATE, "graded_evidence": [], "cost_breakdown": [], "total_cost_usd": 0.0}
    insufficient_handler(state)
    assert state["total_cost_usd"] == 0.0
    assert state["cost_breakdown"] == []


# ---------------------------------------------------------------------------
# System error handler tests (BUG-005)
# ---------------------------------------------------------------------------

def test_system_error_handler_returns_empty_causes():
    """System error must produce zero causes — not a fake 'unknown' cause."""
    state = {**BASE_STATE, "error_type": "system_error", "critic_reasoning": "LLM timeout"}
    result = system_error_handler(state)
    assert result["causes"] == []


def test_system_error_handler_summary_says_system_error():
    state = {**BASE_STATE, "error_type": "system_error", "critic_reasoning": "LLM timeout"}
    result = system_error_handler(state)
    assert "System error" in result["summary_md"]


def test_system_error_handler_grounding_rate_none():
    state = {**BASE_STATE, "error_type": "system_error", "critic_reasoning": "LLM timeout"}
    result = system_error_handler(state)
    assert result["grounding_rate"] is None


def test_system_error_handler_includes_reasoning():
    state = {**BASE_STATE, "error_type": "system_error", "critic_reasoning": "Bad JSON after 3 retries"}
    result = system_error_handler(state)
    assert "Bad JSON after 3 retries" in result["summary_md"]


# ---------------------------------------------------------------------------
# Routing logic tests (BUG-005: error vs insufficient)
# ---------------------------------------------------------------------------

def test_route_system_error_takes_priority():
    """system_error route must be chosen even if graded_evidence is also empty."""
    state = {
        "graded_evidence": [],
        "critic_decision": CriticDecision(
            sufficiency="insufficient",
            next_action="refuse",
            magnitude_coverage=0.0,
            reasoning="No evidence.",
        ),
        "error_type": "system_error",
    }
    assert decision_router(state)["router_edge"] == "system_error"


def test_route_insufficient_when_no_error():
    """Critic refusal without error routes to insufficient."""
    state = {
        "graded_evidence": [],
        "critic_decision": CriticDecision(
            sufficiency="insufficient",
            next_action="refuse",
            magnitude_coverage=0.1,
            reasoning="No evidence.",
        ),
        "error_type": None,
    }
    assert decision_router(state)["router_edge"] == "insufficient"


def test_route_insufficient_when_error_type_absent():
    """Missing error_type key still routes refusal to insufficient."""
    state = {
        "graded_evidence": [],
        "critic_decision": CriticDecision(
            sufficiency="insufficient",
            next_action="refuse",
            magnitude_coverage=0.1,
            reasoning="No evidence.",
        ),
    }
    assert decision_router(state)["router_edge"] == "insufficient"


def test_route_judge_when_evidence_present():
    state = {
        "graded_evidence": [{"chunk_id": "c1"}],
        "critic_decision": CriticDecision(
            sufficiency="partial",
            next_action="proceed",
            magnitude_coverage=0.7,
            reasoning="Judge should synthesize.",
        ),
        "error_type": None,
    }
    assert decision_router(state)["router_edge"] == "judge"


def test_critic_failure_sets_error_type(monkeypatch):
    """LLM connection failure must set error_type='system_error' in the returned state."""
    sleeps = []
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", sleeps.append)

    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    llm = FlakyLLM(failures=3)

    result = critic(state, llm=llm)

    assert result["error_type"] == "system_error"
    assert result["graded_evidence"] == []


def test_critic_success_does_not_set_error_type():
    """A successful critic call must not set error_type."""
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    result = critic(state, llm=MockLLM(GOOD_LLM_RESPONSE))
    assert "error_type" not in result


def test_critic_emits_critic_decision_contract():
    state = {**BASE_STATE, "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}

    result = critic(state, llm=MockLLM(GOOD_LLM_RESPONSE))

    assert isinstance(result["critic_decision"], CriticDecision)
    assert result["critic_decision"].sufficiency in {"sufficient", "partial", "insufficient"}
    assert result["critic_decision"].next_action in {"proceed", "expand_macro", "expand_related", "refuse"}
    assert 0.0 <= result["critic_decision"].magnitude_coverage <= 1.0
