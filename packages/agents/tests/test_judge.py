"""Tests for the Judge node — synthesis of attribution causes.

Spec reference: Section 4.3 — Judge Node.
"""
from __future__ import annotations

import json
import copy

import pytest

from catalyst_agents.nodes.judge import (
    judge,
    _format_evidence,
    _parse_judge_response,
    _compute_grounding_rate,
    MAX_CAUSES,
)


# ---------------------------------------------------------------------------
# Mock LLM infrastructure (same pattern as test_critic.py)
# ---------------------------------------------------------------------------

class MockUsage:
    def __init__(self, input_tokens=4000, output_tokens=600):
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


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

JUDGE_RESPONSE = json.dumps({
    "causes": [
        {
            "text": "China chip export ban expanded",
            "category": "geopolitical",
            "confidence": 0.6,
            "evidence_ids": ["c1", "c2"],
            "direction": "negative",
        },
        {
            "text": "Sector-wide selloff in semis",
            "category": "sector",
            "confidence": 0.3,
            "evidence_ids": ["c3"],
            "direction": "negative",
        },
    ],
    "summary_md": "AAPL dropped due to [c1] China export restrictions and [c3] sector selloff.",
    "self_grounding_check": {
        "total_claims": 2,
        "grounded_claims": 2,
        "ungrounded_claims": 0,
    },
})

BASE_STATE = {
    "ticker": "AAPL",
    "trade_date": "2026-01-15",
    "query": None,
    "price_move_pct": -4.2,
    "reranked_chunks": [
        {
            "asset_id": "c1",
            "content_md": "China expanded H20 chip export ban",
            "source_type": "polygon_news",
            "reference_date": "2026-01-15",
        },
        {
            "asset_id": "c2",
            "content_md": "AI stocks under pressure",
            "source_type": "polygon_news",
            "reference_date": "2026-01-15",
        },
        {
            "asset_id": "c3",
            "content_md": "Semiconductor index fell 3%",
            "source_type": "polygon_news",
            "reference_date": "2026-01-15",
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
            "relevance": 0.6,
            "category": "sector",
            "temporal_match": True,
            "reasoning": "Related",
        },
        {
            "chunk_id": "c3",
            "relevance": 0.7,
            "category": "sector",
            "temporal_match": True,
            "reasoning": "Sector context",
        },
    ],
    "model_id": "claude-sonnet-4-20250514",
    "cost_breakdown": [],
    "total_cost_usd": 0.0,
    "total_tokens": 0,
}


# ---------------------------------------------------------------------------
# _format_evidence unit tests
# ---------------------------------------------------------------------------

def test_format_evidence_includes_chunk_ids():
    formatted = _format_evidence(BASE_STATE["graded_evidence"], BASE_STATE["reranked_chunks"])
    assert "c1" in formatted
    assert "c2" in formatted
    assert "c3" in formatted


def test_format_evidence_includes_content_from_reranked():
    """Content must come from reranked_chunks, looked up by asset_id/chunk_id."""
    formatted = _format_evidence(BASE_STATE["graded_evidence"], BASE_STATE["reranked_chunks"])
    assert "China expanded H20 chip export ban" in formatted
    assert "AI stocks under pressure" in formatted
    assert "Semiconductor index fell 3%" in formatted


def test_format_evidence_includes_relevance_scores():
    formatted = _format_evidence(BASE_STATE["graded_evidence"], BASE_STATE["reranked_chunks"])
    assert "0.90" in formatted or "0.9" in formatted


def test_format_evidence_includes_category():
    formatted = _format_evidence(BASE_STATE["graded_evidence"], BASE_STATE["reranked_chunks"])
    assert "geopolitical" in formatted
    assert "sector" in formatted


def test_format_evidence_includes_critic_reasoning():
    formatted = _format_evidence(BASE_STATE["graded_evidence"], BASE_STATE["reranked_chunks"])
    assert "Direct cause" in formatted
    assert "Sector context" in formatted


