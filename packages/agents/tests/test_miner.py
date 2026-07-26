from __future__ import annotations

from attribution_fixtures import FixtureRetriever, RecordingCutoffPolicy
from catalyst_agents.nodes.miner import _build_query, _check_magnitude_plausible, _extract_query_ticker, miner
from catalyst_agents.state import OutputStatus


def _state(query=None):
    return {"ticker": "AAPL", "trade_date": "2026-01-15", "query": query, "price_move_pct": None, "corpus_manifest_id": "corpus-fixture-v1"}


def test_build_query_with_nlp_query():
    assert _build_query(_state("Why did Apple drop?")) == "Why did Apple drop?"


def test_build_query_without_query():
    assert _build_query(_state()).startswith("Why did AAPL move")


def test_miner_calls_injected_retriever_with_canonical_cutoff():
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    result = miner(_state("Why did Apple crash after earnings?"), retriever=retriever, cutoff_policy=cutoff_policy)

    assert retriever.calls == [{
        "query": "Why did Apple crash after earnings?",
        "ticker": "AAPL",
        "cutoff": "2026-01-15T21:00:00Z",
        "requested_manifest_id": "corpus-fixture-v1",
        "top_k": 8,
        "candidate_depth": 20,
    }]
    assert cutoff_policy.calls == [("AAPL", "2026-01-15", "attribution")]
    assert len(result["reranked_chunks"]) == 2


def test_miner_projects_all_retrieved_evidence_fields():
    result = miner(_state(), retriever=FixtureRetriever(), cutoff_policy=RecordingCutoffPolicy())
    chunk = result["reranked_chunks"][0]
    for key in {
        "asset_id", "chunk_id", "document_id", "content_text", "available_at",
        "source_class", "ticker_associations", "dedup_cluster_id",
        "cluster_first_available_at", "representative_document_id", "is_novel",
        "lexical_raw_score", "lexical_rank", "corpus_manifest_id",
        "index_manifest_id", "mode_requested", "mode_served", "is_degraded",
        "fallback_reason",
    }:
        assert key in chunk


def test_miner_retriever_exception_becomes_system_error():
    result = miner(_state(), retriever=FixtureRetriever(fail=True), cutoff_policy=RecordingCutoffPolicy())
    assert result["output_status"] == OutputStatus.SYSTEM_ERROR
    assert result["validation_error"] == "retriever_error"
    assert result["retrieved_chunks"] == []


def test_miner_missing_dependencies_are_typed_system_error():
    result = miner(_state())
    assert result["output_status"] == OutputStatus.SYSTEM_ERROR
    assert result["validation_error"] == "missing_retrieval_dependency"


def test_miner_sets_ticker_consistent_false_on_query_ticker_mismatch():
    out = miner(_state("TSLA dropped about 3% on June 12, 2025. Why?"), retriever=FixtureRetriever(), cutoff_policy=RecordingCutoffPolicy())
    assert out["query_ticker_raw"] == "TSLA"
    assert out["ticker_consistent"] is False
    assert out["retrieved_chunks"] == []


def test_extract_query_ticker_uses_known_ticker_not_acronym():
    assert _extract_query_ticker("On March 18, 2025, Microsoft CEO resigned unexpectedly", {"AAPL", "MSFT", "TSLA", "MRNA"}) == "MSFT"


def test_extract_query_ticker_ignores_fda_and_keeps_symbol():
    assert _extract_query_ticker("FDA approved MRNA vaccine update", {"MRNA", "AAPL"}) == "MRNA"


def test_check_magnitude_plausible_extreme():
    assert _check_magnitude_plausible(actual_pct=1.5, claimed_pct=34.0, tolerance=10.0) is False


def test_build_query_empty_string_uses_generated_query():
    assert _build_query(_state("")) == "Why did AAPL move on 2026-01-15?"


def test_miner_skips_retrieval_when_context_session_is_invalid():
    retriever = FixtureRetriever()
    state = {**_state(), "market_session_valid": False}

    result = miner(state, retriever=retriever, cutoff_policy=RecordingCutoffPolicy())

    assert retriever.calls == []
    assert result["retrieved_chunks"] == []
    assert result["reranked_chunks"] == []
    assert result["output_status"] == OutputStatus.ABSTAIN


def test_miner_expansion_delegates_to_injected_retriever():
    retriever = FixtureRetriever()
    state = {**_state(), "current_layer": "macro"}

    result = miner(state, retriever=retriever, cutoff_policy=RecordingCutoffPolicy())

    assert result.get("error_type") is None
    assert retriever.calls[0]["query"].endswith("macro market sector rates policy context")
    assert result["retrieved_chunks"]
