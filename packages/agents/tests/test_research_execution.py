"""M4-3: bounded deterministic research executor."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import pytest

from catalyst_agents.attribution.evidence_state import (
    CapabilityGap,
    ResearchTaskResultStatus,
    RetrievalDegradation,
)
from catalyst_agents.attribution.move_profile import (
    AlignmentBand,
    AvailabilityState,
    DirectionRelation,
    MoveProfile,
    PeerReturn,
    PeerSummary,
    SessionAlignment,
    VolumeAbnormality,
    VolumeBand,
)
from catalyst_agents.attribution.provider import RetrievedEvidence
from catalyst_agents.retrieval.execution import (
    ResearchDeadlineError,
    ResearchExecution,
    ResearchExecutor,
    ResearchIntegrityError,
)
from catalyst_agents.retrieval.policy import InitialResearchPolicy
from catalyst_agents.retrieval.task import (
    EvidenceNeed,
    ResearchTask,
    ScenarioType,
    TimeScope,
)
from catalyst_agents.runtime.manifest import ObservationPolicyConfig
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


def _policy() -> ObservationPolicyConfig:
    return ObservationPolicyConfig(
        material_target_return_pct=2.0,
        material_prior_return_pct=1.5,
        quiet_target_return_pct=0.5,
        flat_reference_return_pct=0.25,
        aligned_residual_pct=1.0,
        volume_elevated_ratio=1.5,
        volume_extreme_ratio=3.0,
        minimum_peer_count=2,
        require_sector_and_peer_for_broad_sector=True,
        scenario_policy_version="sp:v1",
    )


def _evidence(chunk_id: str, rank: int = 1, *, fact_id: str | None = None) -> RetrievedEvidence:
    return RetrievedEvidence(
        chunk_id=None if fact_id else chunk_id,
        fact_id=fact_id,
        document_id=f"doc:{chunk_id}",
        content_text=f"evidence {chunk_id}",
        available_at="2026-01-15T10:00:00Z",
        source_class="reported_news",
        ticker_associations=("AAPL",),
        dedup_cluster_id=None,
        cluster_first_available_at="2026-01-15T10:00:00Z",
        representative_document_id=f"doc:{chunk_id}",
        is_novel=False,
        lexical_raw_score=-1.0,
        lexical_rank=rank,
        corpus_manifest_id="m" * 64,
        index_manifest_id="d" * 64,
        mode_requested="reranked",
        mode_served="reranked",
        is_degraded=False,
        fallback_reason=None,
        reranker_score=float(10 - rank),
        reranker_rank=rank,
        canonical_asset_id=f"asset:{chunk_id}",
        canonical_content_version_id=f"version:{chunk_id}",
        corpus_document_id=f"doc:{chunk_id}",
        section_key="body",
        chunk_ordinal=1,
        asset_type="NEWS",
        content_hash="c" * 64,
        material_capability="MATERIAL_CAPABLE",
        serving_status="body_candidate",
        temporal_precision="publication_time",
        evidence_role="INDEPENDENT_REPORT",
        provider="polygon",
        content_state="FULL_TEXT",
        eligible_at=_utc("2026-01-15T10:00:00Z"),
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
    )


class FixtureResearchRetriever:
    def __init__(
        self,
        results: dict[str, tuple[RetrievedEvidence, ...]] | None = None,
        *,
        delays: dict[str, float] | None = None,
        fail_with: dict[str, Exception] | None = None,
    ) -> None:
        self.results = results or {}
        self.delays = delays or {}
        self.fail_with = fail_with or {}
        self.calls: list[dict[str, Any]] = []

    def retrieve(
        self, query, *, ticker, cutoff, requested_manifest_id,
        temporal_identity=None, top_k=8, candidate_depth=20,
    ):
        self.calls.append({
            "query": query, "ticker": ticker, "cutoff": cutoff,
            "requested_manifest_id": requested_manifest_id,
            "temporal_identity": temporal_identity,
        })
        delay = self.delays.get(query, 0.0)
        if delay:
            time.sleep(delay)
        if query in self.fail_with:
            raise self.fail_with[query]
        return self.results.get(query, ())


class RecordingStructuredProvider:
    def __init__(self, facts: dict[str, tuple[RetrievedEvidence, ...]]):
        self.facts = facts
        self.calls: list[str] = []

    def __call__(self, task: ResearchTask):
        self.calls.append(task.evidence_need.value)
        return tuple(
            item.to_evidence_state_item(
                first_seen_round=task.round,
                contributing_task_ids=(task.task_id,),
            )
            for item in self.facts.get(task.evidence_need.value, ())
        )


def _tasks() -> tuple[ResearchTask, ...]:
    policy = InitialResearchPolicy(
        _policy(),
        supported_evidence_needs=frozenset(EvidenceNeed),
    )
    return policy.classify(_macro_profile()).tasks


def _macro_profile() -> MoveProfile:
    from catalyst_agents.attribution.move_profile import ScheduledMacroFlag

    return MoveProfile(
        target_return=3.0,
        prior_session_return=2.0,
        gap_return=0.5,
        market_return=2.9,
        sector_return=2.8,
        peer_summary=PeerSummary(
            schema_version="peer_summary_v1",
            expected_peer_count=2,
            available_peer_count=2,
            peer_returns=(
                PeerReturn(ticker="MSFT", return_pct=2.8),
                PeerReturn(ticker="NVDA", return_pct=3.0),
            ),
            median_return_pct=2.9,
            target_minus_median_pct=0.1,
            coverage_state=AvailabilityState.AVAILABLE,
        ),
        market_adjusted_return=0.1,
        sector_adjusted_return=0.2,
        volume_abnormality=VolumeAbnormality(
            schema_version="volume_abnormality_v1",
            target_volume=1000.0,
            baseline_median_volume=1000.0,
            ratio=1.0,
            expected_session_count=20,
            valid_session_count=12,
            band=VolumeBand.NORMAL,
            availability=AvailabilityState.AVAILABLE,
        ),
        scheduled_macro_flags=(ScheduledMacroFlag(name="FOMC", scheduled_at=None),),
        market_comove=SessionAlignment(
            schema_version="session_alignment_v1",
            reference_kind="MARKET",
            target_return_pct=3.0,
            reference_return_pct=2.9,
            residual_return_pct=0.1,
            direction_relation=DirectionRelation.SAME_DIRECTION,
            band=AlignmentBand.ALIGNED,
            availability=AvailabilityState.AVAILABLE,
        ),
        sector_comove=SessionAlignment(
            schema_version="session_alignment_v1",
            reference_kind="SECTOR",
            target_return_pct=3.0,
            reference_return_pct=2.8,
            residual_return_pct=0.2,
            direction_relation=DirectionRelation.SAME_DIRECTION,
            band=AlignmentBand.ALIGNED,
            availability=AvailabilityState.AVAILABLE,
        ),
        peer_comove=SessionAlignment(
            schema_version="session_alignment_v1",
            reference_kind="PEER_MEDIAN",
            target_return_pct=3.0,
            reference_return_pct=2.9,
            residual_return_pct=0.1,
            direction_relation=DirectionRelation.SAME_DIRECTION,
            band=AlignmentBand.ALIGNED,
            availability=AvailabilityState.AVAILABLE,
        ),
        coverage_flags=(),
        degraded_fields=(),
    )


def _executor(retriever, **kwargs) -> ResearchExecutor:
    defaults = dict(concurrency=2, stage_timeout_seconds=5.0)
    defaults.update(kwargs)
    return ResearchExecutor(retriever=retriever, **defaults)


def _run(executor, tasks=None):
    return executor.execute(
        tasks=tasks or _tasks(),
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        research_policy_version="sp:v1",
        ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="m" * 64,
    )


def test_executor_returns_ordered_results_for_all_tasks():
    retriever = FixtureResearchRetriever({
        "AAPL macro event": (_evidence("a", 1),),
        "AAPL company news": (_evidence("c", 1),),
    })
    provider = RecordingStructuredProvider(
        {"MACRO_SERIES": (_evidence("fact:1", fact_id="fact:1"),)}
    )
    execution = _run(_executor(retriever, structured_provider=provider))
    assert isinstance(execution, ResearchExecution)
    assert len(execution.task_results) == 3
    # MACRO_EVENT + COMPANY_NEWS are text retrieval; MACRO_SERIES is structured.
    assert len(retriever.calls) == 2
    ordered = [r.priority for r in execution.task_results]
    assert ordered == sorted(ordered)
    for result in execution.task_results:
        assert result.status is ResearchTaskResultStatus.SUCCEEDED
        assert result.data_runtime_identity == _runtime()


def test_executor_completion_timing_never_determines_order():
    """A lower-priority task that finishes first still lands after higher
    priority tasks in the deterministic merge."""
    retriever = FixtureResearchRetriever(
        {
            "AAPL macro event": (_evidence("a", 1),),
            "AAPL company news": (_evidence("c", 1),),
        },
        delays={"AAPL macro event": 0.15},
    )
    provider = RecordingStructuredProvider(
        {"MACRO_SERIES": (_evidence("fact:1", fact_id="fact:1"),)}
    )
    execution = _run(_executor(retriever, structured_provider=provider))
    assert [r.priority for r in execution.task_results] == [0, 1, 2]
    assert [r.task_id for r in execution.task_results] == sorted(r.task_id for r in execution.task_results)


def test_executor_zero_hits_is_valid_empty_success():
    retriever = FixtureResearchRetriever()
    provider = RecordingStructuredProvider({})
    execution = _run(_executor(retriever, structured_provider=provider))
    assert len(execution.task_results) == 3
    assert all(r.status is ResearchTaskResultStatus.SUCCEEDED for r in execution.task_results)
    assert all(r.evidence_items == () for r in execution.task_results)
    assert all(r.structured_facts == () for r in execution.task_results)


def test_executor_market_structure_emits_capability_gap_and_no_retrieval():
    from catalyst_agents.retrieval.task import ResearchTask

    task = _tasks()[0]
    market_task = task.model_copy(update={
        "task_id": "research:1:9:market",
        "priority": 9,
        "evidence_need": EvidenceNeed.MARKET_STRUCTURE,
        "task_fingerprint": "f" * 64,
    })
    retriever = FixtureResearchRetriever()
    execution = _run(_executor(retriever), tasks=(market_task,))
    assert retriever.calls == []
    assert execution.task_results[0].status is ResearchTaskResultStatus.FAILED_CAPABILITY
    assert any(
        gap.evidence_need is EvidenceNeed.MARKET_STRUCTURE
        for gap in execution.capability_gaps
    )


def test_executor_structured_without_provider_is_capability_gap_no_scores():
    from catalyst_agents.retrieval.task import ResearchTask

    task = _tasks()[0]
    series_task = task.model_copy(update={
        "task_id": "research:1:9:series",
        "priority": 9,
        "evidence_need": EvidenceNeed.MACRO_SERIES,
        "task_fingerprint": "e" * 64,
    })
    retriever = FixtureResearchRetriever()
    execution = _run(_executor(retriever), tasks=(series_task,))
    assert retriever.calls == []
    result = execution.task_results[0]
    assert result.status is ResearchTaskResultStatus.FAILED_CAPABILITY
    assert any(
        gap.evidence_need is EvidenceNeed.MACRO_SERIES
        and gap.reason_code == "SOURCE_NOT_INDEXED"
        for gap in execution.capability_gaps
    )
    assert result.structured_facts == ()


def test_executor_structured_provider_facts_have_no_fabricated_scores():
    from catalyst_agents.retrieval.task import ResearchTask

    task = _tasks()[0]
    series_task = task.model_copy(update={
        "task_id": "research:1:9:series",
        "priority": 9,
        "evidence_need": EvidenceNeed.MACRO_SERIES,
        "task_fingerprint": "e" * 64,
    })
    fact = _evidence("fact:1", fact_id="fact:1")
    provider = RecordingStructuredProvider({"MACRO_SERIES": (fact,)})
    execution = _run(_executor(FixtureResearchRetriever(), structured_provider=provider), tasks=(series_task,))
    result = execution.task_results[0]
    assert result.status is ResearchTaskResultStatus.SUCCEEDED
    assert result.structured_facts
    assert result.structured_facts[0].retrieval_contributions == ()


def test_executor_integrity_failure_fails_run_despite_sibling_success():
    from catalyst_data.retrieval.result import RetrievalContractError

    retriever = FixtureResearchRetriever(
        {"AAPL macro series": (_evidence("b", 1),)},
        fail_with={"AAPL macro event": RetrievalContractError("runtime_identity_manifest_mismatch")},
    )
    with pytest.raises(ResearchIntegrityError):
        _run(_executor(retriever))


def test_executor_degraded_backend_continues():
    retriever = FixtureResearchRetriever(
        {"AAPL company news": (_evidence("c", 1),)},
        fail_with={"AAPL macro event": RuntimeError("backend timeout")},
    )
    provider = RecordingStructuredProvider(
        {"MACRO_SERIES": (_evidence("fact:1", fact_id="fact:1"),)}
    )
    execution = _run(_executor(retriever, structured_provider=provider))
    statuses = {r.status for r in execution.task_results}
    assert ResearchTaskResultStatus.DEGRADED in statuses
    assert ResearchTaskResultStatus.SUCCEEDED in statuses
    assert any(
        isinstance(d, RetrievalDegradation) for d in execution.degradations
    )


def test_executor_deadline_expiration_is_technical_failure_not_abstain():
    retriever = FixtureResearchRetriever(
        {"AAPL macro event": (_evidence("a", 1),)},
        delays={"AAPL macro event": 1.0},
    )
    executor = _executor(retriever, stage_timeout_seconds=0.1, concurrency=1)
    with pytest.raises(ResearchDeadlineError):
        _run(executor)


def test_executor_deterministic_merge_order_within_and_across_tasks():
    """Merged evidence order: task priority -> task id -> within-task rank ->
    evidence id."""
    from catalyst_agents.retrieval.task import ResearchTask

    base = _tasks()[0]
    primary = base.model_copy(update={
        "task_id": "research:1:0:primary",
        "priority": 0,
        "evidence_need": EvidenceNeed.COMPANY_PRIMARY,
        "task_fingerprint": "a" * 64,
    })
    news = base.model_copy(update={
        "task_id": "research:1:1:news",
        "priority": 1,
        "evidence_need": EvidenceNeed.COMPANY_NEWS,
        "task_fingerprint": "b" * 64,
    })
    retriever = FixtureResearchRetriever({
        "AAPL company primary": (_evidence("a1", 1), _evidence("a0", 2)),
        "AAPL company news": (_evidence("b1", 1),),
    })
    execution = _run(_executor(retriever), tasks=(news, primary))
    merged = [
        item.evidence_id
        for result in sorted(execution.task_results, key=lambda r: (r.priority, r.task_id))
        for item in sorted(
            result.evidence_items,
            key=lambda item: (
                item.retrieval_contributions[0].original_rank
                if item.retrieval_contributions else 0,
                item.evidence_id,
            ),
        )
    ]
    assert merged == ["a1", "a0", "b1"]
