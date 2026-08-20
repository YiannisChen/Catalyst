"""V1.1 public API DTO boundary + idempotency request hash (M2-9).

packages/app is the sole live public DTO/projection boundary (Final Migration
TSD §20/§21). DTOs never expose LangGraph state, raw internal semantic fields,
provider objects, credentials, or internal node names. compute_request_hash is
SHA-256 over canonical JSON of the documented semantic inputs only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
)
from catalyst_data.canonical.model import ContentState, SourceClass
from catalyst_app.events import PublicRunEvent
from catalyst_app.lifecycle import RunLifecycleStatus


class RunAcceptedResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: Literal["ACCEPTED"] = "ACCEPTED"
    stream_url: str
    request_hash_prefix: str
    model_capability_label: str


class RunFailureDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str | None = None


class RunDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    lifecycle_status: RunLifecycleStatus
    attribution_status: AttributionStatus | None = None
    attribution_type: AttributionType | None = None
    created_at: datetime
    duration_ms: int | None = None
    failure: RunFailureDTO | None = None
    manifest_summary: dict[str, str] = {}
    terminal_artifact_refs: tuple[str, ...] = ()


class CancelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: RunLifecycleStatus
    acknowledged: bool


PublicRunEventDTO = PublicRunEvent


class ArtifactRefDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    artifact_type: str


class ArtifactDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    artifact_type: str
    stage: str | None = None
    created_at: datetime | None = None
    ref: ArtifactRefDTO | None = None


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


class ClaimDetailDTO(BaseModel):
    """Validated claim and bound support/counter evidence IDs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    role: Literal["PRIMARY", "SECONDARY", "CONTEXT", "LIMITATION"]
    statement: str
    mechanism: str | None = None
    support_evidence_ids: tuple[str, ...] = ()
    counter_evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


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
    artifact_refs: tuple[str, ...] = ()


class CapabilityModelDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model_id: str
    streaming: bool
    structured_output: bool
    cancellation: bool
    token_accounting: bool
    readiness: bool


class CapabilityResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    models: tuple[CapabilityModelDTO, ...] = ()


class HealthDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ready", "degraded", "failed"]
    components: dict[str, Literal["ready", "degraded", "failed"]] = {}


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
