"""M4-8: authoritative foundation graph (fixture mode) beside sealed MCJ."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from catalyst_agents.attribution.provider import ContextInputs, RetrievedEvidence
from catalyst_agents.graph import FoundationRunResult, build_foundation_graph
from catalyst_agents.runtime.manifest import ObservationPolicyConfig
from catalyst_agents.runtime.pack_persistence import (
    InMemoryPackStore,
    replay_asserts_pair,
)
from catalyst_agents.state import FoundationGraphState, FoundationStage
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


class FakeObservationProvider:
    def __init__(self):
        self.calls = 0

    def load_context_inputs(self, *, ticker, session_date, cutoff,
                           information_window_start_at=None) -> ContextInputs:
        self.calls += 1
        return ContextInputs(
            ticker=ticker,
            session_date=date.fromisoformat(session_date),
            cutoff=cutoff,
            target_close=110.0,
            previous_target_close=100.0,
            target_open=102.0,
            previous_2_target_close=90.0,
            target_volume=2200.0,
            expected_prior_sessions=tuple(f"2026-01-{d:02d}" for d in range(1, 21)),
            prior_volumes_by_session={f"2026-01-{d:02d}": 1000.0 + d for d in range(1, 21)},
            benchmark_ticker="SPY",
            benchmark_return_pct=9.5,
            sector_ticker="XLK",
            sector_return_pct=9.2,
            peer_returns_by_ticker={"MSFT": 9.8, "NVDA": 9.6},
        )


class FakeRetriever:
    def __init__(self):
        self.calls = 0

    def _evidence(self, chunk_id, rank):
        return RetrievedEvidence(
            chunk_id=chunk_id,
            document_id=f"doc:{chunk_id}",
            content_text=f"evidence {chunk_id}",
            available_at="2026-01-15T10:00:00Z",
            source_class="issuer_disclosure",
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
            evidence_role="DIRECT_PRIMARY",
            provider="polygon",
            content_state="FULL_TEXT",
            eligible_at=_utc("2026-01-15T10:00:00Z"),
            temporal_identity=_temporal(),
            data_runtime_identity=_runtime(),
        )

    def retrieve(self, query, *, ticker, cutoff, requested_manifest_id,
                 temporal_identity=None, top_k=8, candidate_depth=20):
        self.calls += 1
        # Deterministic chunk id per query: completion order must never
        # influence evidence identity or pack bytes.
        label = query.lower().replace(" ", "_")
        return (self._evidence(f"chunk:{label}", 1),)


class RecordingAnalystBoundary:
    def __init__(self):
        self.calls = 0
        self.last_pack = None

    def __call__(self, pack):
        self.calls += 1
        self.last_pack = pack
        return {"boundary": "fixture", "pack_sha256": pack.context_pack_sha256}


def _run_kwargs(**overrides):
    base = dict(
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="m" * 64,
        observation_provider=FakeObservationProvider(),
        retriever=FakeRetriever(),
        policy_config=_policy(),
        research_concurrency=2,
        research_stage_timeout_seconds=5.0,
        persistence=InMemoryPackStore(),
        packing_policy_version="pack:v1",
        template_bytes=b"system: analyse\n",
        template_version="prompt:v1",
        analyst_boundary=None,
    )
    base.update(overrides)
    return base


def test_foundation_graph_wires_full_pipeline_fixture():
    result = build_foundation_graph(**_run_kwargs())
    assert isinstance(result, FoundationRunResult)
    state = result.state
    assert state["run_id"] == "run:1"
    assert state["stage"] is FoundationStage.ANALYST_BOUNDARY
    assert result.move_profile.target_return is not None
    assert result.evidence_state.evidence_items
    assert result.coverage_summary.eligible_item_count >= 1
    assert result.context_pack.context_pack_sha256
    assert len(result.context_pack.context_pack_sha256) == 64
    # Identities retained end-to-end.
    assert result.evidence_state.temporal_identity == _temporal()
    assert result.evidence_state.data_runtime_identity == _runtime()


def test_foundation_graph_replay_determinism():
    first = build_foundation_graph(**_run_kwargs())
    second = build_foundation_graph(**_run_kwargs())
    assert first.context_pack == second.context_pack
    assert first.context_pack.context_pack_sha256 == second.context_pack.context_pack_sha256
    assert first.context_pack.rendered_messages_sha256 == second.context_pack.rendered_messages_sha256
    # Evidence union is deterministic (task timing metadata is observable and
    # run-specific; the pack/render hashes are the replay authority).
    assert first.evidence_state.evidence_items == second.evidence_state.evidence_items
    assert first.coverage_summary == second.coverage_summary
    # Persisted pair + replay assertion.
    assert first.persisted_pair is not None
    replay_asserts_pair(
        first.persisted_pair.refs,
        pack_sha256=first.context_pack.context_pack_sha256,
        rendered_messages=first.persisted_pair.rendered_messages,
        rendered_messages_sha256=first.context_pack.rendered_messages_sha256,
        prompt_template_sha256=first.context_pack.prompt_template_sha256,
    )


def test_foundation_graph_analyst_fixture_boundary():
    boundary = RecordingAnalystBoundary()
    result = build_foundation_graph(**_run_kwargs(analyst_boundary=boundary))
    assert boundary.calls == 1
    assert boundary.last_pack is result.context_pack
    assert result.analyst_result == {"boundary": "fixture", "pack_sha256": result.context_pack.context_pack_sha256}


def test_foundation_graph_state_holds_references_not_copied_payloads():
    """V1.1 graph state: artifact refs/hashes only; no copied evidence lists,
    prose, causes, or hidden reasoning."""
    fields = set(FoundationGraphState.__annotations__)
    for forbidden in (
        "evidence_items", "retrieved_chunks", "reranked_chunks", "graded_evidence",
        "critic_reasoning", "causes", "summary_md", "hypotheses", "reasoning",
    ):
        assert forbidden not in fields, forbidden
    result = build_foundation_graph(**_run_kwargs())
    state = result.state
    assert state["evidence_state_ref"]
    assert state["evidence_state_hash"] == result.evidence_state.state_hash
    assert state["context_pack_ref"]
    assert state["context_pack_hash"] == result.context_pack.context_pack_sha256
    assert state["policy_version"] == "sp:v1"
    assert state["round"] == 1
    assert state["attempt"] == 1
    assert state["cancel_requested"] is False


def test_foundation_graph_does_not_run_mcj_nodes():
    """The foundation path never runs Miner/Critic/Judge (archived at M5-11);
    the V1.1 graph is the only production graph."""
    import catalyst_agents.graph as graph_module

    assert hasattr(graph_module, "build_foundation_graph")
    assert hasattr(graph_module, "run_v1_graph")
    result = build_foundation_graph(**_run_kwargs())
    # No MCJ semantic fields appear in the foundation state.
    assert "critic_decision" not in result.state
    assert "causes" not in result.state


# ---------------------------------------------------------------------------
# Final corrective pass: minimum remaining run/stage deadline
# ---------------------------------------------------------------------------


def test_foundation_graph_deadline_is_epoch_and_injected():
    """deadline_epoch_ms must be Unix epoch milliseconds; when the stage
    timeout is shorter than the run deadline the effective stage deadline is
    the binding deadline (never time.monotonic())."""
    import time as _time

    run_deadline = int((_time.time() + 60.0) * 1000)
    assert run_deadline > 1_600_000_000_000  # sane epoch ms
    stage_timeout = 5.0
    result = build_foundation_graph(
        **_run_kwargs(
            run_deadline_epoch_ms=run_deadline,
            research_stage_timeout_seconds=stage_timeout,
        )
    )
    deadline = result.state["deadline_epoch_ms"]
    assert deadline > 1_600_000_000_000  # epoch ms, not monotonic
    assert deadline < run_deadline


def test_foundation_graph_stage_timeout_shorter_than_run_deadline_wins():
    """Stage timeout shorter than the run deadline remains the effective
    deadline (min semantics): deadline ~= now + stage_timeout_ms."""
    import time as _time

    run_deadline = int((_time.time() + 60.0) * 1000)
    stage_timeout = 5.0
    result = build_foundation_graph(
        **_run_kwargs(
            run_deadline_epoch_ms=run_deadline,
            research_stage_timeout_seconds=stage_timeout,
        )
    )
    now_ms = int(_time.time() * 1000)
    deadline = result.state["deadline_epoch_ms"]
    assert deadline < run_deadline
    assert abs(deadline - (now_ms + int(stage_timeout * 1000))) <= 2000


def test_foundation_graph_uses_shorter_run_deadline_without_value_error():
    """Stage timeout 5s with a run deadline ~1s away: the pipeline uses the
    shorter deadline instead of raising ValueError, and succeeds within it."""
    import time as _time

    run_deadline = int((_time.time() + 1.0) * 1000)
    result = build_foundation_graph(
        **_run_kwargs(
            run_deadline_epoch_ms=run_deadline,
            research_stage_timeout_seconds=5.0,
        )
    )
    assert result.state["deadline_epoch_ms"] == run_deadline
    assert result.state["deadline_epoch_ms"] <= run_deadline


def test_foundation_graph_already_expired_run_deadline_fails_technically():
    """An already-expired run deadline produces the technical deadline failure
    (ResearchDeadlineError), never ValueError or ABSTAIN."""
    import time as _time

    from catalyst_agents.retrieval.execution import ResearchDeadlineError

    run_deadline = int((_time.time() - 1000.0) * 1000)
    with pytest.raises(ResearchDeadlineError):
        build_foundation_graph(
            **_run_kwargs(
                run_deadline_epoch_ms=run_deadline,
                research_stage_timeout_seconds=5.0,
            )
        )


def test_foundation_graph_state_deadline_never_exceeds_run_deadline():
    """The persisted state deadline is always <= the supplied run deadline."""
    import time as _time

    run_deadline = int((_time.time() + 2.0) * 1000)
    result = build_foundation_graph(
        **_run_kwargs(
            run_deadline_epoch_ms=run_deadline,
            research_stage_timeout_seconds=5.0,
        )
    )
    assert result.state["deadline_epoch_ms"] <= run_deadline
