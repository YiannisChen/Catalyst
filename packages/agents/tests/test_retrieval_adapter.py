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
