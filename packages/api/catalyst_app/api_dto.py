"""V1.1 public API DTO boundary + idempotency request hash (M2-9, corrective).

packages/api is the sole live public DTO/projection boundary (Final Migration
TSD §20/§21; Phase 5 TSD §21/§23). DTOs never expose LangGraph state, raw
internal semantic fields, provider objects, credentials, or internal node
names. Only COMPLETED runs may carry attribution status/type. Artifact refs
retain version/hash/same-run metadata; evidence detail preserves chunk/fact
identity; capability uses the exact Phase 5 §23.2 fields; health types
process/executor/runtime-DB/event-store readiness. compute_request_hash is
SHA-256 over canonical JSON of the documented semantic inputs only.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal

from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
)
from catalyst_agents.attribution.claims import ClaimRole
from catalyst_agents.observability.decision_trace import AbstentionReasonCode
from catalyst_data.canonical.model import ContentState, SourceClass
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from catalyst_app.events import PublicRunEvent
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.public_text import validate_safe_public_text

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

ProductTier = Literal[
    "CONFIRMED", "REPORTED", "MOVED_WITH_GROUP", "LARGER_THAN_GROUP", "NOTHING_FOUND", "ORDINARY_MOVE",
]


class RunAcceptedResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: Literal["ACCEPTED"] = "ACCEPTED"
    stream_url: str
    request_hash_prefix: str
    model_capability_label: str
    access: dict[str, Any] | None = None


class RunFailureDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    code: str
    message: str | None = None

    @field_validator("message")
    @classmethod
    def _safe_public_message(cls, value: str | None) -> str | None:
        return validate_safe_public_text(value)


class RunDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    lifecycle_status: RunLifecycleStatus
    attribution_status: AttributionStatus | None = None
    attribution_type: AttributionType | None = None
    created_at: datetime
    duration_ms: int | None = Field(default=None, ge=0)
    failure: RunFailureDTO | None = None
    manifest_summary: dict[str, str] = {}
    terminal_artifact_refs: tuple[ArtifactRefDTO, ...] = ()

    @model_validator(mode="after")
    def _attribution_only_on_completed(self) -> "RunDTO":
        if self.lifecycle_status is not RunLifecycleStatus.COMPLETED:
            if self.attribution_status is not None or self.attribution_type is not None:
                raise ValueError(
                    "non-COMPLETED runs cannot carry attribution status/type"
                )
        if self.lifecycle_status is RunLifecycleStatus.COMPLETED and self.failure is not None:
            raise ValueError("COMPLETED runs cannot carry failure details")
        if self.failure is not None and self.lifecycle_status is not RunLifecycleStatus.FAILED:
            raise ValueError("failure details are permitted only for FAILED runs")
        if any(ref.run_id != self.run_id for ref in self.terminal_artifact_refs):
            raise ValueError("terminal artifact refs must belong to the same run")
        return self


class PublicCacheRunDTO(RunDTO):
    """A cached public result response with its explicit serving source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    served_from: Literal["public_cache"] = "public_cache"


class RunViewModelDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str | None = None
    analyst_model_id: str | None = None


class RunViewRevisionDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str | None = None
    dirty: bool | None = None


class RunViewRunDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    ticker: str | None = None
    session_date: str | None = None
    lifecycle: Literal[
        "ACCEPTED", "RUNNING", "CANCEL_REQUESTED", "COMPLETED", "FAILED", "CANCELLED"
    ]
    created_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    duration_source: Literal["decision_trace", "terminal_event", "not_recorded"]
    model: RunViewModelDTO
    policy: dict[str, Any] | None = None
    code_revision: RunViewRevisionDTO
    force_full: bool | None = None


class RunViewCostDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_calls: dict[str, int] = Field(default_factory=dict)
    provider_attempts: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    cost_method: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    used_tokens: int | None = Field(default=None, ge=0)
    token_method: str | None = None


class RunViewFailureDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    code: str
    message: str | None = None

    @field_validator("message")
    @classmethod
    def _safe_truncated_message(cls, value: str | None) -> str | None:
        return validate_safe_public_text(value[:300] if value else value)


class _OmitAbsentModel(BaseModel):
    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        validation_schema = dict(core_schema)
        validation_schema.pop("serialization", None)
        schema = handler(validation_schema)
        schema["additionalProperties"] = False
        return schema


class RunViewPublicSourceDTO(_OmitAbsentModel):
    """One public source entry; internal document and claim fields stay private."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    text: str | None = None
    publisher: str | None = None
    url: str | None = None
    evidence_id: str | None = None
    role: str | None = None
    published_at: str | None = None
    published_precision: str | None = None
    release_session: Literal["pre", "intraday", "post"] | None = None
    release_time_et: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        return {field: value for field, value in payload.items() if field in self.model_fields_set}


class RunViewPublicAttributionDTO(_OmitAbsentModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    quotes: list[RunViewPublicSourceDTO] = Field(default_factory=list)
    event_type: str | None = None
    scope: str | None = None
    event_date: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        return {field: value for field, value in payload.items() if field in self.model_fields_set}


class RunViewPublicPossibleExplanationDTO(_OmitAbsentModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    quotes: list[RunViewPublicSourceDTO] = Field(default_factory=list)
    verdict_category: str | None = None
    category_text: str | None = None
    label: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        return {field: value for field, value in payload.items() if field in self.model_fields_set}


class RunViewPublicLeadDTO(_OmitAbsentModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str | None = None
    publisher: str | None = None
    published_at: str | None = None
    url: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        return {field: value for field, value in payload.items() if field in self.model_fields_set}


class RunViewPublicAbstentionDTO(_OmitAbsentModel):
    """Code-derived abstention reason projection with an explicit public shape."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    reason_code: AbstentionReasonCode | None = None
    reason_text: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    gate_reason_code: str | None = None
    failed_stage: int | None = None
    possible_explanation: RunViewPublicPossibleExplanationDTO | list[RunViewPublicSourceDTO] | None = None
    leads: list[RunViewPublicLeadDTO] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        return {field: value for field, value in payload.items() if field in self.model_fields_set}


