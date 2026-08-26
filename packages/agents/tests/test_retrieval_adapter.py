from __future__ import annotations

def test_agent_adapter_preserves_typed_provenance():
    """The V1.1 adapter preserves ticker/representative/index/mode/fusion."""
    from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
    from catalyst_data.retrieval.v1_result import RetrievalResultSet, StageRank, StageScore

    hit = _v1_hit(
        scores=(StageScore(stage="fusion", value=0.123), StageScore(stage="reranked", value=0.5)),
        ranks=(StageRank(stage="fusion", value=1), StageRank(stage="reranked", value=1)),
    )
    result_set = RetrievalResultSet(
        hits=(hit,),
        temporal_identity=hit.temporal_identity,
        data_runtime_identity=hit.data_runtime_identity,
    )

    class FakeProductionRetriever:
        def retrieve(self, query, **kwargs):
            assert query == "why"
            assert kwargs["requested_manifest_id"] == "a" * 64
            return result_set

    evidence = AgentRetrieverAdapter(FakeProductionRetriever()).retrieve(
        "why", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="a" * 64, top_k=8, candidate_depth=20,
        temporal_identity=hit.temporal_identity,
    )
    assert evidence[0].ticker_associations == ("NVDA",)
    assert evidence[0].representative_document_id == hit.corpus_document_id
    assert evidence[0].index_manifest_id == hit.data_runtime_identity.dense_index_version
    assert evidence[0].mode_served == "reranked"
    assert evidence[0].fusion_score == 0.123


def test_agent_adapter_preserves_reranker_provenance():
    """reranker_score/reranker_rank must survive the V1.1 adapter boundary."""
    from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
    from catalyst_data.retrieval.v1_result import RetrievalResultSet, StageRank, StageScore

    hit = _v1_hit(
        scores=(StageScore(stage="reranked", value=0.97),),
        ranks=(StageRank(stage="reranked", value=1),),
    )
    result_set = RetrievalResultSet(
        hits=(hit,),
        temporal_identity=hit.temporal_identity,
        data_runtime_identity=hit.data_runtime_identity,
    )

    class FakeProductionRetriever:
        def retrieve(self, query, **kwargs):
            return result_set

    evidence = AgentRetrieverAdapter(FakeProductionRetriever()).retrieve(
        "why", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="a" * 64, top_k=8, candidate_depth=20,
        temporal_identity=hit.temporal_identity,
    )
    assert evidence[0].reranker_score == 0.97
    assert evidence[0].reranker_rank == 1


def test_agent_adapter_preserves_temporal_identity():
    """The exact validated TemporalIdentity passes through the V1.1 adapter."""
    from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
    from catalyst_data.retrieval.v1_result import RetrievalResultSet

    hit = _v1_hit()
    result_set = RetrievalResultSet(
        hits=(hit,),
        temporal_identity=hit.temporal_identity,
        data_runtime_identity=hit.data_runtime_identity,
    )

    class FakeProductionRetriever:
        def retrieve(self, query, **kwargs):
            return result_set

    evidence = AgentRetrieverAdapter(FakeProductionRetriever()).retrieve(
        "Why did AAPL move on 2025-07-24?", ticker="AAPL",
        cutoff="2026-01-06T21:00:00Z",
        requested_manifest_id="a" * 64, top_k=8, candidate_depth=20,
        temporal_identity=hit.temporal_identity,
    )
    assert evidence[0].temporal_identity == hit.temporal_identity
    assert evidence[0].temporal_center_date == "2026-01-06"


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


# ---------------------------------------------------------------------------
# M4-0: adapter consumes the V1.1 RetrievalHit contract
# ---------------------------------------------------------------------------


def _v1_hit(**overrides):
    from datetime import datetime, timezone

    from catalyst_data.canonical.identity import DataRuntimeIdentity
    from catalyst_data.canonical.temporal import TemporalIdentity
    from catalyst_data.retrieval.v1_result import RetrievalHit, StageRank, StageScore

    temporal = TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=datetime(2026, 1, 6, 14, 30, tzinfo=timezone.utc),
        session_close_at=datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc),
        information_window_start_at=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc),
    )
    runtime = DataRuntimeIdentity(
        data_snapshot_id="snapshot:7a004",
        corpus_manifest_id="a" * 64,
        fts_index_version="build:fts",
        dense_index_version="1" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )
    base = dict(
        evidence_id="chunk:1",
        canonical_asset_id="v1:asset:1",
        content_version_id="v1:content:1",
        corpus_document_id="doc:1",
        chunk_id="chunk:1",
        excerpt="NVDA raised guidance at its earnings call.",
        scores=(StageScore(stage="reranked", value=0.97),),
        ranks=(StageRank(stage="reranked", value=1),),
        source_class="reported_news",
        content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc),
        ticker_scope=("NVDA",),
        provider="polygon",
        publisher="Example Wire",
        dedup_cluster_id="v1:dedup:1",
        parse_quality="full",
        retrieval_policy_version="qp:v1",
        temporal_identity=temporal,
        data_runtime_identity=runtime,
        section_key="body",
        chunk_ordinal=1,
        asset_type="NEWS",
        content_hash="c" * 64,
        material_capability="MATERIAL_CAPABLE",
        serving_status="body_candidate",
        temporal_precision="publication_time",
        independence_group_id="v1:ind:1",
        canonical_url="https://example.test/nvda",
        evidence_role="INDEPENDENT_REPORT",
    )
    base.update(overrides)
    return RetrievalHit(**base)


