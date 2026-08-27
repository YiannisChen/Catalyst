"""M5-4: round-two EvidenceAnalyst over cumulative evidence.

Frozen §6.4; Phase 4 TSD §21; Final TSD §12; M5 plan M5-4. FOLLOW_UP -> one
CorrectiveResearchBatch -> ResearchExecutor -> cumulative EvidenceState
(round-1 union round-2, deduped) -> recomputed CoverageSummary -> rebuilt
round-2 ContextPack (delta ids + prior assessment context) -> second Analyst
call -> round-2 EvidenceAssessment. Round two never discards round-one
evidence and no third Analyst call exists after round 2. A FOLLOW_UP with no
valid batch never triggers a second Analyst call.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from catalyst_agents.attribution.analyst import AnalystDecision
from catalyst_agents.attribution.assessment import normalize_decision
from catalyst_agents.attribution.provider import ContextInputs, RetrievedEvidence
from catalyst_agents.graph import CorrectiveRoundResult, build_foundation_graph, run_corrective_round
from catalyst_agents.nodes.evidence_analyst import evidence_analyst
from catalyst_agents.retrieval.corrective import (
    CorrectiveCapabilityRegistry,
    BackendCapability,
    BackendHealth,
    CorrectivePolicy,
)
from catalyst_agents.runtime.manifest import ObservationPolicyConfig
from catalyst_agents.runtime.pack_persistence import InMemoryPackStore
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
    def load_context_inputs(self, *, ticker, session_date, cutoff,
                            information_window_start_at=None) -> ContextInputs:
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


class RoundRetriever:
    """Returns e1 on initial tasks and e2 on corrective tasks."""

    def __init__(self):
        self.calls = 0

    def _evidence(self, chunk_id: str, rank: int) -> RetrievedEvidence:
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
        label = query.lower().replace(" ", "_")
        if label.startswith("corrective"):
            return (self._evidence("e2", 1),)
        return (self._evidence("e1", 1),)


class FakeAnalystProvider:
    def __init__(self, decision_factory):
        self.decision_factory = decision_factory
        self.calls = 0
        self.capability_metadata = {
            "supports_structured_output": True,
            "supports_true_streaming": True,
            "declares_token_accounting": True,
            "normalizes_timeout_errors": True,
            "capability_revision": "test-capability-1",
        }

    def invoke(self, messages):
        self.calls += 1
        return self.decision_factory()


def _registry() -> CorrectiveCapabilityRegistry:
    capabilities = {}
    for need in (
        "COMPANY_PRIMARY", "COMPANY_NEWS", "SECTOR_NEWS", "MACRO_EVENT",
        "MACRO_SERIES", "FUNDAMENTALS",
    ):
        from catalyst_agents.retrieval.task import EvidenceNeed
        capabilities[EvidenceNeed(need)] = BackendCapability(
            backend=f"backend:{need.lower()}",
            health=BackendHealth.HEALTHY,
        )
    return CorrectiveCapabilityRegistry(capabilities)


def _follow_up_decision(evidence_id: str) -> dict:
    return {
        "schema_version": "1.0",
        "evidence_decisions": [
            {
                "evidence_id": evidence_id,
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ["h1"],
                "reason_code": "material_support",
            }
        ],
        "candidate_hypotheses": [
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "AAPL rose on record guidance.",
                "supporting_evidence_ids": [evidence_id],
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            }
        ],
        "research_decision": "FOLLOW_UP",
        "recommended_status": "PARTIAL",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
        "proposed_missing_evidence": [
            {
                "proposal_ref": "p1",
                "evidence_need": "COMPANY_PRIMARY",
                "time_scope": "PRIOR_SESSION",
                "expected_information": "issuer filing confirmation",
                "reason_code": "MISSING_PRIMARY_CONFIRMATION",
            }
        ],
        "proposed_corrective_intents": [
            {"proposal_ref": "p1", "query_hints": ("8-K",)}
        ],
    }


def _ready_decision(evidence_id: str) -> dict:
    return {
        "schema_version": "1.0",
        "evidence_decisions": [
            {
                "evidence_id": evidence_id,
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ["h1"],
                "reason_code": "material_support",
            }
        ],
        "candidate_hypotheses": [
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "AAPL rose on record guidance.",
                "supporting_evidence_ids": [evidence_id],
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            }
        ],
        "research_decision": "READY",
        "recommended_status": "PARTIAL",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
    }


def _foundation(run_id: str) -> tuple[Any, ...]:
    retriever = RoundRetriever()
    persistence = InMemoryPackStore()
    result = build_foundation_graph(
        run_id=run_id,
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="m" * 64,
        observation_provider=FakeObservationProvider(),
        retriever=retriever,
        policy_config=_policy(),
        research_concurrency=2,
        research_stage_timeout_seconds=5.0,
        persistence=persistence,
        packing_policy_version="pack:v1",
        template_bytes=b"system: analyse\n",
        template_version="prompt:v1",
        analyst_boundary=None,
    )
    return result, retriever, persistence


def _round1_assessment(run_id: str):
    result, retriever, persistence = _foundation(run_id)
    llm = FakeAnalystProvider(lambda: _follow_up_decision("e1"))
    analyst = evidence_analyst(result.state, llm=llm, persistence=persistence)
    assessment = normalize_decision(
        analyst["analyst_decision"],
        result.context_pack,
        _registry(),
        "norm:v1",
        evidence_state_hash=result.state["evidence_state_hash"],
    )
    assert assessment.corrective_batch is not None
    return result, retriever, persistence, llm, assessment


def test_round_two_merges_cumulative_evidence_and_calls_analyst_once() -> None:
    run_id = "run:1"
    result, retriever, persistence, round1_llm, assessment = _round1_assessment(run_id)

    round2_llm = FakeAnalystProvider(lambda: _ready_decision("e2"))
    corrective = run_corrective_round(
        run_id=run_id,
        round=2,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="m" * 64,
        scenario=result.classification.scenario,
        move_profile=result.move_profile,
        prior_evidence_state=result.evidence_state,
        prior_research_execution=result.research_execution,
        prior_assessment=assessment,
        research_policy_version=_policy().scenario_policy_version,
        retriever=retriever,
        research_concurrency=2,
        research_stage_timeout_seconds=5.0,
        persistence=persistence,
        packing_policy_version="pack:v1",
        template_bytes=b"system: analyse\n",
        template_version="prompt:v1",
        llm=round2_llm,
        capability_registry=_registry(),
        query_builder=lambda task, ticker: "corrective " + task.evidence_need.value,
        research_history=result.classification.tasks,
    )

    # Cumulative evidence: round-one e1 is never discarded, round-two e2 added.
    assert isinstance(corrective, CorrectiveRoundResult)
    ids = {item.evidence_id for item in corrective.evidence_state.evidence_items}
    assert "e1" in ids
    assert "e2" in ids
    assert corrective.delta_evidence_ids == ("e2",)
    assert corrective.assessment.final_status.value in {"PARTIAL", "SUFFICIENT", "ABSTAIN"}
    # Exactly one second Analyst call; no third call exists after round two.
    assert round2_llm.calls == 1
    assert corrective.analyst_logical_calls == 1
    assert corrective.analyst_provider_attempts == 1


def test_round_two_pack_carries_prior_assessment_context() -> None:
    run_id = "run:2"
    result, retriever, persistence, _, assessment = _round1_assessment(run_id)
    round2_llm = FakeAnalystProvider(lambda: _ready_decision("e2"))
    corrective = run_corrective_round(
        run_id=run_id,
        round=2,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="m" * 64,
        scenario=result.classification.scenario,
        move_profile=result.move_profile,
        prior_evidence_state=result.evidence_state,
        prior_research_execution=result.research_execution,
        prior_assessment=assessment,
        research_policy_version=_policy().scenario_policy_version,
        retriever=retriever,
        research_concurrency=2,
        research_stage_timeout_seconds=5.0,
        persistence=persistence,
        packing_policy_version="pack:v1",
        template_bytes=b"system: analyse\n",
        template_version="prompt:v1",
        llm=round2_llm,
        capability_registry=_registry(),
        query_builder=lambda task, ticker: "corrective " + task.evidence_need.value,
        research_history=result.classification.tasks,
    )
    context = corrective.context_pack.prior_assessment_context
    assert context is not None
    assert set(context.previously_supported_hypothesis_ids) == {
        hypothesis.hypothesis_id
        for hypothesis in assessment.normalized_hypotheses
        if hypothesis.supporting_evidence_ids
    }
    assert context.unresolved_gap_ids == tuple(
        gap.gap_id for gap in assessment.validated_missing_evidence
    )


def test_follow_up_without_valid_batch_never_calls_second_analyst() -> None:
    run_id = "run:3"
    result, retriever, persistence, _, assessment = _round1_assessment(run_id)
    # Strip the batch to simulate a FOLLOW_UP normalized without an executable
    # corrective batch (e.g., all gaps non-recoverable).
    assessment = assessment.model_copy(
        update={"corrective_batch": None, "research_decision": "READY"}
    )
    round2_llm = FakeAnalystProvider(lambda: _ready_decision("e2"))
    with pytest.raises(ValueError, match="no corrective batch"):
        run_corrective_round(
            run_id=run_id,
            round=2,
            temporal_identity=_temporal(),
            data_runtime_identity=_runtime(),
            ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z",
            requested_manifest_id="m" * 64,
            scenario=result.classification.scenario,
            move_profile=result.move_profile,
            prior_evidence_state=result.evidence_state,
            prior_research_execution=result.research_execution,
            prior_assessment=assessment,
            research_policy_version=_policy().scenario_policy_version,
            retriever=retriever,
            research_concurrency=2,
            research_stage_timeout_seconds=5.0,
            persistence=persistence,
            packing_policy_version="pack:v1",
            template_bytes=b"system: analyse\n",
            template_version="prompt:v1",
            llm=round2_llm,
            capability_registry=_registry(),
        )
    assert round2_llm.calls == 0
