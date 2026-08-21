"""V1.1 public run event envelope contract (M2-8, corrective).

The closed persisted event taxonomy and the public envelope (Final Migration
TSD §17; Frozen §8.2). Every event type has a closed typed payload model; raw
provider responses, credentials, headers, reasoning and full context/evidence
arrays are structurally impossible. Public payloads are bounded to 64 KiB;
answer.delta text is bounded to 2,048 UTF-8 characters. Public names
sequence/emitted_at are normative (API-01); SSE framing: id: {run_id}:{sequence},
event: {event_type}; heartbeat is a non-persisted comment.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from catalyst_app.public_text import validate_safe_public_text

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_PUBLIC_PAYLOAD_BYTES = 64 * 1024  # 64 KiB compact public payload limit
MAX_ANSWER_DELTA_CHARACTERS = 2048


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


class ArtifactRef(BaseModel):
    """Stable artifact ref with type/schema/hash metadata (Phase 5 §11.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    artifact_type: str
    schema_version: str
    content_sha256: str

    @model_validator(mode="after")
    def _hash_shape(self) -> "ArtifactRef":
        if _SHA256_RE.fullmatch(self.content_sha256) is None:
            raise ValueError("content_sha256 must be a lowercase SHA-256 hex digest")
        return self


class RunAcceptedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_identity_digest: str
    ticker: str
    trade_date: str
    workflow_version: str
    model_provider_label: str
    stream_url: str


class StageStartedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str
    round: int | None = None
    task_id: str | None = None
    input_artifact_refs: tuple[str, ...] = ()
    deadline: datetime | None = None


class EvidenceRetrievedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    candidate_evidence_ids: tuple[str, ...] = ()
    candidate_count: int = Field(ge=0)
    retrieval_degradation: tuple[str, ...] = ()
    latency_ms: int | None = Field(default=None, ge=0)


class EvidenceRerankedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_evidence_ids: tuple[str, ...] = ()
    rank_changes: dict[str, int] = {}
    duplicate_drops: tuple[str, ...] = ()
    reranker_status: str
    latency_ms: int | None = Field(default=None, ge=0)


class EvidenceAssessedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted_evidence_ids: tuple[str, ...] = ()
    lead_only_evidence_ids: tuple[str, ...] = ()
    rejected_evidence_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    gap_ids: tuple[str, ...] = ()
    status_ceiling: str
    decision: str


class FollowUpStartedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    action_ids: tuple[str, ...] = ()
    gap_ids: tuple[str, ...] = ()
    evidence_needs: tuple[str, ...] = ()
    time_scopes: tuple[str, ...] = ()
    research_fingerprints: tuple[str, ...] = ()


class AnswerStartedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    writer_stream_id: str
    validated_claim_plan_ref: str
    validated_claim_plan_hash: str
    citation_ids: tuple[str, ...] = ()
    provisional: bool = True


class AnswerDeltaPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    writer_stream_id: str
    delta_text: str
    delta_ordinal: int = Field(ge=1)
    cumulative_character_count: int = Field(ge=0)
    provisional: bool = True

    @model_validator(mode="after")
    def _delta_bounded(self) -> "AnswerDeltaPayload":
        if len(self.delta_text) > MAX_ANSWER_DELTA_CHARACTERS:
            raise ValueError(
                "answer.delta text must not exceed 2,048 UTF-8 characters"
            )
        return self


class AnswerCompletedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    writer_stream_id: str
    final_text_hash: str
    citation_ids: tuple[str, ...] = ()
    usage_tokens_in: int | None = Field(default=None, ge=0)
    usage_tokens_out: int | None = Field(default=None, ge=0)
    provisional: bool = True

    @model_validator(mode="after")
    def _hash_shape(self) -> "AnswerCompletedPayload":
        if _SHA256_RE.fullmatch(self.final_text_hash) is None:
            raise ValueError("final_text_hash must be a lowercase SHA-256 hex digest")
        return self


class AssuranceCompletedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    violations: tuple[str, ...] = ()
    final_result_status: str | None = None
    provisional_invalidated: bool = False


class RunCompletedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result_status: str
    final_output_artifact_ref: str
    total_latency_ms: int = Field(ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    runtime_identity_ref: str


class RunFailedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    failure_code: str
    stage: str | None = None
    retryable: bool = False
    safe_message: str | None = None
    provisional_output_invalidated: bool = False

    @field_validator("safe_message")
    @classmethod
    def _safe_public_message(cls, value: str | None) -> str | None:
        return validate_safe_public_text(value)


class RunCancelledPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cancellation_reason: str
    stage: str | None = None
    acknowledgement_mode: str
    provisional_output_invalidated: bool = False


RunEventPayload = Union[
    RunAcceptedPayload,
    StageStartedPayload,
    EvidenceRetrievedPayload,
    EvidenceRerankedPayload,
    EvidenceAssessedPayload,
    FollowUpStartedPayload,
    AnswerStartedPayload,
    AnswerDeltaPayload,
    AnswerCompletedPayload,
    AssuranceCompletedPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunCancelledPayload,
]

_EVENT_PAYLOAD_TYPES: dict[RunEventType, type[BaseModel]] = {
    RunEventType.RUN_ACCEPTED: RunAcceptedPayload,
    RunEventType.STAGE_STARTED: StageStartedPayload,
    RunEventType.EVIDENCE_RETRIEVED: EvidenceRetrievedPayload,
    RunEventType.EVIDENCE_RERANKED: EvidenceRerankedPayload,
    RunEventType.EVIDENCE_ASSESSED: EvidenceAssessedPayload,
    RunEventType.FOLLOWUP_STARTED: FollowUpStartedPayload,
    RunEventType.ANSWER_STARTED: AnswerStartedPayload,
    RunEventType.ANSWER_DELTA: AnswerDeltaPayload,
    RunEventType.ANSWER_COMPLETED: AnswerCompletedPayload,
    RunEventType.ASSURANCE_COMPLETED: AssuranceCompletedPayload,
    RunEventType.RUN_COMPLETED: RunCompletedPayload,
    RunEventType.RUN_FAILED: RunFailedPayload,
    RunEventType.RUN_CANCELLED: RunCancelledPayload,
}


class PublicRunEvent(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True
    )

    schema_version: str
    event_id: str | None = None
    run_id: str
    sequence: int = Field(ge=1)
    event_type: RunEventType
    emitted_at: datetime
    stage: str | None = None
    payload: RunEventPayload
    artifact_refs: tuple[ArtifactRef, ...] = ()

    @model_validator(mode="after")
    def _payload_matches_event_type_and_size(self) -> "PublicRunEvent":
        expected = _EVENT_PAYLOAD_TYPES[self.event_type]
        if not isinstance(self.payload, expected):
            raise ValueError(
                f"payload type must match event type {self.event_type.value}"
            )
        payload_bytes = len(self.payload.model_dump_json().encode("utf-8"))
        if payload_bytes > MAX_PUBLIC_PAYLOAD_BYTES:
            raise ValueError(
                "public event payload must not exceed 64 KiB compact JSON"
            )
        return self


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
    "AnswerCompletedPayload",
    "AnswerDeltaPayload",
    "AnswerStartedPayload",
    "ArtifactRef",
    "AssuranceCompletedPayload",
    "EvidenceAssessedPayload",
    "EvidenceRerankedPayload",
    "EvidenceRetrievedPayload",
    "FollowUpStartedPayload",
    "HEARTBEAT_COMMENT",
    "MAX_ANSWER_DELTA_CHARACTERS",
    "MAX_PUBLIC_PAYLOAD_BYTES",
    "PublicRunEvent",
    "RunAcceptedPayload",
    "RunCancelledPayload",
    "RunCompletedPayload",
    "RunEventPayload",
    "RunEventType",
    "RunFailedPayload",
    "StageStartedPayload",
    "serialize_public_event",
    "sse_frame",
]