def test_adapter_consumes_v1_hits_with_full_metadata():
    """AgentRetrieverAdapter consumes the V1.1 hit contract and emits evidence
    with the complete data-owned metadata; no hardcoded AAPL/provider values."""
    from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
    from catalyst_data.retrieval.v1_result import RetrievalResultSet

    hit = _v1_hit()
    result_set = RetrievalResultSet(
        hits=(hit,),
        temporal_identity=hit.temporal_identity,
        data_runtime_identity=hit.data_runtime_identity,
    )

    class FakeV1Retriever:
        def retrieve(self, query, **kwargs):
            assert query == "why"
            assert kwargs["requested_manifest_id"] == "a" * 64
            assert kwargs["temporal_identity"] is hit.temporal_identity
            return result_set

    evidence = AgentRetrieverAdapter(FakeV1Retriever()).retrieve(
        "why", ticker="NVDA", cutoff="2026-01-06T21:00:00Z",
        requested_manifest_id="a" * 64, top_k=8, candidate_depth=20,
        temporal_identity=hit.temporal_identity,
    )
    assert len(evidence) == 1
    item = evidence[0]
    assert item.chunk_id == "chunk:1"
    assert item.canonical_asset_id == "v1:asset:1"
    assert item.canonical_content_version_id == "v1:content:1"
    assert item.corpus_document_id == "doc:1"
    assert item.section_key == "body"
    assert item.chunk_ordinal == 1
    assert item.asset_type == "NEWS"
    assert item.content_hash == "c" * 64
    assert item.material_capability == "MATERIAL_CAPABLE"
    assert item.serving_status == "body_candidate"
    assert item.temporal_precision == "publication_time"
    assert item.independence_group_id == "v1:ind:1"
    assert item.independence_status == "KNOWN_GROUP"
    assert item.canonical_url == "https://example.test/nvda"
    assert item.publisher == "Example Wire"
    assert item.evidence_role == "INDEPENDENT_REPORT"
    assert item.provider == "polygon"
    assert item.ticker_associations == ("NVDA",)
    assert item.temporal_identity == hit.temporal_identity
    assert item.data_runtime_identity == hit.data_runtime_identity


def test_adapter_derives_independence_status_and_role_from_data():
    """Unknown lineage and source-role ceiling derive from the hit data."""
    from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
    from catalyst_data.retrieval.v1_result import RetrievalResultSet

    hit = _v1_hit(
        independence_group_id=None,
        source_class="analysis_opinion",
        evidence_role="COMMENTARY_LEAD",
        content_state="TITLE_ONLY",
        material_capability="LEAD_ONLY",
    )
    result_set = RetrievalResultSet(
        hits=(hit,),
        temporal_identity=hit.temporal_identity,
        data_runtime_identity=hit.data_runtime_identity,
    )

    class FakeV1Retriever:
        def retrieve(self, query, **kwargs):
            return result_set

    evidence = AgentRetrieverAdapter(FakeV1Retriever()).retrieve(
        "why", ticker="NVDA", cutoff="2026-01-06T21:00:00Z",
        requested_manifest_id="a" * 64, temporal_identity=hit.temporal_identity,
    )
    item = evidence[0]
    assert item.independence_status == "UNKNOWN"
    assert item.evidence_role == "COMMENTARY_LEAD"
    assert item.material_capability == "LEAD_ONLY"


def test_adapter_v1_evidence_builds_evidence_state_item():
    """Adapter output builds EvidenceStateItem with all M4-0 metadata intact."""
    from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
    from catalyst_data.retrieval.v1_result import RetrievalResultSet

    hit = _v1_hit()
    result_set = RetrievalResultSet(
        hits=(hit,),
        temporal_identity=hit.temporal_identity,
        data_runtime_identity=hit.data_runtime_identity,
    )

    class FakeV1Retriever:
        def retrieve(self, query, **kwargs):
            return result_set

    evidence = AgentRetrieverAdapter(FakeV1Retriever()).retrieve(
        "why", ticker="NVDA", cutoff="2026-01-06T21:00:00Z",
        requested_manifest_id="a" * 64, temporal_identity=hit.temporal_identity,
    )
    state_item = evidence[0].to_evidence_state_item(
        first_seen_round=1, contributing_task_ids=("research:1:0:abc",)
    )
    assert state_item.evidence_id == "chunk:1"
    assert state_item.canonical_asset_id == "v1:asset:1"
    assert state_item.canonical_content_version_id == "v1:content:1"
    assert state_item.corpus_document_id == "doc:1"
    assert state_item.section_key == "body"
    assert state_item.chunk_ordinal == 1
    assert state_item.asset_type == "NEWS"
    assert state_item.content_hash == "c" * 64
    assert state_item.material_capability == "MATERIAL_CAPABLE"
    assert state_item.serving_status == "body_candidate"
    assert state_item.temporal_precision == "publication_time"
    assert state_item.independence_group_id == "v1:ind:1"
    assert state_item.independence_status == "KNOWN_GROUP"
    assert state_item.canonical_url == "https://example.test/nvda"
    assert state_item.publisher == "Example Wire"
    assert state_item.evidence_role == "INDEPENDENT_REPORT"
    assert state_item.first_seen_round == 1
    assert state_item.contributing_task_ids == ("research:1:0:abc",)
    assert state_item.eligible_at == hit.eligible_at
