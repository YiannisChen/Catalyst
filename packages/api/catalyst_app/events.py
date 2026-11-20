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

import json
import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from catalyst_app.public_text import validate_safe_public_text

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_PUBLIC_PAYLOAD_BYTES = 64 * 1024  # 64 KiB compact public payload limit
MAX_ANSWER_DELTA_CHARACTERS = 2048


class RunEventType(str, Enum):
    RUN_ACCEPTED = "run.accepted"
    STAGE_STARTED = "stage.started"
    EVIDENCE_RETRIEVED = "evidence.retrieved"
    EVIDENCE_ASSESSED = "evidence.assessed"
    FOLLOWUP_STARTED = "followup.started"
    ANSWER_STARTED = "answer.started"
    ANSWER_DELTA = "answer.delta"
    ANSWER_COMPLETED = "answer.completed"
    ASSURANCE_COMPLETED = "assurance.completed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    FACTS_READY = "facts.ready"
    RETRIEVAL_DONE = "retrieval.done"
    ANALYST_DONE = "analyst.done"
    ANALYST_DECISION = "analyst.decision"
    CHECKS_DONE = "checks.done"
    GATES_DONE = "gates.done"
    VERIFIER_DONE = "verifier.done"
    ANSWER_READY = "answer.ready"
    TIER_DECIDED = "tier.decided"
    INVESTIGATOR_DONE = "investigator.done"


def _bound_progress_value(
    value: Any,
    *,
    truncated: list[bool],
    preserve_strings: bool = False,
    preserve_answer_snapshot: bool = True,
    string_limit: int = 300,
    list_limit: int = 40,
) -> Any:
    """Keep additive progress payloads small and deterministic."""
    if isinstance(value, str) and len(value) > string_limit and not preserve_strings:
        truncated[0] = True
        return value[:string_limit]
    if isinstance(value, (list, tuple)):
        if len(value) > list_limit:
            truncated[0] = True
        return [
            _bound_progress_value(
                item,
                truncated=truncated,
                preserve_strings=preserve_strings,
                preserve_answer_snapshot=preserve_answer_snapshot,
                string_limit=string_limit,
                list_limit=list_limit,
            )
            for item in value[:list_limit]
        ]
    if isinstance(value, dict):
        return {
            key: _bound_progress_value(
                item,
                truncated=truncated,
                preserve_strings=preserve_strings or (
                    preserve_answer_snapshot and key == "answer_snapshot"
                ),
                preserve_answer_snapshot=preserve_answer_snapshot,
                string_limit=string_limit,
                list_limit=list_limit,
            )
            for key, item in value.items()
        }
    return value