def test_format_evidence_missing_content_shows_placeholder():
    """If a chunk_id has no matching reranked chunk, a placeholder must appear."""
    graded = [{"chunk_id": "x99", "relevance": 0.8, "category": "macro",
               "temporal_match": True, "reasoning": "test"}]
    formatted = _format_evidence(graded, [])
    assert "[content not found]" in formatted


def test_format_evidence_empty_graded():
    formatted = _format_evidence([], BASE_STATE["reranked_chunks"])
    assert formatted == ""


# ---------------------------------------------------------------------------
# _parse_judge_response unit tests
# ---------------------------------------------------------------------------

def test_parse_judge_response_plain_json():
    parsed = _parse_judge_response(JUDGE_RESPONSE)
    assert "causes" in parsed
    assert "summary_md" in parsed
    assert len(parsed["causes"]) == 2


def test_parse_judge_response_with_fences():
    fenced = f"```json\n{JUDGE_RESPONSE}\n```"
    parsed = _parse_judge_response(fenced)
    assert "causes" in parsed
    assert parsed["causes"][0]["category"] == "geopolitical"


def test_parse_judge_response_with_plain_fences():
    fenced = f"```\n{JUDGE_RESPONSE}\n```"
    parsed = _parse_judge_response(fenced)
    assert "summary_md" in parsed


# ---------------------------------------------------------------------------
# _compute_grounding_rate unit tests
# ---------------------------------------------------------------------------

def test_compute_grounding_rate_all_grounded():
    """All 3 causes cite at least one available evidence chunk — rate = 1.0."""
    causes = [
        {"text": "Cause A", "evidence_ids": ["c1"]},
        {"text": "Cause B", "evidence_ids": ["c2"]},
        {"text": "Cause C", "evidence_ids": ["c3"]},
    ]
    available = {"c1", "c2", "c3"}
    assert _compute_grounding_rate(causes, available) == pytest.approx(1.0)


def test_compute_grounding_rate_partial():
    """1 out of 2 causes grounded → rate = 0.5."""
    causes = [
        {"text": "Cause A", "evidence_ids": ["c1"]},   # grounded
        {"text": "Cause B", "evidence_ids": ["x99"]},  # not grounded
    ]
    available = {"c1", "c2"}
    assert _compute_grounding_rate(causes, available) == pytest.approx(0.5)


def test_compute_grounding_rate_none_grounded():
    """No causes cite available evidence — rate = 0.0."""
    causes = [
        {"text": "Cause A", "evidence_ids": ["x1"]},
        {"text": "Cause B", "evidence_ids": ["x2"]},
    ]
    available = {"c1", "c2"}
    assert _compute_grounding_rate(causes, available) == pytest.approx(0.0)


def test_compute_grounding_rate_empty_causes():
    """Empty cause list returns 0.0, not a ZeroDivisionError."""
    assert _compute_grounding_rate([], {"c1"}) == pytest.approx(0.0)


def test_compute_grounding_rate_cause_with_no_evidence_ids():
    """A cause with an empty evidence_ids list is treated as ungrounded."""
    causes = [
        {"text": "Cause A", "evidence_ids": []},
        {"text": "Cause B", "evidence_ids": ["c1"]},
    ]
    available = {"c1"}
    assert _compute_grounding_rate(causes, available) == pytest.approx(0.5)


def test_compute_grounding_rate_multiple_ids_one_match():
    """If any evidence_id in the list matches, the cause is grounded."""
    causes = [{"text": "Cause A", "evidence_ids": ["x1", "c2", "x3"]}]
    available = {"c2"}
    assert _compute_grounding_rate(causes, available) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# judge() integration tests
# ---------------------------------------------------------------------------

def _fresh_state():
    """Return a deep copy of BASE_STATE to avoid test cross-contamination."""
    return copy.deepcopy(BASE_STATE)


def test_judge_returns_causes_and_summary():
    """Judge must return causes list and summary_md in its output dict."""
    state = _fresh_state()
    result = judge(state, llm=MockLLM(JUDGE_RESPONSE))
    assert "causes" in result
    assert "summary_md" in result
    assert len(result["causes"]) == 2
    assert "AAPL" in result["summary_md"]


