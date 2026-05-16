from __future__ import annotations

import json
from pathlib import Path

from catalyst_agents.nodes.critic import _parse_critic_response, _build_critic_decision


def test_parse_critic_response_accepts_v2_subscores():
    payload = {
        "graded_chunks": [
            {
                "chunk_id": "c1",
                "relevance": 0.72,
                "category": "sector",
                "temporal_match": True,
                "reasoning": "event-specific",
                "event_specificity": 0.8,
                "temporal_alignment": 0.7,
                "evidence_granularity": 0.6,
                "conflict_signal": 0.7,
            }
        ],
        "reasoning": "ok",
    }
    out = _parse_critic_response(json.dumps(payload))
    assert out["graded_chunks"][0]["event_specificity"] == 0.8


def test_parse_critic_response_accepts_minimal_v2_payload_without_subscores():
    payload = {
        "graded_chunks": [
            {
                "chunk_id": "c1",
                "relevance": 0.72,
                "category": "sector",
                "temporal_match": True,
                "reasoning": "specific event with timing match",
            }
        ],
        "reasoning": "ok",
    }
    out = _parse_critic_response(json.dumps(payload))
    assert out["graded_chunks"][0]["chunk_id"] == "c1"
    assert out["graded_chunks"][0].get("event_specificity") is None


def test_temporal_penalty_helper_exists():
    import catalyst_agents.nodes.critic as critic_mod

    assert hasattr(critic_mod, "_apply_temporal_penalty_and_filter")


def test_critic_prompt_keeps_rubric_text():
    text = Path("packages/agents/catalyst_agents/prompts/critic.md").read_text(encoding="utf-8")
    assert "Grading Rubric" in text
    assert "Event specificity" in text
    assert "pure fundamentals table" in text


def test_critic_prompt_output_format_does_not_require_subscore_fields():
    text = Path("packages/agents/catalyst_agents/prompts/critic.md").read_text(encoding="utf-8")
    assert "`event_specificity`" not in text
    assert "`temporal_alignment`" not in text
    assert "`evidence_granularity`" not in text
    assert "`conflict_signal`" not in text


def test_single_high_confidence_evidence_can_be_sufficient():
    filtered = [{"chunk_id": "c1", "relevance": 0.85, "temporal_match": True, "category": "sector"}]
    decision = _build_critic_decision(filtered, "ok")
    assert decision.sufficiency == "sufficient"