class _ProgressPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    truncated: bool = False

    @model_validator(mode="before")
    @classmethod
    def _bound(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        truncated = [bool(value.get("truncated", False))]
        bounded = _bound_progress_value(value, truncated=truncated)
        if len(json.dumps(bounded, ensure_ascii=False, default=str).encode("utf-8")) > MAX_PUBLIC_PAYLOAD_BYTES:
            truncated[0] = True
            bounded = _bound_progress_value(
                value,
                truncated=truncated,
                preserve_answer_snapshot=False,
                string_limit=100,
                list_limit=20,
            )
        bounded["truncated"] = truncated[0]
        return bounded


class FactsReadyWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    information_window_start_at: str
    cutoff_at: str
    session_open_at: str
    evidence_window_start_at: str | None = None
    evidence_window_end_at: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_evidence_window(self, handler):
        data = handler(self)
        if self.evidence_window_start_at is None:
            data.pop("evidence_window_start_at", None)
            data.pop("evidence_window_end_at", None)
        return data


class FactsReadyPayload(_ProgressPayload):
    window: FactsReadyWindow
    facts: dict[str, Any]
    headline: str


class TierDecidedPayload(_ProgressPayload):
    tier: Literal["T0", "T1", "T2", "T3"]
    governing_ref: dict[str, Any] | None = None
    overrides: tuple[str, ...] = ()
    reason_code: str
    reason_text: str
    threshold_used: float | None = None


class RetrievalDoneItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    title: str | None = None
    publisher: str
    published_at: str | None = None
    url: str | None = None
    phase: Literal["PRE_OPEN", "SESSION"]
    content_state: str


class RetrievalDonePayload(_ProgressPayload):
    inventory_count: int = Field(ge=0)
    included_in_pack: int = Field(ge=0)
    lead_only: int = Field(ge=0)
    items: tuple[RetrievalDoneItem, ...] = ()
    duration_ms: int = Field(ge=0)


class AnalystEvidenceDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    disposition: str
    reason_code: str
    note: str | None = Field(default=None, max_length=300)


class AnalystHypothesis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    cause_type: str
    proposed_role: str
    # Live progress (add-only, 2026-11-18): the candidate's first verbatim quote and its source, shown
    # while the cause is still being checked. Not in the public projection.
    quote: str | None = Field(default=None, max_length=600)
    evidence_id: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_progress_fields(self, handler):  # type: ignore[no-untyped-def]
        data = handler(self)
        return {key: value for key, value in data.items()
                if key not in ("quote", "evidence_id") or value is not None}


class AnalystDonePayload(_ProgressPayload):
    duration_ms: int = Field(ge=0)
    evidence_decisions: tuple[AnalystEvidenceDecision, ...] = ()
    hypotheses: tuple[AnalystHypothesis, ...] = ()
    research_decision: str


class AnalystDecisionPayload(_ProgressPayload):
    """One parsed Analyst evidence decision or candidate cause."""

    kind: Literal["evidence", "candidate_cause"]
    evidence_id: str | None = None
    disposition: str | None = None
    reason_code: str | None = None
    note: str | None = Field(default=None, max_length=300)
    ref: str | None = None
    cause_type: str | None = None
    proposed_role: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unselected_decision_fields(self, handler):  # type: ignore[no-untyped-def]
        return {key: value for key, value in handler(self).items() if value is not None}

    @model_validator(mode="after")
    def _matching_decision_shape(self) -> "AnalystDecisionPayload":
        evidence_fields = (self.evidence_id, self.disposition, self.reason_code)
        cause_fields = (self.ref, self.cause_type, self.proposed_role)
        if self.kind == "evidence":
            if any(value is None for value in evidence_fields) or any(
                value is not None for value in cause_fields
            ):
                raise ValueError("evidence analyst.decision requires evidence fields only")
        elif any(value is None for value in cause_fields) or any(
            value is not None for value in evidence_fields
        ) or self.note is not None:
            raise ValueError("candidate cause analyst.decision requires cause fields only")
        return self


class CheckedQuote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_ref: str
    evidence_id: str
    rule: str
    kept: bool


class CheckGate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: str
    result: str
    detail: str | None = Field(default=None, max_length=300)


class ChecksDonePayload(_ProgressPayload):
    quotes: tuple[CheckedQuote, ...] = ()
    gates: tuple[CheckGate, ...] = ()
    duration_ms: int = Field(ge=0)


class GatesDonePayload(_ProgressPayload):
    gates: tuple[CheckGate, ...] = ()
    duration_ms: int = Field(ge=0)


class VerifierDonePayload(_ProgressPayload):
    called: bool
    verdict: str | None = None
    category: str | None = None
    reason: str | None = Field(default=None, max_length=300)
    skipped_because: str | None = None


class InvestigatorDonePayload(_ProgressPayload):
    mode: str
    stop_reason: str
    steps: list[dict[str, Any]]
    evidence_added: int


class AnswerReadyPayload(_ProgressPayload):
    answer_snapshot: dict[str, Any]
    provisional: bool = True


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


class EvidenceAssessedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted_evidence_ids: tuple[str, ...] = ()
    lead_only_evidence_ids: tuple[str, ...] = ()
    rejected_evidence_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    gap_ids: tuple[str, ...] = ()
    status_ceiling: str
    decision: str
    normalization_diagnostics: tuple[str, ...] = ()


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
    # Frozen-A5 (WP-C C3c): ``writer_assembly`` marks a code-owned structural
    # segment rendered from the ValidatedClaimPlan. ``provider`` (provider
    # stream text) is never serialized, so historical events are unchanged.
    origin: Literal["provider", "writer_assembly"] = "provider"

    @model_serializer(mode="wrap")
    def _omit_provider_origin(self, handler):  # type: ignore[no-untyped-def]
        data = handler(self)
        if self.origin == "provider" and isinstance(data, dict):
            data.pop("origin", None)
        return data

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
    EvidenceAssessedPayload,
    FollowUpStartedPayload,
    AnswerStartedPayload,
    AnswerDeltaPayload,
    AnswerCompletedPayload,
    AssuranceCompletedPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunCancelledPayload,
    FactsReadyPayload,
    RetrievalDonePayload,
    AnalystDonePayload,
    AnalystDecisionPayload,
    ChecksDonePayload,
    GatesDonePayload,
    VerifierDonePayload,
    AnswerReadyPayload,
    TierDecidedPayload,
    InvestigatorDonePayload,
]

