from __future__ import annotations

import pytest

from attribution_fixtures import FixtureRetriever
from catalyst_agents.retrieval.policy import Layer, RetrievalDependencyError, RetrievalMetadata, check_sufficiency, retrieve


def test_retrieve_delegates_to_injected_retriever():
    retriever = FixtureRetriever()
    metadata = RetrievalMetadata(
        ticker="AAPL",
        trade_date="2026-01-15",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="corpus-fixture-v1",
        retriever=retriever,
        top_k=8,
        candidate_depth=20,
    )

    rows = retrieve("query", Layer.DIRECT, metadata)

    assert len(rows) == 2
    assert retriever.calls[0]["cutoff"] == "2026-01-15T21:00:00Z"
    assert metadata.layers_attempted == [Layer.DIRECT]
    assert metadata.stop_reason == "sufficiency_reached"


def test_retrieve_requires_injected_retriever():
    metadata = RetrievalMetadata(ticker="AAPL", trade_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    with pytest.raises(RetrievalDependencyError):
        retrieve("query", Layer.DIRECT, metadata)


def test_retrieve_requires_cutoff():
    metadata = RetrievalMetadata(ticker="AAPL", trade_date="2026-01-15", retriever=FixtureRetriever())
    with pytest.raises(RetrievalDependencyError):
        retrieve("query", Layer.DIRECT, metadata)


def test_related_layer_delegates_without_sql_fallback():
    retriever = FixtureRetriever()
    metadata = RetrievalMetadata(
        ticker="AAPL",
        trade_date="2026-01-15",
        cutoff="2026-01-15T21:00:00Z",
        retriever=retriever,
    )

    rows = retrieve("related query", Layer.RELATED, metadata)

    assert rows
    assert retriever.calls[0]["query"] == "related query"
    assert metadata.layers_attempted == [Layer.RELATED]


def test_check_sufficiency_count_only_for_delegate_facade():
    assert check_sufficiency([], min_count=1) is False
    assert check_sufficiency([{"chunk_id": "c1"}], min_count=1) is True
