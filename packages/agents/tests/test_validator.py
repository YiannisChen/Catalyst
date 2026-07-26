from __future__ import annotations

from attribution_fixtures import RecordingCutoffPolicy
from catalyst_agents.nodes.validator import validator
from catalyst_agents.state import OutputStatus


class _RepairResponse:
    content = ""
    usage_metadata = {"input_tokens": 10, "output_tokens": 5}


class _RepairLLM:
    def __init__(self, payload):
        import json
        self.payload = json.dumps(payload)
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        response = _RepairResponse()
        response.content = self.payload
        return response


def _state():
    return {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "cutoff": "2026-01-15T21:00:00Z",
        "context_artifact": {"benchmark_return_pct": -1.0, "sector_return_pct": -2.0},
        "reranked_chunks": [
            {
                "asset_id": "c1",
                "document_id": "d1",
                "available_at": "2026-01-15T18:00:00Z",
                "content_md": "Guidance cut",
                "source_class": "reported_news",
                "ticker_associations": ("AAPL",),
                "dedup_cluster_id": "cluster-1",
                "is_novel": True,
                "corpus_manifest_id": "corpus-fixture-v1",
                "index_manifest_id": "index-fixture-v1",
            }
        ],
        "graded_evidence": [{"chunk_id": "c1", "relevance": 0.9, "category": "earnings", "temporal_match": True, "reasoning": "Direct"}],
        "hypothesis_drafts": [
            {
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
        ],
        "summary_md": "AAPL dropped after [c1] guidance weakness.",
    }


def test_validator_computes_gate_and_ranking_fields_from_draft():
    result = validator(_state())
    h = result["hypotheses"][0]
    assert h["prerequisite_gate_passed"] is True
    assert h["direct_support_exists"] is True
    assert h["independent_supporting_cluster_count"] == 1
    assert h["max_supporting_critic_relevance"] == 0.9
    assert h["is_novel"] is True
    assert result["output_status"] == OutputStatus.SUFFICIENT


def test_validator_resolves_missing_evidence_id_to_abstain():
    state = _state()
    state["hypothesis_drafts"][0]["supporting_evidence_ids"] = ["missing"]
    result = validator(state)
    assert result["validation_error"] == "evidence_id_missing"
    assert result["output_status"] == OutputStatus.ABSTAIN


def test_validator_detects_cutoff_violation():
    state = _state()
    state["reranked_chunks"][0]["available_at"] = "2026-01-15T22:00:00Z"
    result = validator(state)
    assert result["validation_error"] == "cutoff_violation"
    assert result["output_status"] == OutputStatus.ABSTAIN


def test_validator_uses_injected_cutoff_policy_once():
    policy = RecordingCutoffPolicy()
    result = validator(_state(), cutoff_policy=policy)
    assert policy.calls == [("AAPL", "2026-01-15", "attribution")]
    assert result["output_status"] == OutputStatus.SUFFICIENT


def test_validator_legacy_schema_failure_can_repair_at_most_once():
    state = _state()
    state["hypothesis_drafts"] = []
    state["causes"] = [{"text": "bad", "category": "macro", "confidence": 1.5, "evidence_ids": ["missing"], "direction": "negative"}]
    result = validator(state, llm=None)
    assert result["validator_attempts"] == 0
    assert result["output_status"] == OutputStatus.PARTIAL


def test_validator_repairs_invalid_hypothesis_once():
    state = _state()
    state.update({
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "cost_status": "known",
        "total_tokens": 0,
        "model_id": "claude-sonnet-4-20250514",
    })
    state["hypothesis_drafts"][0]["supporting_evidence_ids"] = ["missing"]
    repaired = {"hypotheses": _state()["hypothesis_drafts"], "summary_md": "Repaired [c1]."}
    llm = _RepairLLM(repaired)

    result = validator(state, llm=llm)

    assert llm.calls == 1
    assert result["validator_attempts"] == 1
    assert result["repair_count"] == 1
    assert result["output_status"] == OutputStatus.SUFFICIENT


def test_unexplained_gate_fails_when_an_explanatory_gate_passes():
    state = _state()
    unexplained = {
        **state["hypothesis_drafts"][0],
        "cause_label": "unexplained",
        "supporting_evidence_ids": [],
        "transmission_mechanism": "No supported explanation",
    }
    state["hypothesis_drafts"].append(unexplained)

    result = validator(state)
    by_cause = {item["cause_label"]: item for item in result["hypotheses"]}

    assert by_cause["earnings_guidance"]["prerequisite_gate_passed"] is True
    assert by_cause["unexplained"]["prerequisite_gate_passed"] is False


def test_structured_market_direction_mismatch_fails_gate():
    state = _state()
    state["context_artifact"]["market_component"] = -1.0
    state["hypothesis_drafts"][0].update({
        "cause_label": "market",
        "direction": "positive",
        "supporting_evidence_ids": [],
    })

    result = validator(state)
    hypothesis = result["hypotheses"][0]

    assert hypothesis["prerequisite_gate_passed"] is False
    assert "direction_mismatch" in hypothesis["validation_violations"]
