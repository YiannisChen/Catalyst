"""Tests for retrieval policy Layer 1 + Layer 2 behavior."""
from __future__ import annotations

from pathlib import Path

import pytest

from catalyst_agents.retrieval.policy import (
    Layer,
    MAX_EXPANSIONS,
    MAX_LAYERS_P0,
    RetrievalMetadata,
    check_sufficiency,
    retrieve,
)
from tests.fixtures.retrieval_fixture import build_retrieval_fixture


def _metadata(tmp_path: Path) -> RetrievalMetadata:
    db_path = build_retrieval_fixture(tmp_path)
    return RetrievalMetadata(
        ticker="AAPL",
        trade_date="2026-01-15",
        date_range=("2026-01-12", "2026-01-18"),
        db_path=db_path,
        top_k=8,
    )


def test_retrieve_direct_sql_fallback_filters_by_ticker_and_window(tmp_path):
    metadata = _metadata(tmp_path)

    results = retrieve("Why did Apple move?", Layer.DIRECT, metadata, rerank=None)

    assert [row["ticker"] for row in results] == ["AAPL", "AAPL"]
    assert all("2026-01-12" <= row["reference_date"] <= "2026-01-18" for row in results)
    assert all(row["source_type"] in {"polygon_news", "fmp_fundamentals"} for row in results)
    assert metadata.hit_counts_per_layer[Layer.DIRECT] == 2


def test_retrieve_macro_sql_fallback_drops_strict_ticker_filter(tmp_path):
    metadata = _metadata(tmp_path)

    results = retrieve("What macro factors moved tech?", Layer.MACRO, metadata, rerank=None)

    tickers = {row["ticker"] for row in results}
    assert {"MSFT", "SPY", "NVDA"} <= tickers
    assert "AAPL" not in tickers
    assert metadata.hit_counts_per_layer[Layer.MACRO] == 3


def test_retrieve_related_raises_not_implemented(tmp_path):
    metadata = _metadata(tmp_path)

    with pytest.raises(NotImplementedError, match="layer3_not_implemented"):
        retrieve("related entities", Layer.RELATED, metadata, rerank=None)


def test_retrieval_metadata_tracks_bounds_and_attempts(tmp_path):
    metadata = _metadata(tmp_path)

    retrieve("direct query", Layer.DIRECT, metadata, rerank=None)
    retrieve("macro query", Layer.MACRO, metadata, rerank=None)

    assert metadata.layers_attempted == [Layer.DIRECT, Layer.MACRO]
    assert metadata.max_layers == MAX_LAYERS_P0
    assert metadata.max_expansions == MAX_EXPANSIONS
    assert metadata.total_unique_evidence == 5


def test_retrieve_uses_lancedb_path_when_available(tmp_path, monkeypatch):
    metadata = _metadata(tmp_path)
    metadata.lancedb_dir = tmp_path / "lancedb_gold" / "eval_frozen"
    metadata.lancedb_dir.mkdir(parents=True)
    metadata.table = object()
    metadata.embedding_fn = lambda text: [0.1, 0.2]

    captured = {}

    def fake_hybrid_search(table, query, ticker=None, date_range=None, top_k=20, embedding_fn=None):
        captured["table"] = table
        captured["query"] = query
        captured["ticker"] = ticker
        captured["date_range"] = date_range
        return [
            {
                "asset_id": "vec1",
                "ticker": "AAPL",
                "source_type": "polygon_news",
                "reference_date": "2026-01-15",
                "content_md": "Vector result",
                "rrf_score": 0.9,
            }
        ]

    monkeypatch.setattr(
        "catalyst_agents.retrieval.policy.hybrid_search",
        fake_hybrid_search,
    )

    results = retrieve("Why did Apple move?", Layer.DIRECT, metadata, rerank=None)

    assert results[0]["asset_id"] == "vec1"
    assert captured["table"] is metadata.table
    assert captured["ticker"] == "AAPL"


def test_retrieve_respects_top_k_limit_in_sql_fallback(tmp_path):
    metadata = _metadata(tmp_path)
    metadata.top_k = 1

    results = retrieve("Why did Apple move?", Layer.DIRECT, metadata, rerank=None)

    assert len(results) == 1


def test_retrieve_with_reranker_uses_locked_top_k(tmp_path, monkeypatch):
    metadata = _metadata(tmp_path)
    metadata.lancedb_dir = tmp_path / "lancedb_gold" / "eval_frozen"
    metadata.lancedb_dir.mkdir(parents=True)
    metadata.table = object()
    metadata.embedding_fn = lambda text: [0.1, 0.2]
    metadata.top_k = 20

    def fake_hybrid_search(table, query, ticker=None, date_range=None, top_k=20, embedding_fn=None):  # noqa: ARG001
        return [
            {
                "asset_id": f"vec{i}",
                "ticker": "AAPL",
                "source_type": "polygon_news",
                "reference_date": "2026-01-15",
                "content_md": f"Vector result {i}",
                "rrf_score": 1.0 / (60 + i),
            }
            for i in range(20)
        ]

    class MockReranker:
        def compute_score(self, pairs):
            return list(range(len(pairs)))

    monkeypatch.setattr("catalyst_agents.retrieval.policy.hybrid_search", fake_hybrid_search)

    reranked = retrieve("Why did Apple move?", Layer.DIRECT, metadata, rerank=MockReranker())

    assert len(reranked) == 8
    assert all("rerank_score" in row for row in reranked)


def test_check_sufficiency_passes_when_count_and_mean_rrf_meet_threshold():
    chunks = [
        {"asset_id": f"a{i}", "rrf_score": 0.03}
        for i in range(5)
    ]
    assert check_sufficiency(chunks, min_count=5, min_mean_score=0.02) is True


def test_check_sufficiency_fails_when_count_or_mean_rrf_below_threshold():
    too_few = [{"asset_id": "a1", "rrf_score": 0.5}]
    low_mean = [{"asset_id": f"a{i}", "rrf_score": 0.001} for i in range(6)]

    assert check_sufficiency(too_few, min_count=5, min_mean_score=0.02) is False
    assert check_sufficiency(low_mean, min_count=5, min_mean_score=0.02) is False


def test_retrieve_marks_stop_reason_based_on_sufficiency(tmp_path, monkeypatch):
    metadata = _metadata(tmp_path)

    sufficient_rows = [
        {
            "asset_id": f"a{i}",
            "ticker": "AAPL",
            "source_type": "polygon_news",
            "reference_date": "2026-01-15",
            "content_md": f"row{i}",
            "rrf_score": 0.03,
        }
        for i in range(6)
    ]
    insufficient_rows = [
        {
            "asset_id": f"b{i}",
            "ticker": "AAPL",
            "source_type": "polygon_news",
            "reference_date": "2026-01-15",
            "content_md": f"row{i}",
            "rrf_score": 0.001,
        }
        for i in range(6)
    ]

    monkeypatch.setattr(
        "catalyst_agents.retrieval.policy._sql_fallback_query",
        lambda layer, md: sufficient_rows,  # noqa: ARG005
    )
    retrieve("query", Layer.DIRECT, metadata, rerank=None)
    assert metadata.stop_reason == "sufficiency_reached"

    monkeypatch.setattr(
        "catalyst_agents.retrieval.policy._sql_fallback_query",
        lambda layer, md: insufficient_rows,  # noqa: ARG005
    )
    retrieve("query", Layer.MACRO, metadata, rerank=None)
    assert metadata.stop_reason == "expansions_exhausted"
