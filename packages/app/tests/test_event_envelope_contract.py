"""V1.1 public event envelope contract tests (M2-8, corrective).

Closed event taxonomy with closed typed payload models per RunEventType;
Frozen public names sequence/emitted_at (API-01); 64 KiB payload cap;
answer.delta 2,048 UTF-8 character cap; typed ArtifactRef; raw provider
responses, credentials, headers and reasoning are structurally impossible.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_app.events import (
    AnswerDeltaPayload,
    ArtifactRef,
    HEARTBEAT_COMMENT,
    PublicRunEvent,
    RunAcceptedPayload,
    RunFailedPayload,
    RunEventType,
    serialize_public_event,
    sse_frame,
)


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
        "evidence.reranked",
        "evidence.assessed",
        "followup.started",
        "answer.started",
        "answer.delta",
        "answer.completed",
        "assurance.completed",
        "run.completed",
        "run.failed",
        "run.cancelled",
    ]
    with pytest.raises(ValidationError):
        PublicRunEvent(**_event(event_type="unknown.event"))


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
    ):
        with pytest.raises(ValidationError) as exc_info:
            RunFailedPayload(failure_code="provider_failure", safe_message=unsafe)
        assert unsafe not in str(exc_info.value)


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
