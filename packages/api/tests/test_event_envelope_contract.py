"""V1.1 public event envelope contract tests (M2-8, corrective).

Closed event taxonomy with closed typed payload models per RunEventType;
Frozen public names sequence/emitted_at (API-01); 64 KiB payload cap;
answer.delta 2,048 UTF-8 character cap; typed ArtifactRef; raw provider
responses, credentials, headers and reasoning are structurally impossible.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from catalyst_app.events import (
    HEARTBEAT_COMMENT,
    AnalystDecisionPayload,
    AnalystDonePayload,
    AnswerDeltaPayload,
    AnswerReadyPayload,
    ArtifactRef,
    ChecksDonePayload,
    FactsReadyPayload,
    GatesDonePayload,
    PublicRunEvent,
    RetrievalDonePayload,
    RunAcceptedPayload,
    RunEventType,
    RunFailedPayload,
    TierDecidedPayload,
    VerifierDonePayload,
    serialize_public_event,
    sse_frame,
)
from pydantic import ValidationError


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _accepted_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "request_identity_digest": "a" * 64,
        "ticker": "AAPL",
        "trade_date": "2026-01-06",
        "workflow_version": "v1.1",
        "model_provider_label": "test-provider/model",
        "stream_url": "/api/live-runs/run:1/stream",
    }
    base.update(overrides)
    return base


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "v1",
        "event_id": None,
        "run_id": "run:1",
        "sequence": 1,
        "event_type": "run.accepted",
        "emitted_at": _utc("2026-01-06T14:00:00Z"),
        "stage": "ADMISSION",
        "payload": RunAcceptedPayload(**_accepted_payload()),
        "artifact_refs": (),
    }
    base.update(overrides)
    return base


def test_event_taxonomy_is_closed() -> None:
    assert [member.value for member in RunEventType] == [
        "run.accepted",
        "stage.started",
        "evidence.retrieved",
        "evidence.assessed",
        "followup.started",
        "answer.started",
        "answer.delta",
        "answer.completed",
        "assurance.completed",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "facts.ready",
        "retrieval.done",
        "analyst.done",
        "analyst.decision",
        "checks.done",
        "gates.done",
        "verifier.done",
        "answer.ready",
        "tier.decided",
        "investigator.done",
    ]
    with pytest.raises(ValidationError):
        PublicRunEvent(**_event(event_type="unknown.event"))


def test_progressive_payloads_are_typed_bounded_and_registered() -> None:
    payloads = [
        (RunEventType.FACTS_READY, FactsReadyPayload(
            window={"information_window_start_at": "start", "cutoff_at": "cutoff", "session_open_at": "open"},
            facts={"target_return_pct": 1.0}, headline="AAPL rose.",
        )),
        (RunEventType.RETRIEVAL_DONE, RetrievalDonePayload(
            inventory_count=41, included_in_pack=1, lead_only=0,
            items=[{"evidence_id": f"e{i}", "title": None, "publisher": "p", "published_at": None,
                    "url": None, "phase": "SESSION", "content_state": "FULL_TEXT"} for i in range(41)],
            duration_ms=3,
        )),
        (RunEventType.ANALYST_DONE, AnalystDonePayload(
            duration_ms=1, evidence_decisions=[{"evidence_id": "e1", "disposition": "SUPPORT",
            "reason_code": "R", "note": "n" * 500}], hypotheses=[], research_decision="READY",
        )),
        (RunEventType.ANALYST_DECISION, AnalystDecisionPayload(
            kind="evidence", evidence_id="e1", disposition="SUPPORT",
            reason_code="supports", note=None,
        )),
        (RunEventType.CHECKS_DONE, ChecksDonePayload(quotes=[], gates=[], duration_ms=1)),
        (RunEventType.GATES_DONE, GatesDonePayload(gates=[], duration_ms=1)),
        (RunEventType.VERIFIER_DONE, VerifierDonePayload(called=False, skipped_because="no_primary")),
        (RunEventType.ANSWER_READY, AnswerReadyPayload(answer_snapshot={"outcome": "ABSTAINED"})),
        (RunEventType.TIER_DECIDED, TierDecidedPayload(
            tier="T1", governing_ref=None, overrides=(),
            reason_code="ORDINARY_CLOSE_REFERENCES_UNAVAILABLE",
            reason_text="We did not look for a stock-specific story.", threshold_used=1.0,
        )),
    ]
    for event_type, payload in payloads:
        event = PublicRunEvent(**_event(event_type=event_type, payload=payload))
        assert event.payload is payload
        assert len(payload.model_dump_json().encode("utf-8")) <= 64 * 1024
    evidence_decision = payloads[3][1].model_dump()
    assert evidence_decision["kind"] == "evidence"
    assert "ref" not in evidence_decision

    retrieval = payloads[1][1]
    assert len(retrieval.items) == 40
    assert retrieval.truncated is True
    analyst = payloads[2][1]
    assert len(analyst.evidence_decisions[0].note) <= 300
    assert analyst.truncated is True
    answer_payload = next(payload for event, payload in payloads if event is RunEventType.ANSWER_READY)
    assert answer_payload.answer_snapshot["outcome"] == "ABSTAINED"

    oversized_snapshot = AnswerReadyPayload(
        answer_snapshot={"outcome": "ABSTAINED", "sources": ["x" * 4000 for _ in range(100)]}
    )
    assert oversized_snapshot.truncated is True
    assert len(oversized_snapshot.model_dump_json().encode("utf-8")) <= 64 * 1024


def test_public_run_event_is_strict_and_frozen() -> None:
    event = PublicRunEvent(**_event())
    with pytest.raises(ValidationError):
        event.sequence = 2  # frozen
    with pytest.raises(ValidationError):
        PublicRunEvent(**_event(), unknown_field=True)  # extra forbidden


def test_public_names_sequence_and_emitted_at_are_normative() -> None:
    event = PublicRunEvent(**_event())
    data = json.loads(serialize_public_event(event))
    assert data["sequence"] == 1
    assert data["emitted_at"] == "2026-01-06T14:00:00Z"
    assert "seq" not in data
    assert "occurred_at" not in data


def test_sequence_is_positive() -> None:
    with pytest.raises(ValidationError):
        PublicRunEvent(**_event(sequence=0))
    with pytest.raises(ValidationError):
        PublicRunEvent(**_event(sequence=-1))


def test_payload_is_typed_and_bound_to_event_type() -> None:
    event = PublicRunEvent(**_event())
    assert isinstance(event.payload, RunAcceptedPayload)
    with pytest.raises(ValidationError):
        PublicRunEvent(
            **_event(
                event_type="evidence.retrieved",
                payload=RunAcceptedPayload(**_accepted_payload()),
            )
        )


def test_payload_forbids_secrets_and_raw_provider_fields() -> None:
    with pytest.raises(ValidationError):
        PublicRunEvent(
            **_event(
                payload={
                    **_accepted_payload(),
                    "api_key": "sk-secret",
                }
            )
        )
    with pytest.raises(ValidationError):
        PublicRunEvent(
            **_event(
                payload={
                    **_accepted_payload(),
                    "raw_provider_response": {"choices": []},
                }
            )
        )
    with pytest.raises(ValidationError):
        PublicRunEvent(
            **_event(
                payload={
                    **_accepted_payload(),
                    "headers": {"Authorization": "Bearer x"},
                }
            )
        )
    with pytest.raises(ValidationError):
        PublicRunEvent(
            **_event(
                payload={
                    **_accepted_payload(),
                    "critic_reasoning": "hidden chain of thought",
                }
            )
        )


def test_safe_failure_message_rejects_secret_and_raw_provider_patterns() -> None:
    for unsafe in (
        "api_key=sk-secret-value",
        "provider_key: abcdef",
        "raw provider response: {choices: []}",
        "request failed sk-abcdefghijklmnopqrstuvwxyz012345",
    ):
        with pytest.raises(ValidationError) as exc_info:
            RunFailedPayload(failure_code="provider_failure", safe_message=unsafe)
        assert unsafe not in str(exc_info.value)


def test_stored_json_event_parses_with_its_own_payload_type() -> None:
    # A gates.done payload is also a valid checks.done payload; reading a
    # stored event back must not let the union pick checks.done.
    gates = {
        "gates": [{"gate": "timing", "result": "no_change"}],
        "duration_ms": 5,
        "truncated": False,
    }
    stored = json.loads(
        serialize_public_event(
            PublicRunEvent(
                **_event(
                    event_type="gates.done",
                    stage="GATES",
                    payload=GatesDonePayload(**gates),
                )
            )
        )
    )
    assert isinstance(PublicRunEvent.model_validate(stored).payload, GatesDonePayload)
    assert isinstance(
        PublicRunEvent.model_validate_json(json.dumps(stored)).payload,
        GatesDonePayload,
    )
    checks = {**stored, "event_type": "checks.done"}
    assert isinstance(PublicRunEvent.model_validate(checks).payload, ChecksDonePayload)
    with pytest.raises(ValidationError):
        PublicRunEvent.model_validate({**stored, "payload": {"duration_ms": -1}})


def test_event_envelope_rejection_never_echoes_nested_failure_secret() -> None:
    secret = "sk-envelope-secret-abcdefghijklmnopqrstuvwxyz012345"
    with pytest.raises(ValidationError) as exc_info:
        PublicRunEvent(
            **_event(
                event_type="run.failed",
                payload={
                    "failure_code": "provider_failure",
                    "safe_message": f"api_key={secret}",
                },
            )
        )
    assert secret not in str(exc_info.value)


def test_payload_cannot_carry_full_context_or_evidence_arrays() -> None:
    with pytest.raises(ValidationError):
        PublicRunEvent(
            **_event(
                payload={
                    **_accepted_payload(),
                    "evidence": [{"evidence_id": "e1"}],
                }
            )
        )
    with pytest.raises(ValidationError):
        PublicRunEvent(
            **_event(
                payload={
                    **_accepted_payload(),
                    "context_pack": {"items": []},
                }
            )
        )


def test_answer_delta_is_bounded_to_2048_utf8_characters() -> None:
    delta = AnswerDeltaPayload(
        writer_stream_id="stream:1",
        delta_text="AAPL rose.",
        delta_ordinal=1,
        cumulative_character_count=10,
        provisional=True,
    )
    assert delta.delta_text == "AAPL rose."
    big = AnswerDeltaPayload(
        writer_stream_id="stream:1",
        delta_text="x" * 2048,
        delta_ordinal=1,
        cumulative_character_count=2048,
        provisional=True,
    )
    assert len(big.delta_text) == 2048
    with pytest.raises(ValidationError):
        AnswerDeltaPayload(
            writer_stream_id="stream:1",
            delta_text="x" * 2049,
            delta_ordinal=1,
            cumulative_character_count=2049,
            provisional=True,
        )


def test_public_payload_is_bounded_to_64_kib() -> None:
    oversized = RunAcceptedPayload(
        **_accepted_payload(
            request_identity_digest="z" * 70_000,
        )
    )
    assert len(oversized.model_dump_json().encode("utf-8")) > 65536
    with pytest.raises(ValidationError):
        PublicRunEvent(
            **_event(
                payload=RunAcceptedPayload(
                    **_accepted_payload(
                        request_identity_digest="z" * 70_000,
                    )
                )
            )
        )


def test_artifact_refs_are_typed_with_schema_and_hash_metadata() -> None:
    ref = ArtifactRef(
        artifact_id="artifact:1",
        artifact_type="evidence_state",
        schema_version="v1",
        content_sha256="c" * 64,
    )
    event = PublicRunEvent(**_event(artifact_refs=(ref,)))
    assert event.artifact_refs[0].content_sha256 == "c" * 64
    with pytest.raises(ValidationError):
        PublicRunEvent(**_event(artifact_refs=("artifact:1",)))  # untyped ref rejected
    with pytest.raises(ValidationError):
        ArtifactRef(artifact_id="artifact:2", artifact_type="claim_plan")
    with pytest.raises(ValidationError):
        ArtifactRef(
            artifact_id="artifact:2", artifact_type="claim_plan",
            schema_version="v1", content_sha256="not-a-hash",
        )


def test_sse_frame_uses_run_id_sequence_and_event_type() -> None:
    from catalyst_app.events import EvidenceRetrievedPayload

    event = PublicRunEvent(
        **_event(
            sequence=7,
            event_type="evidence.retrieved",
            payload=EvidenceRetrievedPayload(
                task_id="task-1",
                candidate_evidence_ids=("corpus:chunk:0001",),
                candidate_count=1,
            ),
        )
    )
    frame = sse_frame(event)
    assert frame.startswith("id: run:1:7\n")
    assert "event: evidence.retrieved\n" in frame
    assert '"run_id":"run:1"' in frame
    assert frame.endswith("\n\n")


def test_stage_is_optional_per_phase_5_11_1() -> None:
    event = PublicRunEvent(
        schema_version="v1",
        run_id="run:1",
        sequence=1,
        event_type="run.accepted",
        emitted_at=_utc("2026-01-06T14:00:00Z"),
        stage=None,
        payload=RunAcceptedPayload(**_accepted_payload()),
    )
    assert event.stage is None


def test_heartbeat_is_a_non_persisted_comment() -> None:
    assert HEARTBEAT_COMMENT.startswith(":")
    assert "event:" not in HEARTBEAT_COMMENT
    assert "id:" not in HEARTBEAT_COMMENT


def test_facts_ready_accepts_event_window_without_changing_default_payload() -> None:
    legacy = FactsReadyPayload(
        window={"information_window_start_at": "start", "cutoff_at": "cutoff", "session_open_at": "open"},
        facts={}, headline="AAPL rose.",
    )
    assert legacy.model_dump(mode="json")["window"] == {
        "information_window_start_at": "start", "cutoff_at": "cutoff", "session_open_at": "open",
    }
    event_time = FactsReadyPayload(
        window={"information_window_start_at": "start", "cutoff_at": "cutoff", "session_open_at": "open",
                "evidence_window_start_at": "lower", "evidence_window_end_at": "upper"},
        facts={}, headline="AAPL rose.",
    )
    assert event_time.model_dump(mode="json")["window"]["evidence_window_start_at"] == "lower"
