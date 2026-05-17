"""Tests for the Miner node — deterministic retrieval, no LLM call.

Spec reference: Section 4.3 — Miner Node.
"""
from __future__ import annotations

import pytest

from catalyst_agents.nodes.miner import miner, _build_query, _compute_date_range
from catalyst_agents.retrieval.policy import Layer


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------

def test_build_query_with_nlp_query():
    state = {"ticker": "AAPL", "trade_date": "2026-01-15", "query": "Why did Apple drop?"}
    assert _build_query(state) == "Why did Apple drop?"


def test_build_query_without_query():
    state = {"ticker": "AAPL", "trade_date": "2026-01-15", "query": None}
    q = _build_query(state)
    assert "AAPL" in q
    assert "2026-01-15" in q


def test_build_query_empty_string_treated_as_missing():
    """Empty string query should fall back to the generated query."""
    state = {"ticker": "TSLA", "trade_date": "2026-03-10", "query": ""}
    q = _build_query(state)
    assert "TSLA" in q
    assert "2026-03-10" in q


def test_compute_date_range():
    start, end = _compute_date_range("2026-01-15", window_days=3)
    assert start == "2026-01-12"
    assert end == "2026-01-18"


def test_compute_date_range_default_window():
    """Default window is DATE_WINDOW_DAYS = 3."""
    start, end = _compute_date_range("2026-06-01")
    assert start == "2026-05-29"
    assert end == "2026-06-04"


def test_compute_date_range_month_boundary():
    start, end = _compute_date_range("2026-03-01", window_days=3)
    assert start == "2026-02-26"
    assert end == "2026-03-04"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOCK_CHUNKS = [
    {
        "asset_id": f"a{i}",
        "ticker": "AAPL",
        "source_type": "polygon_news",
        "reference_date": "2026-01-15",
        "content_md": f"Content {i}",
        "rrf_score": 1.0 / (60 + i),
    }
    for i in range(1, 21)
]


class MockReranker:
    """Returns descending scores: first pair gets the highest score."""

    def compute_score(self, pairs):
        return [20 - i for i in range(len(pairs))]


def _make_retrieve_mock(chunks=None):
    if chunks is None:
        chunks = MOCK_CHUNKS

    def mock_retrieve(query, layer, metadata, *, rerank=None):
        return chunks[: metadata.top_k]

    return mock_retrieve


# ---------------------------------------------------------------------------
# Miner integration tests (mocked dependencies)
# ---------------------------------------------------------------------------

def test_miner_returns_retrieved_and_reranked(monkeypatch):
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", _make_retrieve_mock())

    state = {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": None,
        "price_move_pct": -4.2,
    }
    result = miner(state, table=None, embedding_fn=None, reranker=MockReranker())

    assert len(result["retrieved_chunks"]) == 20
    assert len(result["reranked_chunks"]) == 8


def test_miner_without_reranker_uses_rrf_order(monkeypatch):
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", _make_retrieve_mock())

    state = {"ticker": "AAPL", "trade_date": "2026-01-15", "query": None, "price_move_pct": None}
    result = miner(state, table=None, embedding_fn=None, reranker=None)

    assert len(result["reranked_chunks"]) == 8
    # Without reranker, RRF order from hybrid_search is preserved
    assert result["reranked_chunks"][0]["asset_id"] == "a1"


def test_miner_with_reranker_reorders(monkeypatch):
    """MockReranker returns [20, 19, 18, ...] — first chunk keeps highest score."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", _make_retrieve_mock())

    state = {"ticker": "AAPL", "trade_date": "2026-01-15", "query": None, "price_move_pct": None}
    result = miner(state, table=None, embedding_fn=None, reranker=MockReranker())

    assert result["reranked_chunks"][0]["asset_id"] == "a1"


def test_miner_reranker_assigns_scores(monkeypatch):
    """Each chunk in retrieved_chunks must have a rerank_score after reranking."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", _make_retrieve_mock())

    state = {"ticker": "AAPL", "trade_date": "2026-01-15", "query": None, "price_move_pct": None}
    result = miner(state, table=None, embedding_fn=None, reranker=MockReranker())

    for chunk in result["retrieved_chunks"]:
        assert "rerank_score" in chunk


def test_miner_empty_retrieval(monkeypatch):
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", lambda query, layer, metadata, *, rerank=None: [])

    state = {"ticker": "AAPL", "trade_date": "2026-01-15", "query": None, "price_move_pct": None}
    result = miner(state, table=None, embedding_fn=None, reranker=None)

    assert result["retrieved_chunks"] == []
    assert result["reranked_chunks"] == []