def test_judge_limits_to_max_causes():
    """If the LLM returns 7 causes, only MAX_CAUSES (5) must be kept."""
    seven_causes = [
        {
            "text": f"Cause {i}",
            "category": "sector",
            "confidence": 0.1,
            "evidence_ids": ["c1"],
            "direction": "negative",
        }
        for i in range(7)
    ]
    seven_response = json.dumps({
        "causes": seven_causes,
        "summary_md": "Summary with many causes.",
        "self_grounding_check": {"total_claims": 7, "grounded_claims": 7, "ungrounded_claims": 0},
    })
    state = _fresh_state()
    result = judge(state, llm=MockLLM(seven_response))
    assert len(result["causes"]) == MAX_CAUSES


def test_judge_tracks_cost():
    """After judge() runs, a cost entry with node='judge' must appear in state."""
    state = _fresh_state()
    judge(state, llm=MockLLM(JUDGE_RESPONSE))
    assert len(state["cost_breakdown"]) == 1
    assert state["cost_breakdown"][0]["node"] == "judge"
    assert state["total_cost_usd"] > 0


def test_judge_cost_calculation_correct():
    """Verify cost math: claude-sonnet-4 = $3/M input, $15/M output."""
    state = _fresh_state()
    judge(state, llm=MockLLM(JUDGE_RESPONSE))
    # MockUsage: 4000 input, 600 output
    # cost = (4000 * 3.0 + 600 * 15.0) / 1_000_000 = (12000 + 9000) / 1_000_000 = 0.021
    assert abs(state["total_cost_usd"] - 0.021) < 1e-9


def test_judge_computes_grounding_rate():
    """grounding_rate must be computed independently from the LLM self-check."""
    state = _fresh_state()
    result = judge(state, llm=MockLLM(JUDGE_RESPONSE))
    # causes cite c1, c2, c3 — all present in graded_evidence → rate = 1.0
    assert "grounding_rate" in result
    assert result["grounding_rate"] == pytest.approx(1.0)


def test_judge_grounding_rate_partial():
    """Grounding rate is computed from our side, not from the LLM self-check."""
    # Only c1 is in graded_evidence; cause 2 cites c99 which is unknown
    partial_response = json.dumps({
        "causes": [
            {"text": "Known cause", "category": "geopolitical", "confidence": 0.7,
             "evidence_ids": ["c1"], "direction": "negative"},
            {"text": "Fabricated cause", "category": "macro", "confidence": 0.3,
             "evidence_ids": ["c99"], "direction": "neutral"},
        ],
        "summary_md": "Mixed grounding.",
        "self_grounding_check": {"total_claims": 2, "grounded_claims": 2, "ungrounded_claims": 0},
    })
    state = _fresh_state()
    result = judge(state, llm=MockLLM(partial_response))
    # c1 is grounded (in graded_evidence), c99 is not → 0.5
    assert result["grounding_rate"] == pytest.approx(0.5)


def test_judge_returns_grounding_rate_in_output():
    """grounding_rate must appear in the returned dict (merged into state by LangGraph)."""
    state = _fresh_state()
    result = judge(state, llm=MockLLM(JUDGE_RESPONSE))
    assert "grounding_rate" in result
    assert isinstance(result["grounding_rate"], float)


def test_judge_max_causes_constant():
    """MAX_CAUSES must be 5 per spec Section 4.3."""
    assert MAX_CAUSES == 5


def test_judge_cause_fields_preserved():
    """All required cause fields (text, category, confidence, evidence_ids, direction)
    must survive the slice from LLM output to returned causes."""
    state = _fresh_state()
    result = judge(state, llm=MockLLM(JUDGE_RESPONSE))
    cause = result["causes"][0]
    assert "text" in cause
    assert "category" in cause
    assert "confidence" in cause
    assert "evidence_ids" in cause
    assert "direction" in cause
