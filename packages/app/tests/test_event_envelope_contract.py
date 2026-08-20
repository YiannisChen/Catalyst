"""V1.1 public event envelope contract tests (M2-8).

Closed event taxonomy, Frozen public names sequence/emitted_at (API-01), and
the SSE framing contract (Final Migration TSD §17/§18; Frozen §8.2).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_app.events import (
    HEARTBEAT_COMMENT,
    PublicRunEvent,
    RunEventType,
    serialize_public_event,
    sse_frame,
)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "v1",
        "event_id": None,
        "run_id": "run:1",
        "sequence": 1,
        "event_type": "run.accepted",
        "emitted_at": _utc("2026-01-06T14:00:00Z"),
        "stage": "ADMISSION",
        "payload": {"status": "ACCEPTED"},
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
    assert "event_id" in data or data.get("event_id") is None


def test_sequence_is_positive() -> None:
    with pytest.raises(ValidationError):
        PublicRunEvent(**_event(sequence=0))
    with pytest.raises(ValidationError):
        PublicRunEvent(**_event(sequence=-1))


def test_sse_frame_uses_run_id_sequence_and_event_type() -> None:
    event = PublicRunEvent(**_event(sequence=7, event_type="evidence.retrieved"))
    frame = sse_frame(event)
    assert frame.startswith("id: run:1:7\n")
    assert "event: evidence.retrieved\n" in frame
    assert '"run_id":"run:1"' in frame
    assert frame.endswith("\n\n")


def test_heartbeat_is_a_non_persisted_comment() -> None:
    assert HEARTBEAT_COMMENT.startswith(":")
    assert "event:" not in HEARTBEAT_COMMENT
    assert "id:" not in HEARTBEAT_COMMENT
