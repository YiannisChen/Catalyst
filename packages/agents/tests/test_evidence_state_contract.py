"""V1.1 EvidenceState contract tests (M2-4, corrective).

Phase 3 TSD §9: canonical EvidenceState carries no Analyst dispositions; full
canonical/content/document/chunk/fact, materiality, eligibility, role,
independence, excerpt/ref and retrieval-contribution fields; cumulative union
preserves first_seen_round and fails closed on conflicting immutable metadata.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import SourceClass
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.trading_calendar import session_close_utc

from catalyst_agents.attribution.evidence_state import (
    EvidenceState,
    EvidenceStateItem,
    ResearchTaskResult,
    RetrievalContribution,
    compute_state_hash,
)


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


def _contribution(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": "task-1",
        "requested_mode": "hybrid",
        "served_mode": "hybrid",
        "lexical_rank": 1,
        "lexical_score": 12.5,
        "dense_rank": None,
        "dense_score": None,
        "fusion_rank": 1,
        "fusion_score": 7.25,
        "reranker_rank": None,
        "reranker_score": None,
        "original_rank": 1,
        "degradation_flags": (),
    }
    base.update(overrides)
    return base


def _item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "evidence_id": "corpus:chunk:0001",
        "canonical_asset_id": "issuer:AAPL:news:0001",
        "canonical_content_version_id": "content:v1:0001",
        "corpus_document_id": "corpus:doc:0001",
        "chunk_id": "corpus:chunk:0001",
        "section_key": None,
        "chunk_ordinal": None,
        "asset_type": "NEWS",
        "provider": "polygon",
        "publisher": "example-news",
        "canonical_url": None,
        "source_class": "reported_news",
        "evidence_role": "INDEPENDENT_REPORT",
        "eligible_at": _utc("2026-01-05T21:05:00Z"),
        "temporal_precision": "published_utc",
        "content_state": "FULL_TEXT",
        "material_capability": "MATERIAL_CAPABLE",
        "serving_status": "active",
        "parse_quality": "full",
        "independence_group_id": "syndication:g1",
        "independence_status": "KNOWN_GROUP",
        "content_hash": "d" * 64,
        "text_ref": "corpus:doc:0001#0",
        "excerpt_text": "AAPL reported record quarterly revenue.",
        "first_seen_round": 1,
        "contributing_task_ids": ("task-1",),
        "retrieval_contributions": (_contribution(),),
    }
    base.update(overrides)
    return base


def _state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": "run:1",
        "round": 1,
        "temporal_identity": _temporal(),
        "data_runtime_identity": _runtime(),
        "research_policy_version": "rp:v1",
        "task_results": (),
        "evidence_items": (_item(),),
        "structured_facts": (),
        "degradations": (),
        "capability_gaps": (),
        "state_hash": "e" * 64,
    }
    base.update(overrides)
    return base


def test_evidence_state_item_is_strict_and_frozen() -> None:
    item = EvidenceStateItem(**_item())
    assert item.evidence_id == item.chunk_id
    assert item.independence_status == "KNOWN_GROUP"
    with pytest.raises(ValidationError):
        item.evidence_id = "other"  # frozen
    with pytest.raises(ValidationError):
        EvidenceStateItem(**_item(), unknown_field=True)  # extra forbidden


def test_evidence_state_has_no_analyst_dispositions() -> None:
    """Canonical EvidenceState must not carry accepted/lead/rejected state;
    Analyst judgments live in EvidenceAssessment (Phase 3 §9)."""
    assert "state" not in EvidenceStateItem.model_fields
    assert "accepted" not in EvidenceStateItem.model_fields
    assert "lead" not in EvidenceStateItem.model_fields
    assert "rejected" not in EvidenceStateItem.model_fields
    with pytest.raises(ValidationError):
        EvidenceStateItem(**_item(state="accepted"))


def test_evidence_state_item_requires_exactly_one_of_chunk_or_fact() -> None:
    with pytest.raises(ValidationError):
        EvidenceStateItem(**_item(fact_id="fact:1"))
    with pytest.raises(ValidationError):
        EvidenceStateItem(
            **_item(
                evidence_id="fact:1",
                chunk_id=None,
                fact_id=None,
            )
        )


def test_retrieval_contribution_rejects_nan_and_non_positive_ranks() -> None:
    with pytest.raises(ValidationError):
        RetrievalContribution(**_contribution(lexical_score=float("nan")))
    with pytest.raises(ValidationError):
        RetrievalContribution(**_contribution(lexical_rank=0))
    with pytest.raises(ValidationError):
        RetrievalContribution(**_contribution(lexical_rank=-2))


def test_same_chunk_merges_contributions_and_keeps_all_task_ids() -> None:
    state = EvidenceState(**_state())
    merged = state.upsert(
        EvidenceStateItem(
            **_item(
                contributing_task_ids=("task-2",),
                retrieval_contributions=(_contribution(task_id="task-2"),),
            )
        )
    )
    assert len(merged.evidence_items) == 1
    assert set(merged.evidence_items[0].contributing_task_ids) == {"task-1", "task-2"}
    assert {c.task_id for c in merged.evidence_items[0].retrieval_contributions} == {
        "task-1",
        "task-2",
    }


def test_same_chunk_across_rounds_keeps_first_seen_round() -> None:
    state = EvidenceState(**_state())
    merged = state.upsert(EvidenceStateItem(**_item(first_seen_round=2)))
    assert merged.evidence_items[0].first_seen_round == 1


def test_multiple_chunks_from_one_article_stay_separate() -> None:
    state = EvidenceState(**_state())
    state = state.upsert(
        EvidenceStateItem(
            **_item(
                evidence_id="corpus:chunk:0002",
                chunk_id="corpus:chunk:0002",
                text_ref="corpus:doc:0001#1",
            )
        )
    )
    assert len(state.evidence_items) == 2


def test_syndicated_assets_keep_common_group_and_separate_ids() -> None:
    state = EvidenceState(**_state())
    state = state.upsert(
        EvidenceStateItem(
            **_item(
                evidence_id="corpus:chunk:0002",
                chunk_id="corpus:chunk:0002",
                text_ref="corpus:doc:0001#1",
            )
        )
    )
    assert len(state.evidence_items) == 2
    assert state.evidence_items[0].independence_group_id == "syndication:g1"
    assert state.evidence_items[1].independence_group_id == "syndication:g1"
    assert state.evidence_items[0].evidence_id != state.evidence_items[1].evidence_id


def test_conflicting_immutable_metadata_fails_integrity_validation() -> None:
    state = EvidenceState(**_state())
    with pytest.raises(ValueError):
        state.upsert(
            EvidenceStateItem(
                **_item(
                    canonical_asset_id="issuer:MSFT:news:0002",
                )
            )
        )
    with pytest.raises(ValueError):
        state.upsert(
            EvidenceStateItem(
                **_item(
                    source_class="issuer_disclosure",
                    evidence_role="DIRECT_PRIMARY",
                )
            )
        )
    with pytest.raises(ValueError):
        state.upsert(
            EvidenceStateItem(
                **_item(
                    content_state="TITLE_ONLY",
                    material_capability="LEAD_ONLY",
                )
            )
        )


def test_evidence_state_is_immutable() -> None:
    state = EvidenceState(**_state())
    with pytest.raises(ValidationError):
        state.evidence_items = ()  # frozen


def test_research_task_result_full_phase_3_contract() -> None:
    result = ResearchTaskResult(
        task_id="task-1",
        task_fingerprint="b" * 64,
        priority=1,
        status="SUCCEEDED",
        started_at=_utc("2026-01-06T14:00:00Z"),
        ended_at=_utc("2026-01-06T14:00:02Z"),
        latency_ms=2000,
        deadline_exhausted=False,
        evidence_items=(EvidenceStateItem(**_item()),),
        structured_facts=(),
        mode_requested="hybrid",
        mode_served="hybrid",
        degradation_reasons=(),
        error_code=None,
        data_runtime_identity=_runtime(),
    )
    assert result.status == "SUCCEEDED"
    assert result.data_runtime_identity.data_snapshot_id == "snapshot:7a004"
    with pytest.raises(ValidationError):
        ResearchTaskResult(
            task_id="task-1",
            task_fingerprint="b" * 64,
            priority=1,
            status="SUCCEEDED",
            started_at=_utc("2026-01-06T14:00:00Z"),
            ended_at=_utc("2026-01-06T13:59:59Z"),
            latency_ms=2000,
            deadline_exhausted=False,
            mode_requested="hybrid",
            data_runtime_identity=_runtime(),
        )
    with pytest.raises(ValidationError):
        ResearchTaskResult(
            task_id="task-1",
            task_fingerprint="b" * 64,
            priority=1,
            status="SUCCEEDED",
            started_at=_utc("2026-01-06T14:00:00Z"),
            ended_at=_utc("2026-01-06T14:00:02Z"),
            latency_ms=-1,
            deadline_exhausted=False,
            mode_requested="hybrid",
            data_runtime_identity=_runtime(),
        )


def test_merged_state_hash_is_recomputed_canonically() -> None:
    state = EvidenceState(**_state())
    merged = state.upsert(
        EvidenceStateItem(
            **_item(
                contributing_task_ids=("task-2",),
                retrieval_contributions=(_contribution(task_id="task-2"),),
            )
        )
    )
    assert merged.state_hash == compute_state_hash(merged)
    assert merged.state_hash != state.state_hash


def test_structured_fact_is_serialized_and_upserted_in_its_own_union() -> None:
    state = EvidenceState(**_state())
    fact = EvidenceStateItem(**_item(
        evidence_id="fact:pe:1", chunk_id=None, fact_id="fact:pe:1",
        text_ref="structured:pe:1", source_class="structured_market_data",
        evidence_role="STRUCTURED_CONTEXT",
    ))
    assert fact.model_dump()["fact_id"] == "fact:pe:1"
    updated = state.upsert(fact)
    assert updated.evidence_items == state.evidence_items
    assert updated.structured_facts == (fact,)


def test_structured_fact_merge_preserves_union_and_rejects_identity_conflict() -> None:
    fact = EvidenceStateItem(**_item(
        evidence_id="fact:pe:1", chunk_id=None, fact_id="fact:pe:1",
        text_ref="structured:pe:1", source_class="structured_market_data",
        evidence_role="STRUCTURED_CONTEXT", contributing_task_ids=("task-1",),
    ))
    state = EvidenceState(**_state(structured_facts=(fact,)))
    merged = state.upsert(EvidenceStateItem(**_item(
        evidence_id="fact:pe:1", chunk_id=None, fact_id="fact:pe:1",
        text_ref="structured:pe:1", source_class="structured_market_data",
        evidence_role="STRUCTURED_CONTEXT", contributing_task_ids=("task-2",),
    )))
    assert merged.evidence_items == state.evidence_items
    assert merged.structured_facts[0].contributing_task_ids == ("task-1", "task-2")
    with pytest.raises(ValueError):
        state.upsert(EvidenceStateItem(**_item(
            evidence_id="fact:pe:1", chunk_id=None, fact_id="fact:pe:1",
            text_ref="structured:other", source_class="structured_market_data",
            evidence_role="STRUCTURED_CONTEXT",
        )))
