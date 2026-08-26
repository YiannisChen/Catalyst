"""M4-4: cumulative evidence state builder (locked EvidenceStateBuildContext)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalyst_agents.attribution.evidence_state import (
    EvidenceState,
    EvidenceStateItem,
    ResearchTaskResult,
    ResearchTaskResultStatus,
    RetrievalContribution,
    compute_state_hash,
)
from catalyst_agents.attribution.evidence_state_builder import (
    EvidenceIntegrityError,
    EvidenceStateBuildContext,
    build_evidence_state,
)
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-15",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-15T14:30:00Z"),
        session_close_at=_utc("2026-01-15T21:00:00Z"),
        information_window_start_at=_utc("2026-01-14T21:00:00Z"),
        cutoff_at=_utc("2026-01-15T21:00:00Z"),
    )


def _runtime() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id="m" * 64,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _item(
    evidence_id: str,
    *,
    task_id: str,
    round: int,
    eligible_at: str = "2026-01-15T10:00:00Z",
    chunk_id: str | None = None,
    fact_id: str | None = None,
    independence_group_id: str | None = None,
    **overrides,
) -> EvidenceStateItem:
    is_fact = fact_id is not None
    base = dict(
        evidence_id=evidence_id,
        canonical_asset_id=f"asset:{evidence_id}",
        canonical_content_version_id=f"version:{evidence_id}",
        corpus_document_id=f"doc:{evidence_id}",
        chunk_id=chunk_id if chunk_id is not None else (None if is_fact else evidence_id),
        fact_id=fact_id,
        section_key="body" if not is_fact else None,
        chunk_ordinal=1 if not is_fact else None,
        asset_type="STRUCTURED_CONTEXT" if is_fact else "NEWS",
        provider="polygon",
        publisher=None,
        canonical_url=None,
        source_class="structured_market_data" if is_fact else "reported_news",
        evidence_role="STRUCTURED_CONTEXT" if is_fact else "INDEPENDENT_REPORT",
        eligible_at=_utc(eligible_at),
        temporal_precision="publication_time",
        content_state="FULL_TEXT",
        material_capability="MATERIAL_CAPABLE",
        serving_status="body_candidate",
        parse_quality="full",
        independence_group_id=independence_group_id,
        independence_status=(
            "KNOWN_GROUP" if independence_group_id else "UNKNOWN"
        ),
        content_hash="c" * 64,
        text_ref=evidence_id,
        excerpt_text="excerpt",
        first_seen_round=round,
        contributing_task_ids=(task_id,),
        retrieval_contributions=(),
    )
    base.update(overrides)
    return EvidenceStateItem(**base)


def _result(
    task_id: str,
    *,
    priority: int,
    round: int,
    items: tuple[EvidenceStateItem, ...] = (),
    facts: tuple[EvidenceStateItem, ...] = (),
    runtime=None,
) -> ResearchTaskResult:
    started = _utc("2026-01-15T10:00:00Z")
    ended = _utc("2026-01-15T10:00:01Z")
    return ResearchTaskResult(
        task_id=task_id,
        task_fingerprint="f" * 64,
        priority=priority,
        status=ResearchTaskResultStatus.SUCCEEDED,
        started_at=started,
        ended_at=ended,
        latency_ms=1,
        deadline_exhausted=False,
        evidence_items=items,
        structured_facts=facts,
        mode_requested="reranked",
        mode_served="reranked",
        degradation_reasons=(),
        error_code=None,
        data_runtime_identity=runtime or _runtime(),
    )


def _context(**overrides) -> EvidenceStateBuildContext:
    base = dict(
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        research_policy_version="sp:v1",
        task_results=(),
        prior=None,
    )
    base.update(overrides)
    return EvidenceStateBuildContext(**base)


def test_build_round_one_unions_items_and_recomputes_hash():
    item_a = _item("a", task_id="research:1:0:a", round=1)
    item_b = _item("b", task_id="research:1:1:b", round=1)
    context = _context(
        task_results=(
            _result("research:1:0:a", priority=0, round=1, items=(item_a,)),
            _result("research:1:1:b", priority=1, round=1, items=(item_b,)),
        )
    )
    state = build_evidence_state(context)
    assert state.run_id == "run:1"
    assert state.round == 1
    assert state.temporal_identity == _temporal()
    assert state.data_runtime_identity == _runtime()
    assert {item.evidence_id for item in state.evidence_items} == {"a", "b"}
    assert state.state_hash == compute_state_hash(state)


def test_build_round_two_is_immutable_union_preserving_first_seen():
    item_a = _item("a", task_id="research:1:0:a", round=1)
    round_one = build_evidence_state(
        _context(round=1, task_results=(_result("research:1:0:a", priority=0, round=1, items=(item_a,)),))
    )
    item_a_round2 = _item("a", task_id="research:1:2:a", round=2)
    item_b = _item("b", task_id="research:1:3:b", round=2)
    round_two = build_evidence_state(
        _context(
            round=2,
            prior=round_one,
            task_results=(
                _result("research:1:2:a", priority=0, round=2, items=(item_a_round2,)),
                _result("research:1:3:b", priority=1, round=2, items=(item_b,)),
            ),
        )
    )
    assert round_two.round == 2
    assert {item.evidence_id for item in round_two.evidence_items} == {"a", "b"}
    by_id = {item.evidence_id: item for item in round_two.evidence_items}
    assert by_id["a"].first_seen_round == 1  # preserved from round one
    assert by_id["b"].first_seen_round == 2
    assert "research:1:0:a" in by_id["a"].contributing_task_ids
    assert "research:1:2:a" in by_id["a"].contributing_task_ids


def test_same_evidence_id_merges_task_contributions():
    item_a1 = _item("a", task_id="research:1:0:a", round=1)
    item_a2 = _item("a", task_id="research:1:5:a", round=1)
    state = build_evidence_state(
        _context(
            task_results=(
                _result("research:1:0:a", priority=0, round=1, items=(item_a1,)),
                _result("research:1:5:a", priority=5, round=1, items=(item_a2,)),
            )
        )
    )
    assert len(state.evidence_items) == 1
    merged = state.evidence_items[0]
    assert set(merged.contributing_task_ids) == {"research:1:0:a", "research:1:5:a"}
    assert merged.first_seen_round == 1


def test_distinct_chunks_remain_distinct_items():
    item_1 = _item("doc:1:body:0001", task_id="t:1", round=1)
    item_2 = _item("doc:1:body:0002", task_id="t:1", round=1)
    state = build_evidence_state(
        _context(task_results=(_result("t:1", priority=0, round=1, items=(item_1, item_2)),))
    )
    assert len(state.evidence_items) == 2


def test_syndicated_assets_remain_distinct_assets_in_one_group():
    item_1 = _item("news:a", task_id="t:1", round=1, independence_group_id="g:1")
    item_2 = _item("news:b", task_id="t:1", round=1, independence_group_id="g:1")
    state = build_evidence_state(
        _context(task_results=(_result("t:1", priority=0, round=1, items=(item_1, item_2)),))
    )
    assert len(state.evidence_items) == 2
    assert {item.independence_group_id for item in state.evidence_items} == {"g:1"}


def test_structured_facts_and_text_keep_separate_partitions():
    text = _item("chunk:1", task_id="t:1", round=1)
    fact = _item("fact:1", task_id="t:1", round=1, fact_id="fact:1")
    state = build_evidence_state(
        _context(task_results=(_result("t:1", priority=0, round=1, items=(text,), facts=(fact,)),))
    )
    assert len(state.evidence_items) == 1
    assert len(state.structured_facts) == 1
    assert state.evidence_items[0].chunk_id == "chunk:1"
    assert state.structured_facts[0].fact_id == "fact:1"


def test_result_runtime_identity_mismatch_fails_integrity():
    other_runtime = DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id="z" * 64,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )
    item = _item("a", task_id="t:1", round=1)
    with pytest.raises(EvidenceIntegrityError):
        build_evidence_state(
            _context(
                task_results=(_result("t:1", priority=0, round=1, items=(item,), runtime=other_runtime),)
            )
        )


def test_prior_state_identity_mismatch_fails_integrity():
    item = _item("a", task_id="t:1", round=1)
    prior = build_evidence_state(
        _context(round=1, task_results=(_result("t:1", priority=0, round=1, items=(item,)),))
    )
    with pytest.raises(EvidenceIntegrityError):
        build_evidence_state(
            _context(round=2, prior=prior, run_id="run:other")
        )
    with pytest.raises(EvidenceIntegrityError):
        build_evidence_state(
            _context(
                round=2,
                prior=prior,
                temporal_identity=TemporalIdentity(
                    session_date="2026-01-16",
                    market_timezone="America/New_York",
                    session_open_at=_utc("2026-01-16T14:30:00Z"),
                    session_close_at=_utc("2026-01-16T21:00:00Z"),
                    information_window_start_at=_utc("2026-01-15T21:00:00Z"),
                    cutoff_at=_utc("2026-01-16T21:00:00Z"),
                ),
            )
        )
    with pytest.raises(EvidenceIntegrityError):
        build_evidence_state(
            _context(
                round=2,
                prior=prior,
                data_runtime_identity=DataRuntimeIdentity(
                    data_snapshot_id="s" * 64,
                    corpus_manifest_id="m" * 64,
                    fts_index_version="build:fts",
                    dense_index_version="d" * 64,
                    embedding_model_revision="emb:1",
                    reranker_revision="rr:1",
                    query_policy_version="qp:v2",
                ),
            )
        )


def test_conflicting_immutable_metadata_fails_integrity():
    item_1 = _item("a", task_id="t:1", round=1, eligible_at="2026-01-15T10:00:00Z")
    item_2 = _item("a", task_id="t:2", round=1, eligible_at="2026-01-15T11:00:00Z")
    with pytest.raises(ValueError):
        build_evidence_state(
            _context(
                task_results=(
                    _result("t:1", priority=0, round=1, items=(item_1,)),
                    _result("t:2", priority=1, round=1, items=(item_2,)),
                )
            )
        )


def test_task_completion_order_never_changes_final_bytes():
    item_a = _item("a", task_id="research:1:0:a", round=1)
    item_b = _item("b", task_id="research:1:1:b", round=1)
    results_a = (
        _result("research:1:0:a", priority=0, round=1, items=(item_a,)),
        _result("research:1:1:b", priority=1, round=1, items=(item_b,)),
    )
    results_b = (
        _result("research:1:1:b", priority=1, round=1, items=(item_b,)),
        _result("research:1:0:a", priority=0, round=1, items=(item_a,)),
    )
    state_a = build_evidence_state(_context(task_results=results_a))
    state_b = build_evidence_state(_context(task_results=results_b))
    assert state_a == state_b
    assert state_a.state_hash == state_b.state_hash
