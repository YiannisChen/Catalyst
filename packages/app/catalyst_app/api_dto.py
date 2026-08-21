"""V1.1 public API DTO boundary + idempotency request hash (M2-9, corrective).

packages/app is the sole live public DTO/projection boundary (Final Migration
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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
)
from catalyst_agents.attribution.claims import ClaimRole
from catalyst_data.canonical.model import ContentState, SourceClass
from catalyst_app.events import PublicRunEvent
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.public_text import validate_safe_public_text

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class RunAcceptedResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: Literal["ACCEPTED"] = "ACCEPTED"
    stream_url: str
    request_hash_prefix: str
    model_capability_label: str


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
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    artifact_type: str
    stage: str | None = None
    created_at: datetime | None = None
    ref: ArtifactRefDTO

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
    runtime_db: Literal["ready", "degraded", "failed"]
    event_store: Literal["ready", "degraded", "failed"]


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
    }
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
    "PublicRunEventDTO",
    "RunAcceptedResponse",
    "RunDTO",
    "RunFailureDTO",
    "WorkbenchProjectionDTO",
    "compute_request_hash",
]
