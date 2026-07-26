"""Regression tests tied to the documented failure taxonomy."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from attribution_fixtures import FixtureRetriever, RecordingCutoffPolicy, mock_provider_with_ohlcv
from catalyst_agents.graph import build_attribution_graph
from catalyst_agents.state import OutputStatus


MOCK_CHUNKS = [
    {
        "asset_id": "c1",
        "ticker": "AAPL",
        "source_type": "polygon_news",
        "reference_date": "2026-01-15",
        "content_md": "China export restrictions pressured Apple shares.",
        "rrf_score": 0.5,
    }
]


class MockUsage:
    def __init__(self) -> None:
        self.input_tokens = 3000
        self.output_tokens = 500
        self.total_tokens = 3500


class MockResponse:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = MockUsage()


def _base_state() -> dict:
    return {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": None,
        "price_move_pct": -4.2,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "graded_evidence": [],
        "critic_reasoning": "",
        "critic_decision": None,
        "causes": [],
        "summary_md": "",
        "grounding_rate": None,
        "output_status": None,
        "validation_error": None,
        "validator_attempts": 0,
        "phase": None,
        "router_edge": None,
        "router_reason": None,
        "expansions_used": 0,
        "max_expansions": 2,
        "current_layer": "direct",
        "retrieval_metadata": None,
        "cost_breakdown": [],
        "total_cost_usd": 0.0,
        "cost_status": "known",
        "total_tokens": 0,
        "model_id": "claude-sonnet-4-20250514",
        "error_type": None,
    }


def _mock_retrieve(query, layer, metadata, *, rerank=None):
    return MOCK_CHUNKS[: metadata.top_k]


# class: consistency_failure
def test_consistency_failure_downgrades_to_partial_and_is_traced(tmp_path: Path, monkeypatch):
    trace_db = tmp_path / "consistency_trace.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(trace_db))

    critic_response = json.dumps(
        {
            "graded_chunks": [
                {
                    "chunk_id": "c1",
                    "relevance": 0.9,
                    "category": "geopolitical",
                    "temporal_match": True,
                    "reasoning": "Directly relevant.",
                }
            ],
            "reasoning": "Strong evidence.",
        }
    )
    judge_response = json.dumps(
        {
            "hypotheses": [
                {
                    "cause_label": "earnings_guidance",
                    "direction": "negative",
                    "transmission_mechanism": "Guidance weakness reduced expectations",
                    "supporting_evidence_ids": ["missing-evidence-id"],
                    "counter_evidence_ids": [],
                    "missing_evidence": [],
                    "change_condition": "Reassess if guidance improves",
                    "facts": ["Guidance was reduced"],
                    "calculations": [],
                    "inferences": ["Investors priced lower forward revenue"],
                    "unavailable_evidence": [],
                }
            ],
            "summary_md": "AAPL fell because of [missing-evidence-id].",
        }
    )

    class MockLLM:
        def __init__(self) -> None:
            self._responses = [critic_response, judge_response]
            self._calls = 0

        def invoke(self, prompt: str) -> MockResponse:
            idx = min(self._calls, len(self._responses) - 1)
            self._calls += 1
            return MockResponse(self._responses[idx])

    graph = build_attribution_graph(
        context_provider=mock_provider_with_ohlcv(),
        retriever=FixtureRetriever(),
        cutoff_policy=RecordingCutoffPolicy(),
        requested_manifest_id="corpus-fixture-v1",
        use_critic=True,
        llm=MockLLM(),
    )
    result = graph.invoke(_base_state())

    conn = sqlite3.connect(trace_db)
    validator_rows = conn.execute(
        "SELECT node, error_message, status_after FROM trace_events WHERE run_id = ? AND node = 'validator'",
        (result["run_id"],),
    ).fetchall()
    conn.close()

    assert result["output_status"] == OutputStatus.ABSTAIN
    assert result["validation_error"] == "evidence_id_missing"
    assert result["validator_attempts"] == 1
    assert validator_rows
    assert validator_rows[-1][2] == "ABSTAIN"


# class: model_failure
def test_model_failure_routes_to_system_error_and_traces_error_type(tmp_path: Path, monkeypatch):
    trace_db = tmp_path / "model_failure_trace.db"
    monkeypatch.setenv("CATALYST_TRACE_DB_PATH", str(trace_db))
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)

    class FailingLLM:
        def invoke(self, prompt: str) -> MockResponse:
            raise RuntimeError("llm unavailable")

    graph = build_attribution_graph(
        context_provider=mock_provider_with_ohlcv(),
        retriever=FixtureRetriever(),
        cutoff_policy=RecordingCutoffPolicy(),
        requested_manifest_id="corpus-fixture-v1",
        use_critic=True,
        llm=FailingLLM(),
    )
    result = graph.invoke(_base_state())

    conn = sqlite3.connect(trace_db)
    error_rows = conn.execute(
        "SELECT node, error_type FROM trace_events WHERE run_id = ? AND error_type IS NOT NULL ORDER BY event_seq",
        (result["run_id"],),
    ).fetchall()
    conn.close()

    assert result["output_status"] == OutputStatus.SYSTEM_ERROR
    assert error_rows
    assert any(error_type == "system_error" for _node, error_type in error_rows)
