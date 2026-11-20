from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from catalyst_agents.graphs.research_context import (
    FoundationGraphInputs,
    FoundationRetrievalStageInput,
    run_foundation_retrieval_stage,
)


class _Trace:
    def __init__(self):
        self.payload = {}
        self.started = []
        self.ended = []
        self.retrievals = []

    def begin(self, name, **kwargs):
        self.started.append((name, kwargs))
        return {"duration_ms": 7}

    def end(self, stage):
        self.ended.append(stage)

    def retrieval(self, *, evidence_state, context_pack, research_tasks=()):
        self.retrievals.append((evidence_state, context_pack))
        self.payload["retrieval"] = {"evidence_state_hash": "evidence-hash"}


def _foundation_inputs(temporal_identity):
    return FoundationGraphInputs(
        run_id="run:test", round=1, temporal_identity=temporal_identity,
        data_runtime_identity=object(), ticker="ACME", cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="manifest", observation_provider=object(), retriever=object(),
        policy_config=object(), research_concurrency=2, research_stage_timeout_seconds=10.0,
        persistence=object(), packing_policy_version="pack:v1", template_bytes=b"template",
        template_version="template:v1", structured_provider=None, run_deadline_epoch_ms=None,
        observation_policy_version="observation:v1", move_profile_override=object(),
    )


def _foundation():
    evidence = SimpleNamespace(
        evidence_id="e1", publisher="Publisher", source_class="news",
        eligible_at=datetime(2026, 1, 15, 17, tzinfo=timezone.utc),
        canonical_url="https://example.test/story", content_state="FULL_TEXT",
    )
    pack = SimpleNamespace(
        evidence_inventory=(evidence,), included_evidence_ids=("e1",), lead_only_evidence=(),
    )
    return SimpleNamespace(evidence_state="evidence", context_pack=pack)


def test_initial_foundation_stage_preserves_builder_injection_and_retrieval_event():
    temporal_identity = SimpleNamespace(session_open_at=datetime(2026, 1, 15, 14, tzinfo=timezone.utc))
    trace = _Trace()
    events = []
    calls = []
    foundation = _foundation()

    def builder(**kwargs):
        calls.append(kwargs)
        return foundation

    result = run_foundation_retrieval_stage(FoundationRetrievalStageInput(
        foundation_inputs=_foundation_inputs(temporal_identity), foundation_builder=builder,
        trace=trace, emit=lambda name, payload: events.append((name, payload)),
        stage_name="research_and_context_pack", round=1,
        temporal_identity=temporal_identity, control=None, cascade_retrieval_stage=1,
    ))

    assert result.foundation is foundation
    assert result.duration_ms == 7
    assert calls[0]["analyst_boundary"] is None
    assert calls[0]["on_facts_ready"] is None
    assert trace.started == [("research_and_context_pack", {"round": 1})]
    assert trace.ended == [{"duration_ms": 7}]
    assert trace.payload["cascade_retrievals"] == [
        {"cascade_stage": 1, "evidence_state_hash": "evidence-hash"}
    ]
    assert events == [("retrieval.done", {
        "inventory_count": 1, "included_in_pack": 1, "lead_only": 0,
        "items": [{
            "evidence_id": "e1", "title": None, "publisher": "Publisher",
            "published_at": "2026-01-15T17:00:00+00:00", "url": "https://example.test/story",
            "phase": "SESSION", "content_state": "FULL_TEXT",
        }],
        "duration_ms": 7,
    })]


def test_cascade_foundation_stage_records_round_and_appends_retrieval():
    temporal_identity = SimpleNamespace(session_open_at=datetime(2026, 1, 15, 14, tzinfo=timezone.utc))
    trace = _Trace()
    trace.payload["cascade_retrievals"] = [{"cascade_stage": 1, "evidence_state_hash": "first"}]
    foundation = _foundation()

    result = run_foundation_retrieval_stage(FoundationRetrievalStageInput(
        foundation_inputs=_foundation_inputs(temporal_identity), foundation_builder=lambda **_: foundation,
        trace=trace, emit=lambda *_: None,
        stage_name="cascade_stage_2_research_and_context_pack", round=2,
        temporal_identity=temporal_identity, control=None,
        trace_cascade_stage=2, cascade_retrieval_stage=2,
    ))

    assert result.foundation is foundation
    assert trace.started == [("cascade_stage_2_research_and_context_pack", {"round": 2, "cascade_stage": 2})]
    assert trace.payload["cascade_retrievals"] == [
        {"cascade_stage": 1, "evidence_state_hash": "first"},
        {"cascade_stage": 2, "evidence_state_hash": "evidence-hash"},
    ]
