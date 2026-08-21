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
    result_set = RetrievalResultSet(
        hits=(_text_hit(),),
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
    )
    restored = RetrievalResultSet.model_validate_json(result_set.model_dump_json())
    assert restored == result_set
    hit = restored.hits[0]
    assert hit.chunk_id == "corpus:chunk:0001"
    assert hit.fact_id is None
    assert hit.evidence_id == hit.chunk_id
    assert hit.temporal_identity.session_date == "2026-01-06"
    assert restored.temporal_identity == _temporal()
    assert restored.data_runtime_identity == _runtime()


def _state_item(hit: RetrievalHit, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "evidence_id": hit.evidence_id,
        "canonical_asset_id": hit.canonical_asset_id,
        "canonical_content_version_id": hit.content_version_id,
        "corpus_document_id": hit.corpus_document_id,
        "chunk_id": hit.chunk_id,
        "fact_id": hit.fact_id,
        "section_key": None,
        "chunk_ordinal": None,
        "asset_type": "NEWS" if hit.chunk_id is not None else "STRUCTURED_CONTEXT",
        "provider": hit.provider,
        "publisher": hit.publisher,
        "canonical_url": None,
        "source_class": hit.source_class,
        "evidence_role": "INDEPENDENT_REPORT"
        if hit.source_class == "reported_news"
        else "STRUCTURED_CONTEXT",
        "eligible_at": hit.eligible_at,
        "temporal_precision": "published_utc",
        "content_state": hit.content_state,
        "material_capability": "MATERIAL_CAPABLE",
        "serving_status": "active",
        "parse_quality": hit.parse_quality,
        "independence_group_id": None,
        "independence_status": "UNKNOWN",
        "content_hash": None,
        "text_ref": hit.evidence_id,
        "excerpt_text": hit.excerpt,
        "first_seen_round": 1,
        "contributing_task_ids": ("task-1",),
        "retrieval_contributions": (),
    }
    base.update(overrides)
    return base


def _state_for(hit: RetrievalHit) -> EvidenceState:
    from catalyst_agents.attribution.evidence_state import EvidenceState

    return EvidenceState(
        schema_version="1.0",
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        research_policy_version="rp:v1",
        task_results=(),
        evidence_items=(EvidenceStateItem(**_state_item(hit)),),
        structured_facts=(),
        degradations=(),
        capability_gaps=(),
        state_hash="e" * 64,
    )


def test_agents_consumes_chunk_id_and_fact_id_unchanged() -> None:
    from catalyst_agents.attribution.evidence_state import (
        EvidenceStateItem,
    )

    hit = _text_hit()
    item = EvidenceStateItem(**_state_item(hit))
    assert item.chunk_id == hit.chunk_id
    assert item.evidence_id == hit.evidence_id
    assert item.fact_id is None  # structured property absent for text items
    assert item.independence_status == "UNKNOWN"


def test_structured_hit_fact_id_flows_to_agents_unchanged() -> None:
    from catalyst_agents.attribution.evidence_state import EvidenceStateItem

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
    item = EvidenceStateItem(**_state_item(hit, independence_status="KNOWN_GROUP"))
    assert item.evidence_id == "fact:42"
    assert item.chunk_id is None
    assert item.fact_id == "fact:42"  # structured identity: evidence_id == fact_id
