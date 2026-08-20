"""V1.1 public run event envelope contract (M2-8).

The closed persisted event taxonomy and the public envelope (Final Migration
TSD §17; Frozen §8.2). Public names sequence/emitted_at are normative (API-01);
internal columns seq/occurred_at never surface in public JSON. SSE framing:
id: {run_id}:{sequence}, event: {event_type}; heartbeat is a non-persisted
comment.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunEventType(str, Enum):
    RUN_ACCEPTED = "run.accepted"
    STAGE_STARTED = "stage.started"
    EVIDENCE_RETRIEVED = "evidence.retrieved"
    EVIDENCE_RERANKED = "evidence.reranked"
    EVIDENCE_ASSESSED = "evidence.assessed"
    FOLLOWUP_STARTED = "followup.started"
    ANSWER_STARTED = "answer.started"
    ANSWER_DELTA = "answer.delta"
    ANSWER_COMPLETED = "answer.completed"
    ASSURANCE_COMPLETED = "assurance.completed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


class PublicRunEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    event_id: str | None = None
    run_id: str
    sequence: int = Field(ge=1)
    event_type: RunEventType
    emitted_at: datetime
    stage: str
    payload: dict[str, Any] = {}
    artifact_refs: tuple[str, ...] = ()


def serialize_public_event(event: PublicRunEvent) -> str:
    """Serialize the public envelope using the normative public names."""
    return event.model_dump_json()


def sse_frame(event: PublicRunEvent) -> str:
    """Frame one public event per the SSE contract."""
    return (
        f"id: {event.run_id}:{event.sequence}\n"
        f"event: {event.event_type.value}\n"
        f"data: {serialize_public_event(event)}\n\n"
    )


HEARTBEAT_COMMENT = ": heartbeat\n\n"


__all__ = [
    "HEARTBEAT_COMMENT",
    "PublicRunEvent",
    "RunEventType",
    "serialize_public_event",
    "sse_frame",
]