def test_miner_empty_retrieval_with_reranker_skipped(monkeypatch):
    """With an empty result set, reranker should never be called."""
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", lambda query, layer, metadata, *, rerank=None: [])

    class FailingReranker:
        def compute_score(self, pairs):
            raise RuntimeError("Should not be called for empty retrieval")

    state = {"ticker": "AAPL", "trade_date": "2026-01-15", "query": None, "price_move_pct": None}
    result = miner(state, table=None, embedding_fn=None, reranker=FailingReranker())

    assert result["retrieved_chunks"] == []
    assert result["reranked_chunks"] == []


def test_miner_fewer_than_top_k_results(monkeypatch):
    """If retrieval returns fewer than TOP_K_RERANKED chunks, reranked list is capped at len."""
    small_chunks = MOCK_CHUNKS[:3]
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", _make_retrieve_mock(small_chunks))

    state = {"ticker": "AAPL", "trade_date": "2026-01-15", "query": None, "price_move_pct": None}
    result = miner(state, table=None, embedding_fn=None, reranker=None)

    assert len(result["retrieved_chunks"]) == 3
    assert len(result["reranked_chunks"]) == 3


def test_miner_uses_nlp_query_for_search(monkeypatch):
    """The query passed to retrieve should match the NLP query from state."""

    captured_query = {}

    def capturing_search(query, layer, metadata, *, rerank=None):
        captured_query["value"] = query
        return MOCK_CHUNKS[: metadata.top_k]

    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", capturing_search)

    state = {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": "Why did Apple crash after earnings?",
        "price_move_pct": -5.0,
    }
    miner(state, table=None, embedding_fn=None, reranker=None)

    assert captured_query["value"] == "Why did Apple crash after earnings?"


def test_miner_date_range_passed_to_search(monkeypatch):
    """The ±3-day date_range must be forwarded to retrieval metadata."""

    captured = {}

    def capturing_search(query, layer, metadata, *, rerank=None):
        captured["date_range"] = metadata.date_range
        captured["ticker"] = metadata.ticker
        return MOCK_CHUNKS[: metadata.top_k]

    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", capturing_search)

    state = {"ticker": "AAPL", "trade_date": "2026-01-15", "query": None, "price_move_pct": None}
    miner(state, table=None, embedding_fn=None, reranker=None)

    assert captured["date_range"] == ("2026-01-12", "2026-01-18")
    assert captured["ticker"] == "AAPL"


def test_miner_reranker_scalar_score_handled(monkeypatch):
    """If reranker.compute_score returns a scalar (single pair), it must be wrapped."""
    single_chunk = [MOCK_CHUNKS[0]]
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", _make_retrieve_mock(single_chunk))

    class ScalarReranker:
        def compute_score(self, pairs):
            # Some reranker implementations return a bare float for single pairs
            assert len(pairs) == 1
            return 0.95  # scalar, not a list

    state = {"ticker": "AAPL", "trade_date": "2026-01-15", "query": None, "price_move_pct": None}
    result = miner(state, table=None, embedding_fn=None, reranker=ScalarReranker())

    assert len(result["reranked_chunks"]) == 1
    assert result["reranked_chunks"][0]["rerank_score"] == pytest.approx(0.95)


def test_miner_uses_macro_layer_when_requested(monkeypatch):
    captured = {}

    def fake_retrieve(query, layer, metadata, *, rerank=None):
        captured["query"] = query
        captured["layer"] = layer
        captured["ticker"] = metadata.ticker
        return MOCK_CHUNKS[:4]

    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", fake_retrieve)

    state = {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": "What macro factors moved Apple?",
        "price_move_pct": -4.2,
        "current_layer": Layer.MACRO,
    }
    result = miner(state, table=None, embedding_fn=None, reranker=None)

    assert captured["query"] == "What macro factors moved Apple?"
    assert captured["layer"] == Layer.MACRO
    assert captured["ticker"] == "AAPL"
    assert len(result["retrieved_chunks"]) == 4


def test_miner_sets_ticker_consistent_false_on_query_ticker_mismatch(monkeypatch):
    monkeypatch.setattr("catalyst_agents.nodes.miner.retrieve", _make_retrieve_mock())
    state = {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": "APPL dropped about 3% on June 12, 2025. Why?",
        "price_move_pct": None,
    }
    out = miner(state, table=None, embedding_fn=None, reranker=None)
    assert out["query_ticker_raw"] == "APPL"
    assert out["ticker_consistent"] is False
