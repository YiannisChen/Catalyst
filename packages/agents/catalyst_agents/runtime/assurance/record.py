from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


STRUCTURAL_CHECK_ORDER = (
    "stream_complete",
    "citation_resolution",
    "claim_markers_subset",
    "required_sections",
    "required_limitations",
    "status_type_alignment",
    "hash_coherence",
    "metadata_consistency",
)

CHECK_ORDER = (
    "cutoff",
    "citation_resolution",
    "judge_visibility",
    "prerequisite_gates",
    "legal_path",
    "trace_completeness",
    "identities",
    "budget_retry_repair",
    "degraded_state",
    "structured_context_support",
)


_CHECK_NAMES = (
    "cutoff",
    "citation_resolution",
    "judge_visibility",
    "prerequisite_gates",
    "legal_path",
    "trace_completeness",
    "identities",
    "budget_retry_repair",
    "degraded_state",
    "structured_context_support",
    "stream_complete",
    "claim_markers_subset",
    "required_sections",
    "required_limitations",
    "status_type_alignment",
    "hash_coherence",
    "metadata_consistency",
)


class AssuranceCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check_name: Literal[
        "cutoff",
        "citation_resolution",
        "judge_visibility",
        "prerequisite_gates",
        "legal_path",
        "trace_completeness",
        "identities",
        "budget_retry_repair",
        "degraded_state",
        "structured_context_support",
        "stream_complete",
        "claim_markers_subset",
        "required_sections",
        "required_limitations",
        "status_type_alignment",
        "hash_coherence",
        "metadata_consistency",
    ]
    status: Literal["pass", "fail", "not_applicable"]
    detail: str
    checked_at: str


class RunAssuranceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    trace_id: str
    output_status: Literal["SUFFICIENT", "PARTIAL", "ABSTAIN", "SYSTEM_ERROR"]
    cutoff: str
    corpus_manifest_id: str | None
    index_manifest_id: str | None
    model_ids: list[str]
    prompt_versions: list[str]
    checks: list[AssuranceCheck]
    source_support_flags: dict[str, bool]
    retry_count: int = Field(ge=0)
    repair_count: int = Field(ge=0)
    budget_exhausted: bool
    is_degraded: bool
    created_at: str

    @field_validator("checks")
    @classmethod
    def _check_order(cls, value: list[AssuranceCheck]) -> list[AssuranceCheck]:
        if [check.check_name for check in value] != list(CHECK_ORDER):
            raise ValueError("assurance checks are not in canonical order")
        return value

    @field_validator("model_ids", "prompt_versions")
    @classmethod
    def _sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("identity lists must be sorted unique")
        return value


class StructuralAssuranceRecord(BaseModel):
    """M5-8 structural assurance record (Final TSD §13; Phase 4 §31)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1_1_structural_assurance_v1"] = "v1_1_structural_assurance_v1"
    run_id: str
    answer_text_sha256: str
    validated_status: str
    validated_attribution_type: str
    checks: list[AssuranceCheck]
    provisional_label: str = "PROVISIONAL_RENDERING"
    created_at: str

    @field_validator("checks")
    @classmethod
    def _structural_order(cls, value: list[AssuranceCheck]) -> list[AssuranceCheck]:
        if [check.check_name for check in value] != list(STRUCTURAL_CHECK_ORDER):
            raise ValueError("structural assurance checks are not in canonical order")
        return value
