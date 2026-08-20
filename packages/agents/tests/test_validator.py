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

    # Failed-gate drafts are never published: only the compatible draft remains.
    assert set(by_cause) == {"earnings_guidance"}
    assert by_cause["earnings_guidance"]["prerequisite_gate_passed"] is True
    assert "unexplained" not in by_cause


def test_structured_market_direction_mismatch_fails_gate():
    state = _state()
    state["context_artifact"]["market_component"] = -1.0
    state["hypothesis_drafts"][0].update({
        "cause_label": "market",
        "direction": "positive",
        "supporting_evidence_ids": [],
    })

    result = validator(state)

    # The failed-gate draft is dropped from publication and cannot reach
    # SUFFICIENT; the run abstains instead of emitting an ungrounded cause.
    assert result["hypotheses"] == []
    assert result["causes"] == []
    assert result["output_status"] == OutputStatus.ABSTAIN


# ── AMEND-6: failed-gate drafts are never published ───────────────────────────

def test_published_hypotheses_and_causes_never_include_failed_gate_drafts():
    """A published/final cause list must never contain a hypothesis whose
    prerequisite gate failed."""
    state = _state()
    state["hypothesis_drafts"].append({
        **state["hypothesis_drafts"][0],
        "cause_label": "product_demand",
        "transmission_mechanism": "Demand weakness after the guidance cut",
    })

    result = validator(state)

    published = result["hypotheses"]
    assert published
    assert all(item["prerequisite_gate_passed"] is True for item in published)
    published_causes = {item["cause_label"] for item in published}
    assert published_causes == {"earnings_guidance"}
    published_categories = {item["category"] for item in result["causes"]}
    assert published_categories == {"earnings_guidance"}


def test_compatible_and_incompatible_draft_on_same_chunk_keeps_compatible_only():
    """If Judge drafts a compatible cause (earnings_guidance) and an
    incompatible cause (product_demand) on the same earnings-category chunk,
    the published set keeps only the compatible draft, its prerequisite gates
    pass, and SUFFICIENT remains possible."""
    state = _state()
    state["hypothesis_drafts"].append({
        **state["hypothesis_drafts"][0],
        "cause_label": "product_demand",
        "transmission_mechanism": "Demand weakness after the guidance cut",
    })

    result = validator(state)

    assert [h["cause_label"] for h in result["hypotheses"]] == ["earnings_guidance"]
    assert result["output_status"] == OutputStatus.SUFFICIENT


def test_incompatible_only_drafts_cannot_become_sufficient():
    """A draft whose only cause is incompatible with the chunk's critic
    category must be dropped; the run cannot publish it or reach SUFFICIENT."""
    state = _state()
    state["hypothesis_drafts"] = [{
        **state["hypothesis_drafts"][0],
        "cause_label": "product_demand",
        "transmission_mechanism": "Demand weakness after the guidance cut",
    }]

    result = validator(state)

    assert result["hypotheses"] == []
    assert result["causes"] == []
    assert result["output_status"] == OutputStatus.ABSTAIN


def test_published_gate_results_pass_assurance_prerequisite_gates():
    """gate_results derived from the published hypotheses (exactly as the
    trace writer builds them) must pass the prerequisite_gates assurance
    check even when the Judge drafted an incompatible cause on the same
    earnings-category chunk."""
    from attribution_fixtures import mock_run_artifacts
    from catalyst_agents.runtime.assurance.checks import run_all_checks

    state = _state()
    state["hypothesis_drafts"].append({
        **state["hypothesis_drafts"][0],
        "cause_label": "product_demand",
        "transmission_mechanism": "Demand weakness after the guidance cut",
    })

    result = validator(state)
    gate_results = [
        (h["cause_label"], h["prerequisite_gate_passed"], h["prerequisite_gate_reason"])
        for h in result["hypotheses"]
    ]
    artifacts = mock_run_artifacts(gate_results=gate_results, citations=["c1"])
    by_name = {check.check_name: check.status for check in run_all_checks("run-001", artifacts)}
    assert by_name["prerequisite_gates"] == "pass"


# ── AMEND-7: failed-gate causal text never survives in the public summary ─────

def test_all_filtered_drafts_use_canonical_non_causal_abstain_summary():
    """When every draft fails prerequisite gates the public summary must be the
    canonical non-causal ABSTAIN text, never the Judge's causal summary."""
    state = _state()
    state["hypothesis_drafts"] = [{
        **state["hypothesis_drafts"][0],
        "cause_label": "product_demand",
        "transmission_mechanism": "Demand weakness after the guidance cut",
    }]
    state["summary_md"] = "AAPL dropped after [c1] demand weakness."

    result = validator(state)

    assert result["hypotheses"] == []
    assert result["causes"] == []
    assert result["output_status"] == OutputStatus.ABSTAIN
    # The leaked causal assertion must be gone from the public summary.
    assert "demand weakness" not in result["summary_md"]
    assert "AAPL dropped after [c1]" not in result["summary_md"]
    # The canonical ABSTAIN wording is present.
    assert "abstain" in result["summary_md"].lower()
    # The post-filter metric must not inherit the Judge's pre-filter value.
    assert result["grounding_rate"] == 0.0


def test_partial_filter_summary_mentions_only_surviving_hypotheses():
    """When only some drafts pass, the public summary must be derived only
    from the surviving hypotheses and must not mention discarded drafts."""
    state = _state()
    state["hypothesis_drafts"].append({
        **state["hypothesis_drafts"][0],
        "cause_label": "product_demand",
        "transmission_mechanism": "Demand weakness after the guidance cut",
    })
    state["summary_md"] = "AAPL dropped after [c1] guidance weakness and demand weakness."

    result = validator(state)

    assert [h["cause_label"] for h in result["hypotheses"]] == ["earnings_guidance"]
    assert result["output_status"] == OutputStatus.SUFFICIENT
    # Discarded hypothesis text and its causal statement are absent.
    assert "demand" not in result["summary_md"].lower()
    assert "AAPL dropped after [c1] guidance weakness and demand weakness" not in result["summary_md"]
    # The surviving hypothesis and its evidence are present.
    assert "earnings_guidance" in result["summary_md"]
    assert "[c1]" in result["summary_md"]
    # The post-filter metric is recomputed from the published cause set.
    assert result["grounding_rate"] == 1.0
