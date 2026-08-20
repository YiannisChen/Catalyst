"""V1.1 EvidenceAnalystContextPack contract (M2-5, corrective).

Deterministic model-facing pack (Phase 3 TSD §20; Frozen §6.3.1). The pack is
a derived view over one canonical EvidenceState inventory; role arrays are
views over that inventory and one evidence ID cannot appear in multiple role
arrays. Round-one packs carry no prior assessment context. Packing/rendering
logic lands in M4.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import ContentState, SourceClass
from catalyst_data.canonical.temporal import TemporalIdentity

from catalyst_agents.attribution.coverage import (
    CapabilityGap,
    CoverageSummary,
    DataCoverageGap,
)
from catalyst_agents.attribution.evidence_state import (
    IndependenceStatus,
    MaterialCapability,
    RetrievalDegradation,
)
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.retrieval.task import ResearchTask

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ContextBudget(BaseModel):
    """Token budget contract (Phase 3 §14)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_context_limit: int
    reserved_output_tokens: int
    reserved_system_instruction_tokens: int
    observation_tokens: int
    coverage_summary_tokens: int
    research_history_tokens: int
    inventory_tokens: int
    evidence_payload_tokens: int
    per_news_item_max_tokens: int
    per_sec_chunk_max_tokens: int
    lead_only_tokens: int
    safety_margin_tokens: int

    @model_validator(mode="after")
    def _non_negative(self) -> "ContextBudget":
        for field, value in self.model_dump().items():
            if value < 0:
                raise ValueError(f"{field} must be non-negative")
        return self


class TokenCountReport(BaseModel):
    """Rendered-input token report (Phase 3 §14 invariant fields)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tokenizer_identity: str
    rendered_messages_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    remaining_payload_tokens: int

    @model_validator(mode="after")
    def _non_negative(self) -> "TokenCountReport":
        for field, value in self.model_dump().items():
            if isinstance(value, int) and value < 0:
                raise ValueError(f"{field} must be non-negative")
        return self


class EvidencePayloadItem(BaseModel):
    """Inventory identity fields plus bounded exact excerpt text and offsets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    canonical_asset_id: str
    canonical_content_version_id: str
    corpus_document_id: str
    chunk_id: str | None = None
    section_key: str | None = None
    chunk_ordinal: int | None = None
    source_class: SourceClass
    evidence_role: str
    eligible_at: datetime
    content_state: ContentState
    material_capability: MaterialCapability
    independence_group_id: str | None = None
    independence_status: IndependenceStatus
    content_hash: str | None = None
    excerpt_text: str | None = None
    source_start_offset: int | None = None
    source_end_offset: int | None = None
    tokenizer_identity: str | None = None

    @model_validator(mode="after")
    def _offsets(self) -> "EvidencePayloadItem":
        if self.chunk_ordinal is not None and self.chunk_ordinal < 0:
            raise ValueError("chunk_ordinal must be non-negative")
        if self.source_start_offset is not None and self.source_start_offset < 0:
            raise ValueError("source_start_offset must be non-negative")
        if (
            self.source_end_offset is not None
            and self.source_start_offset is not None
            and self.source_end_offset < self.source_start_offset
        ):
            raise ValueError("source_end_offset must not precede source_start_offset")
        return self


class TruncationRecord(BaseModel):
    """Bounded truncation record (Phase 3 §17)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    action: Literal[
        "INCLUDED_FULL",
        "INCLUDED_TRUNCATED",
        "METADATA_ONLY",
        "EXCLUDED_DUPLICATE",
        "EXCLUDED_BUDGET",
        "EXCLUDED_INELIGIBLE",
        "EXCLUDED_MATERIALITY",
    ]
    original_token_count: int | None = None
    included_token_count: int
    source_start_offset: int | None = None
    source_end_offset: int | None = None
    tokenizer_identity: str
    reason_code: str


class PriorAssessmentContext(BaseModel):
    """Round-two-only bounded references to the prior normalized assessment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prior_counter_evidence_ids: tuple[str, ...] = ()
    prior_semantic_conflicts: tuple[str, ...] = ()
    previously_supported_hypothesis_ids: tuple[str, ...] = ()
    unresolved_gap_ids: tuple[str, ...] = ()
    prior_evidence_assessment_ref: str | None = None


class EvidenceAnalystContextPack(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    packing_policy_version: str
    run_id: str
    round: int
    temporal_identity: TemporalIdentity
    data_runtime_identity: DataRuntimeIdentity
    context_budget: ContextBudget
    token_count_report: TokenCountReport
    hard_constraints: tuple[str, ...] = ()
    observation: MoveProfile
    coverage_summary: CoverageSummary
    research_history: tuple[ResearchTask, ...] = ()
    evidence_inventory: tuple[EvidencePayloadItem, ...] = ()
    direct_primary_evidence: tuple[EvidencePayloadItem, ...] = ()
    primary_authority_evidence: tuple[EvidencePayloadItem, ...] = ()
    independent_reports: tuple[EvidencePayloadItem, ...] = ()
    lead_only_evidence: tuple[EvidencePayloadItem, ...] = ()
    structured_context: tuple[EvidencePayloadItem, ...] = ()
    deterministic_conflict_signals: tuple[str, ...] = ()
    data_coverage_gaps: tuple[DataCoverageGap, ...] = ()
    capability_gaps: tuple[CapabilityGap, ...] = ()
    retrieval_degradations: tuple[RetrievalDegradation, ...] = ()
    included_evidence_ids: tuple[str, ...] = ()
    excluded_evidence_ids: tuple[str, ...] = ()
    truncation_metadata: tuple[TruncationRecord, ...] = ()
    delta_evidence_ids: tuple[str, ...] = ()
    prior_assessment_context: PriorAssessmentContext | None = None
    context_pack_sha256: str
    prompt_template_version: str
    prompt_template_sha256: str
    rendered_messages_sha256: str

    @field_validator(
        "context_pack_sha256",
        "prompt_template_sha256",
        "rendered_messages_sha256",
    )
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def _round_and_role_views(self) -> "EvidenceAnalystContextPack":
        if self.round < 1:
            raise ValueError("round must be positive")
        if self.round == 1 and self.prior_assessment_context is not None:
            raise ValueError("round-one pack must not carry prior assessment context")
        inventory_ids = {item.evidence_id for item in self.evidence_inventory}
        role_arrays = (
            self.direct_primary_evidence,
            self.primary_authority_evidence,
            self.independent_reports,
            self.lead_only_evidence,
        )
        seen: set[str] = set()
        for array in role_arrays:
            for item in array:
                if item.evidence_id not in inventory_ids:
                    raise ValueError(
                        "role arrays must be views over the evidence inventory"
                    )
                if item.evidence_id in seen:
                    raise ValueError(
                        "one evidence ID cannot appear in multiple role arrays"
                    )
                seen.add(item.evidence_id)
        for item in self.structured_context:
            if item.evidence_id not in inventory_ids:
                raise ValueError(
                    "structured_context must be a view over the evidence inventory"
                )
        return self


__all__ = [
    "ContextBudget",
    "EvidenceAnalystContextPack",
    "EvidencePayloadItem",
    "PriorAssessmentContext",
    "TokenCountReport",
    "TruncationRecord",
]