class RunViewPublicAnswerDTO(_OmitAbsentModel):
    """Public answer snapshot with an explicit allowlist and outcome enums."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: str | None = None
    run_id: str | None = None
    ticker: str | None = None
    session_date: str | None = None
    headline: str | None = None
    outcome: Literal["ANSWERED", "ABSTAINED", "TIER_ONLY", "WEB_SOURCE_SUPPORTED"] | None = None
    status: str | None = None
    scope_note: str | None = None
    tier: str | None = None
    tier_reason: str | None = None
    cascade_stage: Literal[1, 2, 4] | None = None
    stage_label: str | None = None
    basis: Literal[
        "REPORTED_ATTRIBUTION", "GROUP_DRIVER", "CONFIRMED_EVENT_INFERRED_LINK", "EARNINGS_FILING",
    ] | None = None
    basis_text: str | None = None
    basis_note: str | None = None
    primary: RunViewPublicAttributionDTO | None = None
    secondary: RunViewPublicAttributionDTO | None = None
    facts: dict[str, Any] | None = None
    window: dict[str, Any] | None = None
    retrospective: dict[str, Any] | None = None
    data_gaps: list[dict[str, Any]] | None = None
    update_notice: dict[str, Any] | None = None
    limitations: list[str] | None = None
    key_sentence: RunViewPublicSourceDTO | None = None
    also_sentence: RunViewPublicSourceDTO | None = None
    event_sentence: RunViewPublicSourceDTO | None = None
    sources: list[RunViewPublicSourceDTO] | None = None
    desk_context: dict[str, Any] | None = None
    investigation: dict[str, Any] | None = None
    earnings_fallback: dict[str, Any] | None = None
    grade: Literal[
        "ORDINARY", "MOVED_WITH_GROUP", "CONFIRMED", "REPORTED", "NO_CLEAR_CAUSE", "SUPPORTED",
    ] | None = None
    grade_label: str | None = None
    reported_by: list[dict[str, Any]] | None = None
    abstention: RunViewPublicAbstentionDTO | None = None
    governing_ref: dict[str, Any] | None = None
    last_attempted_stage: int | None = None
    published_stage: int | None = None
    evidence_origin: str | None = None
    label: str | None = None
    web_evidence: dict[str, Any] | None = None
    # Add-only (2026-11-16): what the product tells the user; see catalyst_app.product_tier.
    product_tier: ProductTier | None = None
    group_context: dict[str, Any] | None = None
    # Add-only (2026-11-17): a source's stated reason on an abstained answer; see attribution.reported_reason.
    reported_reason: dict[str, Any] | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, dict):
            return self.model_dump(mode="python") == other
        return super().__eq__(other)

    @model_serializer(mode="wrap")
    def _omit_absent_abstention(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        provided = set(self.model_fields_set)
        payload = {field: value for field, value in payload.items() if field in provided}
        if self.abstention is None:
            payload.pop("abstention", None)
        for field in ("grade", "grade_label", "key_sentence", "also_sentence", "event_sentence", "sources",
                      "desk_context", "basis", "cascade_stage", "reported_reason"):
            if getattr(self, field) is None:
                payload.pop(field, None)
        return payload


class RunViewAbstentionDTO(BaseModel):
    """Additive, code-derived abstention reason projection."""

    model_config = ConfigDict(frozen=True, extra="allow")

    reason_code: AbstentionReasonCode | None = None
    reason_text: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RunViewAnswerWindowDTO(BaseModel):
    """Temporal window attached to an answer snapshot."""

    model_config = ConfigDict(frozen=True, extra="allow")

    information_window_start_at: str | None = None
    cutoff_at: str | None = None
    session_open_at: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_window_fields(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        return {field: value for field, value in payload.items() if field in self.model_fields_set}


class RunViewAnswerDTO(BaseModel):
    """Public answer snapshot with explicit outcome and abstention enums."""

    model_config = ConfigDict(frozen=True, extra="allow")

    outcome: Literal["ANSWERED", "ABSTAINED", "TIER_ONLY", "WEB_SOURCE_SUPPORTED"] | None = None
    abstention: RunViewAbstentionDTO | None = None
    grade: Literal[
        "ORDINARY", "MOVED_WITH_GROUP", "CONFIRMED", "REPORTED", "NO_CLEAR_CAUSE", "SUPPORTED",
    ] | None = None
    grade_label: str | None = None
    key_sentence: dict[str, Any] | None = None
    event_sentence: dict[str, Any] | None = None
    sources: list[dict[str, Any]] | None = None
    desk_context: dict[str, Any] | None = None
    basis: Literal[
        "REPORTED_ATTRIBUTION", "GROUP_DRIVER", "CONFIRMED_EVENT_INFERRED_LINK", "EARNINGS_FILING",
    ] | None = None
    cascade_stage: Literal[1, 2, 4] | None = None
    schema_version: str | None = None
    run_id: str | None = None
    ticker: str | None = None
    session_date: str | None = None
    status: str | None = None
    headline: str | None = None
    tier: Literal["T0", "T1", "T2", "T3"] | None = None
    tier_reason: str | None = None
    facts: dict[str, Any] | None = None
    window: RunViewAnswerWindowDTO | None = None
    limitations: list[str] | None = None
    also_sentence: dict[str, Any] | None = None
    governing_ref: dict[str, Any] | None = None
    # Add-only (2026-11-16): what the product tells the user; see catalyst_app.product_tier.
    product_tier: ProductTier | None = None
    group_context: dict[str, Any] | None = None
    # Add-only (2026-11-17): a source's stated reason on an abstained answer; see attribution.reported_reason.
    reported_reason: dict[str, Any] | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, dict):
            return self.model_dump(mode="python") == other
        return super().__eq__(other)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core_schema)
        source_projection_schema = {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "release_session": {
                    "type": "string",
                    "enum": ["pre", "intraday", "post"],
                },
                "release_time_et": {"type": "string"},
            },
        }
        schema.update(
            title="RunViewAnswerDTO",
            description="Public answer snapshot with explicit outcome and abstention enums.",
            type="object",
            additionalProperties=True,
            properties={
                "outcome": {
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": [
                                "ANSWERED",
                                "ABSTAINED",
                                "TIER_ONLY",
                                "WEB_SOURCE_SUPPORTED",
                            ],
                        },
                        {"type": "null"},
                    ],
                },
                "abstention": {
                    "anyOf": [
                        {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "reason_code": {
                                    "anyOf": [
                                        {
                                            "type": "string",
                                            "enum": [
                                                "NO_STOCK_NEWS",
                                                "NO_PRIMARY_PROPOSED",
                                                "PRIMARY_QUOTES_FAILED",
                                                "GATE_FAILED",
                                                "VERIFIER_REJECTED",
                                                "VERIFIER_NO_VERDICT",
                                                "CEILING_ABSTAIN",
                                                "ALREADY_ABSTAIN",
                                            ],
                                        },
                                        {"type": "null"},
                                    ],
                                },
                                "reason_text": {
                                    "anyOf": [
                                        {"type": "string"},
                                        {"type": "null"},
                                    ],
                                },
                                "details": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                            },
                        },
                        {"type": "null"},
                    ],
                },
                "grade": {
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": [
                                "ORDINARY",
                                "MOVED_WITH_GROUP",
                                "CONFIRMED",
                                "REPORTED",
                                "NO_CLEAR_CAUSE",
                                "SUPPORTED",
                            ],
                        },
                        {"type": "null"},
                    ],
                },
                "grade_label": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                },
                "key_sentence": {
                    "anyOf": [
                        source_projection_schema,
                        {"type": "null"},
                    ],
                },
                "event_sentence": {
                    "anyOf": [
                        source_projection_schema,
                        {"type": "null"},
                    ],
                },
                "sources": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                **source_projection_schema,
                            },
                        },
                        {"type": "null"},
                    ],
                },
                "desk_context": {
                    "anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}],
                },
                "basis": {
                    "anyOf": [{"type": "string", "enum": [
                        "REPORTED_ATTRIBUTION", "GROUP_DRIVER", "CONFIRMED_EVENT_INFERRED_LINK",
                        "EARNINGS_FILING",
                    ]}, {"type": "null"}],
                },
                "cascade_stage": {
                    "anyOf": [{"type": "integer", "enum": [1, 2, 4]}, {"type": "null"}],
                },
                "schema_version": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "run_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "ticker": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "session_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "status": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "headline": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "tier": {"anyOf": [
                    {"type": "string", "enum": ["T0", "T1", "T2", "T3"]}, {"type": "null"},
                ]},
                "tier_reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "facts": {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]},
                "window": {"anyOf": [{
                    "type": "object", "additionalProperties": True,
                    "properties": {
                        "information_window_start_at": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "cutoff_at": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "session_open_at": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                }, {"type": "null"}]},
                "limitations": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
                "also_sentence": {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]},
                "governing_ref": {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]},
            },
        )
        return schema

    @model_serializer(mode="wrap")
    def _omit_absent_abstention(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        if self.abstention is None:
            payload.pop("abstention", None)
        for field in ("grade", "grade_label", "key_sentence", "event_sentence", "sources",
                      "desk_context", "basis", "cascade_stage", "reported_reason"):
            if getattr(self, field) is None:
                payload.pop(field, None)
        for field in (
            "schema_version", "run_id", "ticker", "session_date", "status", "headline",
            "tier", "tier_reason", "facts", "window", "limitations", "also_sentence",
            "governing_ref",
        ):
            if field not in self.model_fields_set:
                payload.pop(field, None)
        return payload


RunViewAvailability = dict[
    Literal["answer", "trace", "policy", "cost", "facts", "abstention_reason"],
    Literal["present", "derived", "not_recorded", "not_applicable"],
]


class RunViewDTO(BaseModel):
    """Persisted, read-only projection for one run's result page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["run_view_v1"] = "run_view_v1"
    run: RunViewRunDTO
    cost: RunViewCostDTO
    answer: RunViewAnswerDTO | None = None
    legacy_text: str | None = None
    facts: dict[str, Any] | None = None
    trace: dict[str, Any] | None = None
    failure: RunViewFailureDTO | None = None
    availability: RunViewAvailability

    @model_validator(mode="after")
    def _answer_shape(self) -> "RunViewDTO":
        if self.answer is not None and self.legacy_text is not None:
            raise ValueError("legacy_text is only available when answer snapshot is absent")
        return self


class CancelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: RunLifecycleStatus
    acknowledged: bool


PublicRunEventDTO = PublicRunEvent


class ArtifactRefDTO(BaseModel):
    """Artifact ref with version/hash/same-run metadata (Phase 5 §18.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    artifact_type: str
    schema_version: str
    content_sha256: str
    run_id: str

    @model_validator(mode="after")
    def _hash_shape(self) -> "ArtifactRefDTO":
        if _SHA256_RE.fullmatch(self.content_sha256) is None:
            raise ValueError("content_sha256 must be a lowercase SHA-256 hex digest")
        return self


class ArtifactDTO(BaseModel):
    """Artifact metadata with version/hash/same-run metadata (Phase 5 §18.2).

    ``payload`` is the safe same-run projection body returned only by the
    single-artifact endpoint (Final TSD §20.1); refs pages omit it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    artifact_type: str
    stage: str | None = None
    created_at: datetime | None = None
    ref: ArtifactRefDTO
    payload: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _matches_reference(self) -> "ArtifactDTO":
        if self.artifact_id != self.ref.artifact_id or self.artifact_type != self.ref.artifact_type:
            raise ValueError("ArtifactDTO identity/type must match its nested ref")
        return self


class EvidenceDetailDTO(BaseModel):
    """Safe same-run canonical evidence detail (Final Migration TSD §20.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    canonical_asset_id: str
    content_version_id: str
    chunk_id: str | None = None
    fact_id: str | None = None
    excerpt: str
    source_class: SourceClass
    content_state: ContentState
    eligible_at: datetime
    ticker_scope: tuple[str, ...]
    provider: str
    publisher: str | None = None
    dedup_cluster_id: str | None = None

    @model_validator(mode="after")
    def _identity_invariants(self) -> "EvidenceDetailDTO":
        if (self.chunk_id is None) == (self.fact_id is None):
            raise ValueError("exactly one of chunk_id or fact_id must be set")
        if self.chunk_id is not None and self.evidence_id != self.chunk_id:
            raise ValueError("text evidence_id must equal chunk_id")
        if self.fact_id is not None and self.evidence_id != self.fact_id:
            raise ValueError("structured evidence_id must equal fact_id")
        return self


class ClaimDetailDTO(BaseModel):
    """Validated claim and bound support/counter/citation evidence IDs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    role: ClaimRole
    statement: str
    mechanism: str | None = None
    support_evidence_ids: tuple[str, ...] = ()
    counter_evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    citation_evidence_ids: tuple[str, ...] = ()
    validation_status: str
    validation_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _citation_refs_resolve(self) -> "ClaimDetailDTO":
        allowed = set(self.support_evidence_ids) | set(self.counter_evidence_ids)
        unknown = set(self.citation_evidence_ids) - allowed
        if unknown:
            raise ValueError(
                f"citation evidence must be bound to claim {self.claim_id!r}: "
                f"{sorted(unknown)}"
            )
        return self


