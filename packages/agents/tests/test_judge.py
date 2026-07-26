from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from catalyst_agents.nodes.judge import MAX_CAUSES, _compute_grounding_rate, _format_evidence, _parse_judge_response, judge
from catalyst_agents.state import OutputStatus


class MockUsage:
    input_tokens = 4000
    output_tokens = 600
    total_tokens = 4600


class MockResponse:
    def __init__(self, content):
        self.content = content
        self.usage = MockUsage()


class MockLLM:
    def __init__(self, response_content):
        self._content = response_content
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return MockResponse(self._content)


def judge_response(**overrides):
    draft = {
        "cause_label": "earnings_guidance",
        "direction": "negative",
        "transmission_mechanism": "Guidance weakness reduced expectations",
        "supporting_evidence_ids": ["c1"],
        "counter_evidence_ids": [],
        "missing_evidence": [],
        "change_condition": "Reassess if guidance improves",
        "facts": ["Guidance was reduced"],
        "calculations": [],
        "inferences": ["Investors priced lower forward revenue"],
        "unavailable_evidence": [],
    }
    draft.update(overrides)
    return json.dumps({"hypotheses": [draft], "summary_md": "AAPL dropped after [c1] guidance weakness."})


BASE_STATE = {
    "ticker": "AAPL",
    "trade_date": "2026-01-15",
    "query": None,
    "price_move_pct": -4.2,
    "reranked_chunks": [{"asset_id": "c1", "content_md": "Guidance cut", "source_type": "issuer_disclosure", "reference_date": "2026-01-15"}],
    "graded_evidence": [{"chunk_id": "c1", "relevance": 0.9, "category": "earnings", "temporal_match": True, "reasoning": "Direct"}],
    "model_id": "claude-sonnet-4-20250514",
    "cost_breakdown": [],
    "total_cost_usd": 0.0,
    "cost_status": "known",
    "total_tokens": 0,
}


def _fresh_state():
    return copy.deepcopy(BASE_STATE)


def test_parse_judge_response_accepts_hypothesis_draft_schema():
    parsed = _parse_judge_response(judge_response())
    assert parsed["hypotheses"][0]["cause_label"] == "earnings_guidance"


def test_parse_judge_response_rejects_extra_self_reported_fields():
    payload = json.loads(judge_response())
    payload["hypotheses"][0]["prerequisite_gate_passed"] = True
    with pytest.raises(ValidationError):
        _parse_judge_response(json.dumps(payload))


def test_parse_judge_response_rejects_legacy_confidence_causes_schema():
    legacy = json.dumps({"causes": [{"text": "x", "category": "macro", "confidence": 0.5, "evidence_ids": ["c1"], "direction": "negative"}], "summary_md": "x"})
    with pytest.raises(ValidationError):
        _parse_judge_response(legacy)


def test_judge_returns_hypothesis_drafts_and_compat_causes_without_confidence():
    state = _fresh_state()
    result = judge(state, llm=MockLLM(judge_response()))
    assert result["hypothesis_drafts"][0]["cause_label"] == "earnings_guidance"
    assert result["causes"][0]["evidence_ids"] == ["c1"]
    assert "confidence" not in result["causes"][0]


def test_judge_empty_evidence_returns_abstain_without_llm_call():
    state = {**_fresh_state(), "graded_evidence": []}
    llm = MockLLM(judge_response())
    result = judge(state, llm=llm)
    assert result["output_status"] == OutputStatus.ABSTAIN
    assert llm.calls == 0


def test_judge_limits_to_max_causes():
    payload = json.loads(judge_response())
    payload["hypotheses"] = payload["hypotheses"] * 7
    result = judge(_fresh_state(), llm=MockLLM(json.dumps(payload)))
    assert len(result["hypothesis_drafts"]) == MAX_CAUSES


def test_judge_tracks_cost():
    state = _fresh_state()
    judge(state, llm=MockLLM(judge_response()))
    assert state["cost_breakdown"][0]["node"] == "judge"
    assert state["total_cost_usd"] == pytest.approx(0.021)


def test_compute_grounding_rate_uses_supporting_evidence_ids():
    causes = [{"supporting_evidence_ids": ["c1"]}, {"supporting_evidence_ids": ["missing"]}]
    assert _compute_grounding_rate(causes, {"c1"}) == pytest.approx(0.5)


@pytest.mark.parametrize("fence", ["```json\n{}\n```", "```\n{}\n```"])
def test_parse_judge_response_accepts_markdown_fences(fence):
    parsed = _parse_judge_response(fence.format(judge_response()))
    assert parsed["hypotheses"][0]["cause_label"] == "earnings_guidance"


def test_format_evidence_preserves_critic_metadata_and_content():
    formatted = _format_evidence(BASE_STATE["graded_evidence"], BASE_STATE["reranked_chunks"])
    assert "c1" in formatted
    assert "Guidance cut" in formatted
    assert "0.90" in formatted
    assert "earnings" in formatted
    assert "Direct" in formatted
    assert _format_evidence([], BASE_STATE["reranked_chunks"]) == ""
    assert "[content not found]" in _format_evidence(
        [{"chunk_id": "missing", "relevance": 0.8, "category": "macro", "temporal_match": True, "reasoning": "x"}],
        [],
    )


@pytest.mark.parametrize(
    ("causes", "available", "expected"),
    [
        ([], {"c1"}, 0.0),
        ([{"supporting_evidence_ids": []}], {"c1"}, 0.0),
        ([{"supporting_evidence_ids": ["c1", "missing"]}], {"c1"}, 1.0),
        ([{"supporting_evidence_ids": ["c1"]}, {"supporting_evidence_ids": ["missing"]}], {"c1"}, 0.5),
        ([{"supporting_evidence_ids": ["missing"]}], {"c1"}, 0.0),
    ],
)
def test_compute_grounding_rate_edge_cases(causes, available, expected):
    assert _compute_grounding_rate(causes, available) == pytest.approx(expected)


def test_judge_retries_transient_failure_then_returns_raw_response(monkeypatch):
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)

    class FlakyLLM(MockLLM):
        def invoke(self, prompt):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("transient")
            return MockResponse(self._content)

    llm = FlakyLLM(judge_response())
    result = judge(_fresh_state(), llm=llm)
    assert llm.calls == 3
    assert result["judge_raw_llm_response"] == judge_response()


def test_judge_invalid_json_exhausts_retries_and_abstains(monkeypatch):
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)
    llm = MockLLM("not-json")
    result = judge(_fresh_state(), llm=llm)
    assert llm.calls == 3
    assert result["output_status"] == OutputStatus.ABSTAIN
