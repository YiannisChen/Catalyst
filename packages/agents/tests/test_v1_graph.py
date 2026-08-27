"""M5-9: V1.1 production graph over normalized assessment.

Frozen §6.1; Phase 4 TSD §33/§34; M5 plan M5-9. The V1.1 graph routes
READY/FOLLOW_UP/ABSTAIN over the normalized EvidenceAssessment, runs at most
one corrective round, and terminates through claim plan -> claim validation ->
streaming writer -> structural assurance -> thin finalizer. Normal path is
exactly 2 logical model calls; corrective path exactly 3; provider attempts
<= 2 per role; state stays thin (refs/hashes, no copied evidence/prose).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from catalyst_agents.attribution.provider import ContextInputs, RetrievedEvidence
from catalyst_agents.graph import V1RunResult, run_v1_graph
from catalyst_agents.retrieval.corrective import (
    BackendCapability,
    BackendHealth,
    CorrectiveCapabilityRegistry,
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


class GraphRetriever:
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


class GraphAnalystProvider:
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


class GraphWriterProvider:
    def __init__(self, text_or_factory):
        if callable(text_or_factory):
            self.text_factory = text_or_factory
            self.text = None
        else:
            self.text_factory = None
            self.text = text_or_factory
        self.calls = 0
        self.capability_metadata = {
            "supports_structured_output": True,
            "supports_true_streaming": True,
            "declares_token_accounting": True,
            "normalizes_timeout_errors": True,
            "capability_revision": "test-capability-1",
        }

    def stream(self, messages):
        self.calls += 1
        if self.text_factory is not None:
            yield self.text_factory(messages)
        else:
            yield self.text


def _registry() -> CorrectiveCapabilityRegistry:
    from catalyst_agents.retrieval.task import EvidenceNeed

    capabilities = {}
    for name in (
        "COMPANY_PRIMARY", "COMPANY_NEWS", "SECTOR_NEWS", "MACRO_EVENT",
        "MACRO_SERIES", "FUNDAMENTALS",
    ):
        capabilities[EvidenceNeed(name)] = BackendCapability(
            backend=f"backend:{name.lower()}", health=BackendHealth.HEALTHY
        )
    return CorrectiveCapabilityRegistry(capabilities)


def _graph_kwargs(*, analyst: GraphAnalystProvider, writer: GraphWriterProvider) -> dict:
    return dict(
        run_id="run:v1",
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="m" * 64,
        observation_provider=FakeObservationProvider(),
        retriever=GraphRetriever(),
        policy_config=_policy(),
        research_concurrency=2,
        research_stage_timeout_seconds=10.0,
        persistence=InMemoryPackStore(),
        packing_policy_version="pack:v1",
        template_bytes=b"system: analyse\n",
        template_version="prompt:v1",
        analyst_llm=analyst,
        writer_llm=writer,
        capability_registry=_registry(),
        corrective_policy=None,
        prompt_template="You are the Evidence Analyst. Emit the strict schema.",
    )


def _ready_decision(evidence_id: str = "e1") -> dict:
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
        "recommended_status": "SUFFICIENT",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
    }


def _follow_up_decision() -> dict:
    payload = _ready_decision("e1")
    payload["research_decision"] = "FOLLOW_UP"
    payload["recommended_status"] = "PARTIAL"
    payload["proposed_missing_evidence"] = [
        {
            "proposal_ref": "p1",
            "evidence_need": "COMPANY_PRIMARY",
            "time_scope": "PRIOR_SESSION",
            "expected_information": "issuer filing confirmation",
            "reason_code": "MISSING_PRIMARY_CONFIRMATION",
        }
    ]
    payload["proposed_corrective_intents"] = [
        {"proposal_ref": "p1", "query_hints": ("8-K",)}
    ]
    return payload


def _abstain_decision() -> dict:
    return {
        "schema_version": "1.0",
        "evidence_decisions": [],
        "candidate_hypotheses": [],
        "research_decision": "ABSTAIN",
        "recommended_status": "ABSTAIN",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
    }


def _writer_text_factory(evidence_id: str = "e1"):
    import re

    def factory(messages):
        prompt = messages[0]["content"] if isinstance(messages[0], dict) else messages[0].content
        match = re.search(r"claim_id: ([A-Za-z0-9:_-]+)", prompt)
        claim_id = match.group(1) if match else "claim:1"
        return (
            "SUMMARY\nAAPL rose on record guidance. "
            f"[{claim_id}] ({evidence_id})\n"
            "CAUSAL_EXPLANATION\nGuidance raised forward revenue.\n"
            "LIMITATIONS\nNone."
        )

    return factory


def test_v1_graph_normal_path_two_logical_calls() -> None:
    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(_writer_text_factory())
    result = run_v1_graph(**_graph_kwargs(analyst=analyst, writer=writer))
    assert isinstance(result, V1RunResult)
    assert result.logical_model_call_count == 2
    assert result.analyst_logical_calls == 1
    assert result.writer_logical_calls == 1
    assert result.analyst_provider_attempts == 1
    assert result.writer_provider_attempts == 1
    assert result.corrective_rounds == 0
    assert result.terminal_envelope["assured"] is True
    assert result.terminal_envelope["final_status"] == "SUFFICIENT"
    assert result.validated_claim_plan.status.value == "SUFFICIENT"


def test_v1_graph_corrective_path_three_logical_calls() -> None:
    calls = {"n": 0}

    def corrective_decision() -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return _follow_up_decision()
        return _ready_decision("e2")

    analyst = GraphAnalystProvider(corrective_decision)
    writer = GraphWriterProvider(_writer_text_factory("e2"))
    kwargs = _graph_kwargs(analyst=analyst, writer=writer)
    kwargs["query_builder"] = lambda task, ticker: "corrective " + task.evidence_need.value
    result = run_v1_graph(**kwargs)
    assert result.logical_model_call_count == 3
    assert result.analyst_logical_calls == 2
    assert result.writer_logical_calls == 1
    assert result.corrective_rounds == 1
    assert result.analyst_provider_attempts <= 2
    assert result.writer_provider_attempts <= 2
    assert result.terminal_envelope["assured"] is True


def test_v1_graph_abstain_path_terminates_valid() -> None:
    analyst = GraphAnalystProvider(_abstain_decision)
    writer = GraphWriterProvider(
        "OBSERVED_MOVE\nAAPL +9.5%.\nLIMITATIONS\nNo causal explanation was established from the available evidence."
    )
    result = run_v1_graph(**_graph_kwargs(analyst=analyst, writer=writer))
    assert result.logical_model_call_count == 2
    assert result.terminal_envelope["final_status"] == "ABSTAIN"
    assert result.terminal_envelope["assured"] is True
    assert result.validated_claim_plan.status.value == "ABSTAIN"


def test_v1_graph_state_is_thin() -> None:
    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(_writer_text_factory())
    result = run_v1_graph(**_graph_kwargs(analyst=analyst, writer=writer))
    state = result.state
    assert state["run_id"] == "run:v1"
    assert state["round"] == 1
    assert state["evidence_state_hash"]
    assert state["context_pack_hash"]
    # Thin orchestration: no copied evidence lists, prose, causes, or reasoning.
    for forbidden in ("retrieved_chunks", "graded_evidence", "critic_reasoning", "summary_md", "causes"):
        assert forbidden not in state