class WorkbenchProjectionDTO(BaseModel):
    """Compatibility aggregate over domain artifacts; never raw graph state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    lifecycle_status: RunLifecycleStatus
    attribution_status: AttributionStatus | None = None
    attribution_type: AttributionType | None = None
    claims: tuple[ClaimDetailDTO, ...] = ()
    evidence: tuple[EvidenceDetailDTO, ...] = ()
    answer: str | None = None
    limitations: tuple[str, ...] = ()
    artifact_refs: tuple[ArtifactRefDTO, ...] = ()

    @model_validator(mode="after")
    def _attribution_only_on_completed(self) -> "WorkbenchProjectionDTO":
        if self.lifecycle_status is not RunLifecycleStatus.COMPLETED:
            if self.attribution_status is not None or self.attribution_type is not None:
                raise ValueError(
                    "non-COMPLETED runs cannot carry attribution status/type"
                )
        if any(ref.run_id != self.run_id for ref in self.artifact_refs):
            raise ValueError("artifact refs must belong to the same run")
        return self


class CapabilityModelDTO(BaseModel):
    """Exact Phase 5 TSD §23.2 capability fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model_id: str
    executable: bool
    writer_streaming: bool
    analyst_structured_output: bool
    cancellation: Literal["supported", "unsupported", "unknown"]
    token_usage: Literal["exact", "provider_reported", "estimated", "unavailable"]
    timeout_seconds: int | None = Field(default=None, gt=0)
    offline_only: bool
    readiness: Literal["ready", "degraded", "unavailable"]
    reason_code: str | None = None
    capability_probe_at: datetime | None = None
    revision: str


class CapabilityResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    models: tuple[CapabilityModelDTO, ...] = ()


class HealthDTO(BaseModel):
    """Typed local health: process/executor/runtime DB/event store readiness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ready", "degraded", "failed"]
    process: Literal["ready", "degraded", "failed"]
    executor: Literal["ready", "degraded", "failed"]
    served_db: Literal["ready", "degraded", "failed"]
    runtime_db: Literal["ready", "degraded", "failed"]
    event_store: Literal["ready", "degraded", "failed"]
    store_read_failures: int = Field(default=0, ge=0)
    provider_account_errors: int = Field(default=0, ge=0)
    reason: str | None = None
    press_release_archive: dict[str, Any] = Field(default_factory=dict)
    featured: Literal["ready", "corrupt", "not_configured"] = "not_configured"


def compute_request_hash(
    *,
    ticker: str,
    session_date: str,
    normalized_question: str,
    provider: str,
    model_id: str,
    normalized_base_url: str,
    credential_source_identifier: str,
    workflow_version: str,
    config_version: str,
    force_full: bool = False,
    max_cost_usd: float | None = None,
) -> str:
    """SHA-256 over canonical JSON of the documented semantic request inputs.

    The idempotency key, secrets, credentials, and transport headers are not
    inputs and therefore cannot affect the hash (Final Migration TSD §11).
    """
    payload = {
        "ticker": ticker,
        "session_date": session_date,
        "normalized_question": normalized_question,
        "provider": provider,
        "model_id": model_id,
        "normalized_base_url": normalized_base_url,
        "credential_source_identifier": credential_source_identifier,
        "workflow_version": workflow_version,
        "config_version": config_version,
        "force_full": force_full,
    }
    if max_cost_usd is not None:
        payload["max_cost_usd"] = max_cost_usd
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "ArtifactDTO",
    "ArtifactRefDTO",
    "CancelResponse",
    "CapabilityModelDTO",
    "CapabilityResponse",
    "ClaimDetailDTO",
    "EvidenceDetailDTO",
    "HealthDTO",
    "PublicCacheRunDTO",
    "PublicRunEventDTO",
    "RunAcceptedResponse",
    "RunDTO",
    "RunFailureDTO",
    "WorkbenchProjectionDTO",
    "compute_request_hash",
]