EVENT_PAYLOAD_TYPES: dict[RunEventType, type[BaseModel]] = {
    RunEventType.RUN_ACCEPTED: RunAcceptedPayload,
    RunEventType.STAGE_STARTED: StageStartedPayload,
    RunEventType.EVIDENCE_RETRIEVED: EvidenceRetrievedPayload,
    RunEventType.EVIDENCE_ASSESSED: EvidenceAssessedPayload,
    RunEventType.FOLLOWUP_STARTED: FollowUpStartedPayload,
    RunEventType.ANSWER_STARTED: AnswerStartedPayload,
    RunEventType.ANSWER_DELTA: AnswerDeltaPayload,
    RunEventType.ANSWER_COMPLETED: AnswerCompletedPayload,
    RunEventType.ASSURANCE_COMPLETED: AssuranceCompletedPayload,
    RunEventType.RUN_COMPLETED: RunCompletedPayload,
    RunEventType.RUN_FAILED: RunFailedPayload,
    RunEventType.RUN_CANCELLED: RunCancelledPayload,
    RunEventType.FACTS_READY: FactsReadyPayload,
    RunEventType.RETRIEVAL_DONE: RetrievalDonePayload,
    RunEventType.ANALYST_DONE: AnalystDonePayload,
    RunEventType.ANALYST_DECISION: AnalystDecisionPayload,
    RunEventType.CHECKS_DONE: ChecksDonePayload,
    RunEventType.GATES_DONE: GatesDonePayload,
    RunEventType.VERIFIER_DONE: VerifierDonePayload,
    RunEventType.ANSWER_READY: AnswerReadyPayload,
    RunEventType.TIER_DECIDED: TierDecidedPayload,
    RunEventType.INVESTIGATOR_DONE: InvestigatorDonePayload,
}
# Internal compatibility alias; the public name is consumed by contract tooling.
_EVENT_PAYLOAD_TYPES = EVENT_PAYLOAD_TYPES


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

    @model_validator(mode="before")
    @classmethod
    def _payload_from_event_type(cls, data: Any) -> Any:
        # Payload models overlap (a gates.done payload is also a valid
        # checks.done payload), so parsing JSON through the union alone picks
        # the wrong type. Parse the payload with the event type's own model.
        if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
            return data
        try:
            expected = EVENT_PAYLOAD_TYPES[RunEventType(data.get("event_type"))]
        except (ValueError, KeyError):
            return data
        try:
            payload = expected.model_validate(data["payload"])
        except ValueError as exc:
            raise ValueError(
                f"payload does not match event type {data.get('event_type')}"
            ) from exc
        return {**data, "payload": payload}

    @model_validator(mode="after")
    def _payload_matches_event_type_and_size(self) -> "PublicRunEvent":
        expected = EVENT_PAYLOAD_TYPES[self.event_type]
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
    """Serialize the public envelope using the normative public names.

    The payload is serialized through its concrete type, not the union. This
    is required for projected payloads whose non-public optional fields were
    removed before publication.
    """
    data = {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "run_id": event.run_id,
        "sequence": event.sequence,
        "event_type": event.event_type.value,
        "emitted_at": event.emitted_at.isoformat().replace("+00:00", "Z"),
        "stage": event.stage,
        "payload": event.payload.model_dump(mode="json"),
        "artifact_refs": [
            ref.model_dump(mode="json") for ref in event.artifact_refs
        ],
    }
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def sse_frame(event: PublicRunEvent) -> str:
    """Frame one public event per the SSE contract."""
    return (
        f"id: {event.run_id}:{event.sequence}\n"
        f"event: {event.event_type.value}\n"
        f"data: {serialize_public_event(event)}\n\n"
    )


HEARTBEAT_COMMENT = ": heartbeat\n\n"


def run_has_answer_ready(db_path: Any, run_id: str) -> bool:
    """RG-13: whether the run's event log contains an ``answer.ready`` event.

    Used to set ``provisional_output_invalidated`` uniformly on terminal
    FAILED/CANCELLED payloads: it is true iff the answer was already pushed.
    """
    from catalyst_app.persistence.connect import open_readonly

    try:
        with open_readonly(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM run_events WHERE run_id = ?"
                " AND event_type = 'answer.ready' LIMIT 1",
                (run_id,),
            ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        # Fail toward invalidation: if the event log cannot be read, a client withdrawing a possibly-pushed answer is
        # safer than one keeping a stale answer after a failed or cancelled run.
        return True


__all__ = [
    "AnswerCompletedPayload",
    "AnswerDeltaPayload",
    "AnswerStartedPayload",
    "AnalystDecisionPayload",
    "ArtifactRef",
    "AssuranceCompletedPayload",
    "EvidenceAssessedPayload",
    "EvidenceRetrievedPayload",
    "EVENT_PAYLOAD_TYPES",
    "FollowUpStartedPayload",
    "GatesDonePayload",
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
    "run_has_answer_ready",
    "StageStartedPayload",
    "serialize_public_event",
    "sse_frame",
]
