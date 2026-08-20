"""Cross-package V1.1 retrieval fixture boundary tests (M2-11).

A data-core V1.1 RetrievalResultSet fixture serializes losslessly and agents
consumes chunk_id/fact_id unchanged (Final Migration TSD §5.2).
"""
from __future__ import annotations

from datetime import datetime, timezone

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval.v1_result import RetrievalHit, RetrievalResultSet
from catalyst_data.trading_calendar import session_close_utc


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _temporal() -> TemporalIdentity:
    monday_close = _utc(session_close_utc("2026-01-05"))
    tuesday_close = _utc(session_close_utc("2026-01-06"))
    return TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-06T14:30:00Z"),
        session_close_at=tuesday_close,
        information_window_start_at=monday_close,
        cutoff_at=tuesday_close,
    )


def _runtime() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="snapshot:7a004",
        corpus_manifest_id="c" * 64,
        fts_index_version="fts:v3",
        query_policy_version="qp:v1",
    )


def _text_hit() -> RetrievalHit:
    return RetrievalHit(
        evidence_id="corpus:chunk:0001",
        canonical_asset_id="issuer:AAPL:news:0001",
        content_version_id="content:v1:0001",
        corpus_document_id="corpus:doc:0001",
        chunk_id="corpus:chunk:0001",
        fact_id=None,
        excerpt="AAPL reported record quarterly revenue.",
        scores={"lexical": 12.5, "dense": None, "fusion": None, "reranked": None},
        ranks={"lexical": 1, "dense": None, "fusion": None, "reranked": None},
        source_class="reported_news",
        content_state="FULL_TEXT",
        eligible_at=_utc("2026-01-05T21:05:00Z"),
        ticker_scope=("AAPL",),
        provider="polygon",
        publisher="example-news",
        parse_quality="full",
        retrieval_policy_version="qp:v1",
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
    )


def test_v1_retrieval_set_serializes_losslessly_across_packages() -> None:
    result_set = RetrievalResultSet(hits=(_text_hit(),))
    restored = RetrievalResultSet.model_validate_json(result_set.model_dump_json())
    assert restored == result_set
    hit = restored.hits[0]
    assert hit.chunk_id == "corpus:chunk:0001"
    assert hit.fact_id is None
    assert hit.evidence_id == hit.chunk_id
    assert hit.temporal_identity.session_date == "2026-01-06"


def test_agents_consumes_chunk_id_and_fact_id_unchanged() -> None:
    from catalyst_agents.attribution.evidence_state import (
        EvidenceState,
        EvidenceStateItem,
    )

    hit = _text_hit()
    state = EvidenceState().upsert(
        EvidenceStateItem(
            evidence_id=hit.evidence_id,
            first_seen_round=1,
            research_task_ids=("task-1",),
            state="accepted",
            canonical_asset_id=hit.canonical_asset_id,
            content_version_id=hit.content_version_id,
            chunk_id=hit.chunk_id,
            fact_id=hit.fact_id,
            content_state=hit.content_state,
            source_class=hit.source_class,
            independence_group=None,
            dedup_cluster_id=hit.dedup_cluster_id,
        )
    )
    item = state.items[0]
    assert item.chunk_id == hit.chunk_id
    assert item.evidence_id == hit.evidence_id
    assert item.fact_id == hit.fact_id


def test_structured_hit_fact_id_flows_to_agents_unchanged() -> None:
    from catalyst_agents.attribution.evidence_state import (
        EvidenceState,
        EvidenceStateItem,
    )

    hit = RetrievalHit(
        evidence_id="fact:42",
        canonical_asset_id="issuer:AAPL:struct:1",
        content_version_id="content:v1:0001",
        corpus_document_id="corpus:doc:0001",
        chunk_id=None,
        fact_id="fact:42",
        excerpt="AAPL P/E 32.1",
        scores={"structured": 1.0},
        ranks={"structured": 1},
        source_class="structured_market_data",
        content_state="FULL_TEXT",
        eligible_at=_utc("2026-01-05T21:05:00Z"),
        ticker_scope=("AAPL",),
        provider="fmp",
        publisher=None,
        parse_quality="full",
        retrieval_policy_version="qp:v1",
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
    )
    state = EvidenceState().upsert(
        EvidenceStateItem(
            evidence_id=hit.evidence_id,
            first_seen_round=1,
            research_task_ids=("task-1",),
            state="accepted",
            canonical_asset_id=hit.canonical_asset_id,
            content_version_id=hit.content_version_id,
            chunk_id=hit.chunk_id,
            fact_id=hit.fact_id,
            content_state=hit.content_state,
            source_class=hit.source_class,
            independence_group=None,
            dedup_cluster_id=hit.dedup_cluster_id,
        )
    )
    assert state.items[0].fact_id == "fact:42"
    assert state.items[0].chunk_id is None
