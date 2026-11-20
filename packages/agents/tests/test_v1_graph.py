"""M5-9: V1.1 production graph over normalized assessment.

Frozen §6.1; Phase 4 TSD §33/§34; M5 plan M5-9. The V1.1 graph routes
READY/FOLLOW_UP/ABSTAIN over the normalized EvidenceAssessment, runs at most
one corrective round, and terminates through claim plan -> claim validation ->
streaming writer -> structural assurance -> thin finalizer. Normal path is
exactly 2 logical model calls; corrective path exactly 3; provider attempts
<= 2 per role; state stays thin (refs/hashes, no copied evidence/prose).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any

import pytest
from catalyst_agents.attribution.provider import ContextInputs, RetrievedEvidence
from catalyst_agents.graphs.graph import (
    V1FullRunResult,
    V1RunResult,
    V1TierOnlyRunResult,
    run_v1_graph,
)
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
            content_text=(
                "Apple reported record revenue and raised its full-year "
                f"guidance. Reference {chunk_id} was included in the filing."
            ),
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
            parse_quality="full",
            eligible_at=_utc("2026-01-15T10:00:00Z"),
            temporal_identity=_temporal(),
            data_runtime_identity=_runtime(),
        )

    def retrieve(self, query, *, ticker, cutoff, requested_manifest_id,
                 temporal_identity=None, top_k=8, candidate_depth=20, source_classes=None, temporal_centre_date=None):
        self.calls += 1
        label = query.lower().replace(" ", "_")
        if label.startswith("corrective"):
            return (self._evidence("e2", 1),)
        return (self._evidence("e1", 1),)


class OrdinaryObservationProvider(FakeObservationProvider):
    def load_context_inputs(self, *, ticker, session_date, cutoff,
                            information_window_start_at=None) -> ContextInputs:
        inputs = super().load_context_inputs(
            ticker=ticker, session_date=session_date, cutoff=cutoff,
            information_window_start_at=information_window_start_at,
        )
        return ContextInputs(
            **{
                **inputs.__dict__,
                "target_close": 100.5,
                "previous_target_close": 100.0,
                "target_open": 100.1,
                "benchmark_return_pct": 0.4,
                "sector_return_pct": 0.5,
                "peer_returns_by_ticker": {"P1": 0.4, "P2": 0.5, "P3": 0.6},
            }
        )


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

    def with_structured_output(self, schema):
        outer = self

        class Surface:
            def invoke(self, messages):
                return outer.invoke(messages)

        return Surface()


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
    from catalyst_agents.attribution.policy import resolve_attribution_policy

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
        # These fixtures exercise the historical direct graph path. Production
        # callsites select desk_cascade_v1 through the runtime resolver.
        attribution_policy=resolve_attribution_policy(
            {"CATALYST_PRIMARY_VERIFIER": "0"}, evidence_window_policy="pre_close_v1"
        ),
    )


class DeskExtractorProvider:
    def __init__(self, payloads):
        self.payloads = tuple(payloads)
        self.stage2 = False
        self.calls = 0

    def bind(self, **kwargs):
        self.bound_options = kwargs
        return self

    def __call__(self, messages):
        return self.invoke(messages)

    def invoke(self, messages):
        import json
        import json as _json
        from types import SimpleNamespace

        if isinstance(messages, list) and len(messages) > 1 and (
                "date_phrase" in messages[1]["content"]
                or "event_summary" not in messages[1]["content"]):
            return SimpleNamespace(result=SimpleNamespace(content=_json.dumps(
                {"event_date": "2026-01-15", "precision": "DAY", "time_et": None,
                 "date_phrase": "after Apple raised"}),
                counts=SimpleNamespace(logical_calls=1, provider_attempts=1),
                attempts=(), attempt_usages=()))
        self.calls += 1
        payload = self.payloads[1] if self.stage2 and len(self.payloads) > 1 else self.payloads[0]
        return SimpleNamespace(content=json.dumps(payload))


def _desk_extraction(*, publish: bool):
    if not publish:
        return {
            "about_ticker": "OTHER", "session": "OTHER_SESSION", "event_type": "NONE",
            "event_summary": "", "event_timing": "UNKNOWN", "explicit_attribution": False,
            "attribution_quote": "", "event_quote": "", "mechanism_quote": "",
            "is_preview_or_evergreen": False,
        }
    return {
        "about_ticker": "ISSUER", "session": "THIS_SESSION", "event_type": "EARNINGS",
        "event_summary": "record revenue and raised guidance", "event_timing": "INTRADAY",
        "explicit_attribution": True,
        "attribution_quote": "Apple shares rose 10% after Apple raised its full-year guidance.",
        "event_quote": "Apple raised its full-year guidance.", "mechanism_quote": "",
        "is_preview_or_evergreen": False,
    }


class DeskGraphRetriever(GraphRetriever):
    def __init__(self, llm=None):
        super().__init__()
        self.llm = llm

    def retrieve(self, query, *, temporal_identity=None, **kwargs):
        if (self.llm is not None and temporal_identity is not None
                and getattr(temporal_identity, "evidence_window_start_at", None) is not None):
            self.llm.stage2 = True
        return super().retrieve(query, temporal_identity=temporal_identity, **kwargs)

    def _evidence(self, chunk_id: str, rank: int) -> RetrievedEvidence:
        import dataclasses

        evidence = super()._evidence(chunk_id, rank)
        return dataclasses.replace(
            evidence,
            content_text="Apple shares rose 10% after Apple raised its full-year guidance. "
                         "Apple raised its full-year guidance.",
            content_hash="d" * 64,
        )


def _desk_graph_kwargs(payloads):
    from catalyst_agents.attribution.policy import resolve_attribution_policy

    fake = DeskExtractorProvider(payloads)
    retriever = DeskGraphRetriever(fake)
    kwargs = _graph_kwargs(analyst=GraphAnalystProvider(_abstain_decision), writer=GraphWriterProvider(_writer_text_factory()))
    kwargs.update(
        analyst_llm=fake, retriever=retriever,
        attribution_policy=resolve_attribution_policy(
            {"CATALYST_PRIMARY_VERIFIER": "0"}, evidence_window_policy="desk_cascade_v1",
            web_evidence="off",
        ),
    )
    return kwargs, fake


def test_desk_cascade_graph_stage_one_publish_skips_stage_two_and_adds_snapshot_context():
    from types import SimpleNamespace

    kwargs, fake = _desk_graph_kwargs([_desk_extraction(publish=True)])
    kwargs["desk_context_fn"] = lambda *args: SimpleNamespace(
        version="desk_decomposition_v1", verdict="GROUP_DRIVEN", best=None,
        sentence=lambda: "[Catalyst analysis, inferred] group move",
    )
    result = run_v1_graph(**kwargs)
    assert result.answer_snapshot["cascade_stage"] == 1
    assert result.answer_snapshot["basis"] == "REPORTED_ATTRIBUTION"
    assert result.answer_snapshot["desk_context"]["verdict"] == "GROUP_DRIVEN"
    assert result.answer_snapshot["key_sentence"]["text"] == (
        "Apple shares rose 10% after Apple raised its full-year guidance.")
    assert fake.calls >= 1
    assert len(result.decision_trace["rounds"]) == 1


def test_desk_cascade_graph_stage_one_abstain_runs_discovery_stage_two_as_final(monkeypatch):
    monkeypatch.setenv("CATALYST_CASCADE_STAGE2", "1")
    from catalyst_agents.attribution.policy import resolve_attribution_policy
    from catalyst_data.canonical.temporal import with_event_time_publication_window

    kwargs, fake = _desk_graph_kwargs([_desk_extraction(publish=False), _desk_extraction(publish=True)])
    kwargs["attribution_policy"] = resolve_attribution_policy(
        {"CATALYST_PRIMARY_VERIFIER": "0"}, evidence_window_policy="desk_cascade_v1",
        web_evidence="discovery_v2",
    )
    kwargs["stage2_temporal_identity"] = with_event_time_publication_window(_temporal())
    kwargs["stage2_retriever"] = DeskGraphRetriever(fake)
    result = run_v1_graph(**kwargs)
    assert fake.calls >= 2
    assert result.decision_trace["rounds"][-1]["cascade_stage"] == 2
    assert result.answer_snapshot["cascade_stage"] == 2
    assert result.answer_snapshot["outcome"] == "ANSWERED"


def test_desk_cascade_discovery_skips_stage_two_by_default(monkeypatch):
    monkeypatch.delenv("CATALYST_CASCADE_STAGE2", raising=False)
    from catalyst_agents.attribution.policy import resolve_attribution_policy
    from catalyst_data.canonical.temporal import with_event_time_publication_window

    kwargs, fake = _desk_graph_kwargs([_desk_extraction(publish=False), _desk_extraction(publish=True)])
    kwargs["attribution_policy"] = resolve_attribution_policy(
        {"CATALYST_PRIMARY_VERIFIER": "0"}, evidence_window_policy="desk_cascade_v1",
        web_evidence="discovery_v2",
    )
    kwargs["stage2_temporal_identity"] = with_event_time_publication_window(_temporal())
    kwargs["stage2_retriever"] = DeskGraphRetriever(fake)
    result = run_v1_graph(**kwargs)
    assert len(result.decision_trace["rounds"]) == 1
    assert result.decision_trace["cascade_stage_2"] == "skipped"
    assert result.answer_snapshot["cascade_stage"] == 1
    assert result.answer_snapshot["outcome"] == "ABSTAINED"


def test_desk_cascade_web_off_never_runs_stage_two():
    kwargs, fake = _desk_graph_kwargs([_desk_extraction(publish=False)])
    result = run_v1_graph(**kwargs)
    assert fake.calls >= 1
    assert len(result.decision_trace["rounds"]) == 1
    assert result.answer_snapshot["cascade_stage"] == 1


def test_graph_runtime_fallback_rejects_non_cascade_environment_policy(monkeypatch):
    monkeypatch.setenv("CATALYST_EVIDENCE_WINDOW_POLICY", "pre_close_v1")
    kwargs = _graph_kwargs(
        analyst=GraphAnalystProvider(_abstain_decision),
        writer=GraphWriterProvider(_writer_text_factory()),
    )
    kwargs.pop("attribution_policy")
    with pytest.raises(ValueError, match="only desk_cascade_v1"):
        run_v1_graph(**kwargs)


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
                "quotes": [
                    {
                        "evidence_id": evidence_id,
                        "text": "Apple reported record revenue and raised its "
                        "full-year guidance.",
                    }
                ],
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
    assert isinstance(result, V1FullRunResult)
    assert result.logical_model_call_count == 1  # Analyst only (Q1c)
    assert result.analyst_logical_calls == 1
    assert result.writer_logical_calls == 0  # Q1c: code-rendered answer
    assert result.analyst_provider_attempts == 1
    assert result.writer_provider_attempts == 0  # Q1c
    assert result.corrective_rounds == 0
    assert result.tier_decision.tier in {"T2", "T3"}
    assert result.state is not None
    assert result.evidence_state is not None
    assert result.coverage_summary is not None
    assert result.context_pack is not None
    assert result.answer_snapshot["tier"] == result.tier_decision.tier
    assert "tier_reason" in result.answer_snapshot
    assert "governing_ref" in result.answer_snapshot
    assert result.terminal_envelope["assured"] is True
    assert result.terminal_envelope["final_status"] == "SUFFICIENT"
    assert result.validated_claim_plan.status.value == "SUFFICIENT"


def test_v1_graph_emits_progress_events_in_step_order_and_abstention_snapshot() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    analyst = GraphAnalystProvider(_abstain_decision)
    writer = GraphWriterProvider(
        "OBSERVED_MOVE\nAAPL +9.5%.\nLIMITATIONS\nNo causal explanation was established."
    )
    result = run_v1_graph(
        **_graph_kwargs(analyst=analyst, writer=writer),
        on_step=lambda event_type, payload: events.append((event_type, payload)),
    )
    names = [event_type for event_type, _ in events]
    assert names == [
        "facts.ready", "tier.decided", "retrieval.done", "analyst.done",
        "checks.done", "gates.done", "verifier.done", "answer.ready",
    ]
    assert events[0][1]["window"]["cutoff_at"].endswith("+00:00")
    assert events[0][1]["facts"].keys() == events[-1][1]["answer_snapshot"]["facts"].keys()
    assert events[2][1]["inventory_count"] == len(result.context_pack.evidence_inventory)
    assert events[3][1]["research_decision"] == "ABSTAIN"
    assert all("text" not in quote for quote in events[4][1]["quotes"])
    assert events[5][1]["gates"] == events[4][1]["gates"]
    assert events[-1][1]["answer_snapshot"]["outcome"] == "ABSTAINED"


def test_analyst_decisions_emit_after_parse_without_changing_attribution() -> None:
    baseline = run_v1_graph(
        **_graph_kwargs(
            analyst=GraphAnalystProvider(_ready_decision),
            writer=GraphWriterProvider(_writer_text_factory()),
        )
    )
    events: list[tuple[str, dict[str, Any]]] = []
    observed = run_v1_graph(
        **_graph_kwargs(
            analyst=GraphAnalystProvider(_ready_decision),
            writer=GraphWriterProvider(_writer_text_factory()),
        ),
        on_step=lambda name, payload: events.append((name, payload)),
    )

    decision_events = [payload for name, payload in events if name == "analyst.decision"]
    assert [payload["kind"] for payload in decision_events] == [
        "evidence", "candidate_cause"
    ]
    assert decision_events[0]["evidence_id"] == "e1"
    assert decision_events[1]["ref"] == "h1"
    assert [name for name, _ in events].index("analyst.decision") < [
        name for name, _ in events
    ].index("checks.done")
    assert [name for name, _ in events].index("checks.done") < [
        name for name, _ in events
    ].index("gates.done") < [name for name, _ in events].index("verifier.done")
    assert observed.answer.text == baseline.answer.text
    assert observed.answer_snapshot["outcome"] == baseline.answer_snapshot["outcome"]
    assert observed.validated_claim_plan.status == baseline.validated_claim_plan.status


def test_web_source_child_emits_the_same_progress_event_families() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    run_v1_graph(
        **_graph_kwargs(
            analyst=GraphAnalystProvider(_ready_decision),
            writer=GraphWriterProvider(_writer_text_factory()),
        ),
        web_source_title="Apple raises full-year guidance",
        web_source_company_name="Apple Inc.",
        on_step=lambda name, payload: events.append((name, payload)),
    )
    names = [name for name, _ in events]
    assert names.count("analyst.decision") == 2
    assert names.index("analyst.decision") < names.index("checks.done")
    assert names.index("checks.done") < names.index("gates.done") < names.index("verifier.done")
    assert "answer.ready" in names


def test_t1_graph_skips_retrieval_and_models_and_emits_tier_only_answer() -> None:
    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(_writer_text_factory())
    kwargs = _graph_kwargs(analyst=analyst, writer=writer)
    kwargs["observation_provider"] = OrdinaryObservationProvider()
    events: list[tuple[str, dict[str, Any]]] = []
    result = run_v1_graph(
        **kwargs, on_step=lambda name, payload: events.append((name, payload))
    )

    assert result.tier_decision.tier == "T1"
    assert result.state is None
    assert result.evidence_state is None
    assert result.coverage_summary is None
    assert result.context_pack is None
    assert isinstance(result, V1TierOnlyRunResult)
    assert kwargs["retriever"].calls == 0
    assert analyst.calls == 0
    assert writer.calls == 0
    assert result.logical_model_call_count == 0
    assert [name for name, _ in events] == [
        "facts.ready", "tier.decided", "answer.ready"
    ]
    assert "We did not look for a stock-specific story." in result.answer.text
    assert "no cause" not in result.answer.text.lower()
    assert result.answer_snapshot["outcome"] == "TIER_ONLY"
    assert result.answer_snapshot["tier"] == "T1"
    assert result.answer_snapshot["key_sentence"] is None


@pytest.mark.parametrize(("tier", "analysed"), [("T2", True), ("T1", False)])
def test_event_time_p1_keeps_p_tier_gate(tier, analysed, monkeypatch) -> None:
    from types import SimpleNamespace

    from catalyst_agents.attribution import tiers
    from catalyst_agents.attribution.policy import resolve_attribution_policy

    monkeypatch.setattr(tiers, "decide_tier", lambda *args, **kwargs: SimpleNamespace(
        tier=tier, reason_text="test tier", governing_ref=None,
        to_dict=lambda: {"tier": tier},
    ))
    analyst = GraphAnalystProvider(_ready_decision)
    kwargs = _graph_kwargs(analyst=analyst, writer=GraphWriterProvider(_writer_text_factory()))
    kwargs["attribution_policy"] = resolve_attribution_policy(
        {}, evidence_window_policy="event_time_p1"
    )
    result = run_v1_graph(**kwargs)
    # T2 runs the full analysis (research rounds may add calls); T1 never reaches the analyst.
    assert (analyst.calls >= 1) if analysed else (analyst.calls == 0)
    if tier == "T2":
        assert result.answer_snapshot["outcome"] != "TIER_ONLY"
    else:
        assert result.answer_snapshot["outcome"] == "TIER_ONLY"


def test_event_time_p1_graph_selects_p_analyst_schema() -> None:
    from catalyst_agents.attribution.analyst import AnalystDecision
    from catalyst_agents.attribution.policy import resolve_attribution_policy

    class SchemaRecordingAnalyst(GraphAnalystProvider):
        schema = None

        def with_structured_output(self, schema):
            self.schema = schema
            return super().with_structured_output(schema)

    analyst = SchemaRecordingAnalyst(_ready_decision)
    kwargs = _graph_kwargs(analyst=analyst, writer=GraphWriterProvider(_writer_text_factory()))
    kwargs["attribution_policy"] = resolve_attribution_policy(
        {}, evidence_window_policy="event_time_p1"
    )
    run_v1_graph(**kwargs)
    assert analyst.schema is AnalystDecision


def test_force_full_runs_pipeline_for_t1_day() -> None:
    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(_writer_text_factory())
    kwargs = _graph_kwargs(analyst=analyst, writer=writer)
    kwargs["observation_provider"] = OrdinaryObservationProvider()
    result = run_v1_graph(**kwargs, force_full=True)
    assert result.tier_decision.tier == "T1"
    assert kwargs["retriever"].calls > 0
    assert analyst.calls == 1
    assert result.answer_snapshot["tier"] == "T1"


def test_v1_graph_abstain_path_terminates_valid() -> None:
    analyst = GraphAnalystProvider(_abstain_decision)
    writer = GraphWriterProvider(
        "OBSERVED_MOVE\nAAPL +9.5%.\nLIMITATIONS\nNo causal explanation was established from the available evidence."
    )
    result = run_v1_graph(**_graph_kwargs(analyst=analyst, writer=writer))
    assert result.logical_model_call_count == 1  # Analyst only (Q1c)
    assert result.terminal_envelope["final_status"] == "ABSTAIN"
    assert result.terminal_envelope["assured"] is True
    assert result.validated_claim_plan.status.value == "ABSTAIN"


def _writer_role_bracket_abstain_factory(messages):
    """Copy the production Writer prompt shape that live c05 emitted."""
    import re

    prompt = messages[0]["content"] if isinstance(messages[0], dict) else messages[0].content
    claim_ids = re.findall(r"claim_id: ([A-Za-z0-9:_-]+)", prompt)
    lines = [
        "## OBSERVED_MOVE",
        "AAPL +9.50% on 2026-01-15.",
        "",
        "## LIMITATIONS",
        "No causal explanation was established from the available evidence.",
    ]
    for claim_id in claim_ids:
        lines.append(f"- [LIMITATION] (claim_id: {claim_id}) Coverage gap: MISSING_PRIMARY_CONFIRMATION for COMPANY_PRIMARY.")
    return "\n".join(lines)


def test_v1_graph_abstain_writer_role_brackets_do_not_fail_assurance() -> None:
    """Live c05 fingerprint: ABSTAIN Writer copies ``[LIMITATION] (claim_id: ...)``
    from the prompt. Structural assurance must not treat the role tag as a
    claim ID; the run completes ABSTAIN.
    """
    analyst = GraphAnalystProvider(_abstain_decision)
    writer = GraphWriterProvider(_writer_role_bracket_abstain_factory)
    result = run_v1_graph(**_graph_kwargs(analyst=analyst, writer=writer))
    assert writer.calls == 0  # Q1c: answers are code-rendered
    # WP-C C3c: ABSTAIN model text travels on the prose channel, so copied
    # brackets/parentheses are deleted and can never become markers.
    assert "[LIMITATION]" not in result.answer.text
    assert "LIMITATION" in result.answer.text
    assert "claim_id:" not in result.answer.text  # code-rendered ABSTAIN text
    assert result.terminal_envelope["assured"] is True
    assert result.terminal_envelope["final_status"] == "ABSTAIN"
    assert result.validated_claim_plan.status.value == "ABSTAIN"


def _writer_prompt_format_sufficient_factory(evidence_id: str = "e1"):
    import re

    def factory(messages):
        prompt = messages[0]["content"] if isinstance(messages[0], dict) else messages[0].content
        match = re.search(r"claim_id: ([A-Za-z0-9:_-]+)", prompt)
        claim_id = match.group(1) if match else "claim:1"
        return (
            "SUMMARY\n"
            f"- [PRIMARY] (claim_id: {claim_id}) AAPL rose on record guidance. "
            f"[citations: {evidence_id}]\n"
            "CAUSAL_EXPLANATION\nGuidance raised forward revenue.\n"
            "LIMITATIONS\nNone."
        )

    return factory


def test_v1_graph_sufficient_writer_prompt_markers_pass_assurance() -> None:
    """Writer prompt emits ``[PRIMARY] (claim_id: ...) [citations: e1]``.
    Assurance must bind those as claim/citation markers, not the role tag.
    """
    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(_writer_prompt_format_sufficient_factory())
    result = run_v1_graph(**_graph_kwargs(analyst=analyst, writer=writer))
    assert "[PRIMARY]" in result.answer.text
    assert "[citations: e1]" in result.answer.text
    assert result.terminal_envelope["assured"] is True
    assert result.terminal_envelope["final_status"] == "SUFFICIENT"


def test_v1_graph_metadata_only_empty_citable_inventory_skips_analyst() -> None:
    """Live c02 shape through the production graph: retrieved rows are all
    METADATA_ONLY, included_evidence_ids is empty, renderer emits
    inventory=NONE, Analyst provider calls are 0, Writer still runs the
    fixed LIMITATION path, and the run completes ABSTAIN."""
    from dataclasses import replace

    class MetadataOnlyRetriever(GraphRetriever):
        def _evidence(self, chunk_id: str, rank: int):
            return replace(
                super()._evidence(chunk_id, rank),
                content_state="METADATA_ONLY",
                material_capability="NOT_CAPABLE",
                content_text="",
            )

    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(
        "OBSERVED_MOVE\nAAPL +9.5%.\nLIMITATIONS\nNo causal explanation was established from the available evidence."
    )
    kwargs = _graph_kwargs(analyst=analyst, writer=writer)
    kwargs["retriever"] = MetadataOnlyRetriever()
    persistence = kwargs["persistence"]
    result = run_v1_graph(**kwargs)

    assert analyst.calls == 0
    assert writer.calls == 0  # Q1c: answers are code-rendered
    assert result.analyst_logical_calls == 0
    assert result.analyst_provider_attempts == 0
    assert result.writer_logical_calls == 0  # Q1c: code-rendered answer
    assert result.logical_model_call_count == 0  # no Analyst (empty inventory) and no Writer (Q1c)
    assert result.context_pack.included_evidence_ids == ()
    assert result.context_pack.evidence_inventory
    assert all(
        item.content_state == "METADATA_ONLY"
        for item in result.context_pack.evidence_inventory
    )
    assert result.terminal_envelope["final_status"] == "ABSTAIN"
    assert result.terminal_envelope["assured"] is True
    assert result.validated_claim_plan.status.value == "ABSTAIN"
    pair = persistence.load_pair(run_id="run:v1")
    inventory_lines = [
        message.content
        for message in pair.rendered_messages
        if message.content.startswith("inventory=")
    ]
    assert inventory_lines == ["inventory=NONE"]
    assert all(
        item.evidence_id not in " ".join(inventory_lines)
        for item in result.context_pack.evidence_inventory
    )


def test_v1_graph_title_only_empty_citable_inventory_skips_analyst() -> None:
    """Live c05 shape: retrieved rows are TITLE_ONLY (plus METADATA_ONLY), so
    included_evidence_ids is empty, Analyst is not dispatched, and the run
    completes ABSTAIN without a corrective round.
    """
    from dataclasses import replace

    class TitleOnlyRetriever(GraphRetriever):
        def _evidence(self, chunk_id: str, rank: int):
            return replace(
                super()._evidence(chunk_id, rank),
                content_state="TITLE_ONLY",
                material_capability="LEAD_ONLY",
                content_text="TSLA stock has given up its prior gain.",
            )

    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(
        "OBSERVED_MOVE\nAAPL +9.5%.\nLIMITATIONS\nNo causal explanation was established from the available evidence."
    )
    kwargs = _graph_kwargs(analyst=analyst, writer=writer)
    kwargs["retriever"] = TitleOnlyRetriever()
    result = run_v1_graph(**kwargs)
    assert analyst.calls == 0
    assert writer.calls == 0  # Q1c: answers are code-rendered
    assert result.analyst_logical_calls == 0
    assert result.analyst_provider_attempts == 0
    assert result.corrective_rounds == 0
    assert result.context_pack.included_evidence_ids == ()
    assert all(
        item.content_state == "TITLE_ONLY"
        for item in result.context_pack.evidence_inventory
    )
    assert result.terminal_envelope["final_status"] == "ABSTAIN"
    assert result.terminal_envelope["assured"] is True


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


# ── M6 corrective: cooperative control + absolute deadline boundaries ───────

def test_run_deadline_bounds_stage_timeout_before_provider_dispatch() -> None:
    """The run deadline, not the 30s stage budget, governs stage execution.

    A run deadline already in the past must fail before any provider dispatch
    even though research_stage_timeout_seconds is large."""
    from catalyst_agents.graphs.graph import build_foundation_graph
    from catalyst_agents.retrieval.execution import ResearchDeadlineError
    from catalyst_agents.runtime.pack_persistence import InMemoryPackStore

    retriever = GraphRetriever()
    now_ms = int(time.time() * 1000)
    with pytest.raises(ResearchDeadlineError):
        build_foundation_graph(
            run_id="run:deadline",
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
            research_stage_timeout_seconds=30.0,
            persistence=InMemoryPackStore(),
            packing_policy_version="pack:v1",
            template_bytes=b"system: analyse\n",
            template_version="prompt:v1",
            run_deadline_epoch_ms=now_ms - 1,
        )
    # The provider was never dispatched: the run deadline bound the stage.
    assert retriever.calls == 0


def test_control_cancellation_during_retrieval_prevents_writer_dispatch() -> None:
    """Cancellation observed after retrieval prevents the Writer from running."""
    from catalyst_agents.runtime.control import RunCancelledError

    class FlipControl:
        def __init__(self):
            self.armed = False

        def should_cancel(self) -> bool:
            return self.armed

        def deadline_epoch_ms(self) -> int | None:
            return None

        def cancellation_reason(self) -> str | None:
            return "test_cancel"

    control = FlipControl()
    retriever = GraphRetriever()

    class CancellingRetriever(GraphRetriever):
        def retrieve(self, query, *, ticker, cutoff, requested_manifest_id,
                     temporal_identity=None, top_k=8, candidate_depth=20, source_classes=None, temporal_centre_date=None):
            result = super().retrieve(
                query, ticker=ticker, cutoff=cutoff,
                requested_manifest_id=requested_manifest_id,
                temporal_identity=temporal_identity, top_k=top_k,
                candidate_depth=candidate_depth,
            )
            control.armed = True  # cancellation wins right after retrieval
            return result

    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider("SUMMARY\nAAPL.\nLIMITATIONS\nNone.")
    kwargs = _graph_kwargs(analyst=analyst, writer=writer)
    kwargs["retriever"] = CancellingRetriever()
    kwargs["control"] = control

    with pytest.raises(RunCancelledError):
        run_v1_graph(**kwargs)

    # The Writer was never dispatched: no deltas/answer after cancellation.
    assert writer.calls == 0


def test_control_cancellation_before_analyst_prevents_writer_dispatch() -> None:
    """Cancellation observed before the Analyst prevents later stages."""
    from catalyst_agents.runtime.control import RunCancelledError

    class PreCancelControl:
        def should_cancel(self) -> bool:
            return True

        def deadline_epoch_ms(self) -> int | None:
            return None

        def cancellation_reason(self) -> str | None:
            return "pre_cancel"

    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider("SUMMARY\nAAPL.\nLIMITATIONS\nNone.")
    kwargs = _graph_kwargs(analyst=analyst, writer=writer)
    kwargs["control"] = PreCancelControl()

    with pytest.raises(RunCancelledError):
        run_v1_graph(**kwargs)
    assert analyst.calls == 0
    assert writer.calls == 0


def test_v1_graph_observation_cutoff_is_canonical_z(monkeypatch) -> None:
    """The graph must hand the observation stage a canonical ``...Z`` cutoff.

    ``TemporalIdentity.cutoff_at`` is already bound and is never recomputed
    here; only its serialization was wrong. ``datetime.isoformat()`` renders
    UTC as ``...+00:00``, which is not the canonical form the context layer
    compares against its ``...Z`` rows. ``ObservationBuilder.build`` normalizes
    defensively, but the graph must not emit the non-canonical form at all.
    """
    # The observation stage lives in the foundation module (moved from graph.py).
    import catalyst_agents.foundation as graph_module

    real_builder = graph_module.ObservationBuilder
    seen: list[str] = []

    class _RecordingBuilder:
        def __init__(self, *, provider, policy):
            self._inner = real_builder(provider=provider, policy=policy)

        def build(self, **kwargs):
            seen.append(kwargs["cutoff"])
            return self._inner.build(**kwargs)

    monkeypatch.setattr(graph_module, "ObservationBuilder", _RecordingBuilder)

    analyst = GraphAnalystProvider(_ready_decision)
    writer = GraphWriterProvider(_writer_text_factory())
    run_v1_graph(**_graph_kwargs(analyst=analyst, writer=writer))

    assert seen, "the observation stage was never invoked"
    assert seen[0] == "2026-01-15T21:00:00Z", seen[0]
    assert "+00:00" not in seen[0]


class _NoContextObservationProvider(FakeObservationProvider):
    """Observation with no point-in-time market/sector/peer series (V1.1)."""

    def load_context_inputs(self, *, ticker, session_date, cutoff,
                            information_window_start_at=None) -> ContextInputs:
        from dataclasses import replace

        base = super().load_context_inputs(
            ticker=ticker,
            session_date=session_date,
            cutoff=cutoff,
            information_window_start_at=information_window_start_at,
        )
        return replace(
            base,
            benchmark_ticker=None,
            benchmark_return_pct=None,
            sector_ticker=None,
            sector_return_pct=None,
            peer_returns_by_ticker={},
            # Only the three market/sector/peer gaps are decided by adequacy;
            # every other UNAVAILABLE gap still vetoes, so the macro source is
            # present here and the pairing is what is under test.
            macro_source_available=True,
        )


class _EmptyRetriever(GraphRetriever):
    def retrieve(self, *args, **kwargs):
        self.calls += 1
        return ()


def test_v1_graph_empty_citable_pack_delivers_abstain_no_material() -> None:
    """WP-C C2 / Frozen-A4: the empty-citable-pack path (the Analyst fast path)
    still runs the NO_MATERIAL gates — it is never exempt. With no point-in-time
    market/sector/peer series (V1.1), a gates-passing result is ABSTAIN +
    NO_MATERIAL_PUBLIC_CATALYST, and the Writer still runs the fixed abstention
    path."""
    analyst = GraphAnalystProvider(_ready_decision)  # never called: empty pack
    writer = GraphWriterProvider(
        "OBSERVED_MOVE\nAAPL +9.5%.\nLIMITATIONS\nNo causal explanation was established from the available evidence."
    )
    kwargs = _graph_kwargs(analyst=analyst, writer=writer)
    kwargs["retriever"] = _EmptyRetriever()
    kwargs["observation_provider"] = _NoContextObservationProvider()
    result = run_v1_graph(**kwargs)

    assert analyst.calls == 0
    assert writer.calls == 0  # Q1c: answers are code-rendered
    assert result.context_pack.included_evidence_ids == ()
    assert result.assessment.no_material_gates_passed is True
    assert result.terminal_envelope["final_status"] == "ABSTAIN"
    assert result.terminal_envelope["attribution_type"] == "NO_MATERIAL_PUBLIC_CATALYST"
    assert result.validated_claim_plan.status.value == "ABSTAIN"
    assert result.terminal_envelope["assured"] is True
