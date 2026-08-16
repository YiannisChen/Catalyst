from __future__ import annotations

def test_agent_adapter_preserves_typed_provenance():
    from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
    from catalyst_data.retrieval.result import RetrievalFilters, RetrievalResult

    filters = RetrievalFilters(
        ticker="AAPL", requested_manifest_id="a" * 64,
        cutoff="2026-01-15T21:00:00Z",
    )
    result = RetrievalResult(
        chunk_id="chunk:1", document_id="doc", available_at="2026-01-01T00:00:00Z",
        cutoff=filters.cutoff, content_text="evidence", filters_applied=filters,
        source_class="reported_news", lexical_raw_score=None, fusion_rank=1,
        fusion_score=0.123,
        corpus_manifest_id=filters.requested_manifest_id, index_manifest_id="1" * 64,
        mode_requested="hybrid", mode_served="hybrid", is_degraded=False,
        timing_ms=1.0,
    )

    class FakeProductionRetriever:
        def retrieve(self, query, **kwargs):
            assert query == "why"
            assert kwargs["requested_manifest_id"] == "a" * 64
            return [result]

    evidence = AgentRetrieverAdapter(FakeProductionRetriever()).retrieve(
        "why", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="a" * 64, top_k=8, candidate_depth=20,
    )
    assert evidence[0].ticker_associations == ("AAPL",)
    assert evidence[0].representative_document_id == "doc"
    assert evidence[0].index_manifest_id == "1" * 64
    assert evidence[0].mode_served == "hybrid"
    assert evidence[0].fusion_score == 0.123


def test_agent_adapter_preserves_reranker_provenance():
    """reranker_score/reranker_rank must survive the adapter boundary."""
    from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
    from catalyst_data.retrieval.result import RetrievalFilters, RetrievalResult

    filters = RetrievalFilters(
        ticker="AAPL", requested_manifest_id="a" * 64,
        cutoff="2026-01-15T21:00:00Z",
    )
    result = RetrievalResult(
        chunk_id="chunk:1", document_id="doc", available_at="2026-01-01T00:00:00Z",
        cutoff=filters.cutoff, content_text="evidence", filters_applied=filters,
        source_class="reported_news", lexical_raw_score=None,
        fusion_score=0.05, reranker_score=0.97, reranker_rank=1,
        corpus_manifest_id=filters.requested_manifest_id, index_manifest_id="1" * 64,
        mode_requested="reranked", mode_served="reranked", is_degraded=False,
        timing_ms=1.0,
    )

    class FakeProductionRetriever:
        def retrieve(self, query, **kwargs):
            return [result]

    evidence = AgentRetrieverAdapter(FakeProductionRetriever()).retrieve(
        "why", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="a" * 64, top_k=8, candidate_depth=20,
    )
    assert evidence[0].reranker_score == 0.97
    assert evidence[0].reranker_rank == 1


def test_agent_adapter_preserves_temporal_conflict_identity():
    """Wave 2/3 evidence path must surface temporal conflict fields via adapter."""
    from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
    from catalyst_data.retrieval.result import RetrievalFilters, RetrievalResult

    filters = RetrievalFilters(
        ticker="AAPL", requested_manifest_id="a" * 64,
        cutoff="2026-01-15T21:00:00Z",
    )
    result = RetrievalResult(
        chunk_id="chunk:1", document_id="doc", available_at="2026-01-01T00:00:00Z",
        cutoff=filters.cutoff, content_text="evidence", filters_applied=filters,
        source_class="reported_news", lexical_raw_score=None, fusion_rank=1,
        fusion_score=0.123,
        corpus_manifest_id=filters.requested_manifest_id, index_manifest_id="1" * 64,
        mode_requested="hybrid", mode_served="hybrid", is_degraded=False,
        timing_ms=1.0,
        temporal_center_date="2026-01-15",
        query_date="2025-07-24",
        query_date_conflict=True,
        query_date_decision="structured_ignore_query",
    )

    class FakeProductionRetriever:
        def retrieve(self, query, **kwargs):
            return [result]

    evidence = AgentRetrieverAdapter(FakeProductionRetriever()).retrieve(
        "Why did AAPL move on 2025-07-24?", ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="a" * 64, top_k=8, candidate_depth=20,
    )
    assert evidence[0].temporal_center_date == "2026-01-15"
    assert evidence[0].query_date == "2025-07-24"
    assert evidence[0].query_date_conflict is True
    assert evidence[0].query_date_decision == "structured_ignore_query"


def test_miner_evidence_persists_temporal_conflict_fields():
    """Miner chunk evidence must include temporal conflict identity for Wave 2/3."""
    from catalyst_agents.attribution.provider import RetrievedEvidence
    from catalyst_agents.nodes.miner import _evidence_to_chunk, miner
    from attribution_fixtures import FixtureRetriever, RecordingCutoffPolicy

    item = RetrievedEvidence(
        chunk_id="c1", document_id="d1", content_text="evidence",
        available_at="2026-01-15T18:00:00Z", source_class="issuer_disclosure",
        ticker_associations=("AAPL",), dedup_cluster_id=None,
        cluster_first_available_at="2026-01-15T18:00:00Z",
        representative_document_id="d1", is_novel=False,
        lexical_raw_score=-2.0, lexical_rank=1,
        corpus_manifest_id="corpus-fixture-v1", index_manifest_id="index-fixture-v1",
        mode_requested="reranked", mode_served="reranked", is_degraded=False,
        fallback_reason=None, fusion_score=0.05,
        reranker_score=0.97, reranker_rank=1,
        temporal_center_date="2026-01-15",
        query_date="2025-07-24",
        query_date_conflict=True,
        query_date_decision="structured_ignore_query",
    )
    chunk = _evidence_to_chunk(item)
    assert chunk["temporal_center_date"] == "2026-01-15"
    assert chunk["query_date"] == "2025-07-24"
    assert chunk["query_date_conflict"] is True
    assert chunk["query_date_decision"] == "structured_ignore_query"
