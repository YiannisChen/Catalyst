"""V1.1 RunManifest contract (M2-7, corrective).

Agents-owned production run identity (Final Migration TSD §14–15; Frozen
§6.7). RunManifest references DataRuntimeIdentity by ref/hash and never
duplicates an independently recomputed data identity. Production corrective
bounds (1 round, 1 action) and the initial 60-second run deadline are
enforced; runtime configuration is typed, not an unrestricted dict.
"""
from __future__ import annotations

import re
from typing import Literal

from catalyst_data.canonical.temporal import TemporalIdentity
from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_serializer,
    model_validator,
)

from catalyst_agents.attribution.context_pack import ContextBudget
from catalyst_agents.attribution.policy import AttributionPolicy

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

MAX_CORRECTIVE_ROUNDS_PRODUCTION = 1
MAX_ACTIONS_PER_BATCH_PRODUCTION = 1
INITIAL_RUN_TIMEOUT_SECONDS = 120
# Runs created before 2026-11-16 used a 60-second deadline; their stored manifests must still load.
LEGACY_INITIAL_RUN_TIMEOUT_SECONDS = 60
INVESTIGATOR_RUN_TIMEOUT_SECONDS = 240
# v3 adds 3-vote classification and a blind skeptic search (design 2026-10-19).
INVESTIGATOR_V3_RUN_TIMEOUT_SECONDS = 420


def investigator_run_timeout(attribution_policy) -> int | None:
    mode = getattr(attribution_policy, "investigator_mode", None)
    if mode is None:
        return None
    return INVESTIGATOR_V3_RUN_TIMEOUT_SECONDS if mode == "agent_v3" else INVESTIGATOR_RUN_TIMEOUT_SECONDS


class ObservationPolicyConfig(BaseModel):
    """Versioned ObservationPolicyConfig (Phase 3 TSD §6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    material_target_return_pct: float
    material_prior_return_pct: float
    quiet_target_return_pct: float
    flat_reference_return_pct: float
    aligned_residual_pct: float
    volume_elevated_ratio: float
    volume_extreme_ratio: float
    minimum_peer_count: int
    require_sector_and_peer_for_broad_sector: bool
    scenario_policy_version: str

    @model_validator(mode="after")
    def _non_negative(self) -> "ObservationPolicyConfig":
        for field, value in self.model_dump().items():
            if isinstance(value, (int, float)) and value < 0:
                raise ValueError(f"{field} must be non-negative")
        return self


class RuntimeConfiguration(BaseModel):
    """Typed run runtime configuration (Phase 3 §6/§14; M4-2 amendment §3).

    Carries policy/budget values only; never an identity-bearing free-form
    dict and never a duplicate DataRuntimeIdentity. The context budget is the
    canonical agents-owned ContextBudget type so pack and manifest budgets
    cannot diverge. Research limits are typed and positive: the stage timeout
    is strictly below the absolute 60-second run deadline and the initial
    concurrency is capped at the maximum initial task count (3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_policy: ObservationPolicyConfig
    context_budget: ContextBudget
    research_stage_timeout_seconds: int = 30
    max_initial_research_concurrency: int = 2

    @model_validator(mode="after")
    def _research_limits_bounded(self) -> "RuntimeConfiguration":
        if self.research_stage_timeout_seconds < 1:
            raise ValueError("research_stage_timeout_seconds must be positive")
        if self.research_stage_timeout_seconds >= INITIAL_RUN_TIMEOUT_SECONDS:
            raise ValueError(
                "research_stage_timeout_seconds must be strictly below the "
                f"absolute {INITIAL_RUN_TIMEOUT_SECONDS}-second run deadline"
            )
        if not 1 <= self.max_initial_research_concurrency <= 3:
            raise ValueError(
                "max_initial_research_concurrency must be between 1 and the "
                "maximum initial task count (3)"
            )
        return self


class RunManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    request_hash: str
    temporal_identity: TemporalIdentity
    # Present only for desk_cascade_v1; the primary identity remains the
    # pre-close stage-one identity.
    stage2_temporal_identity: TemporalIdentity | None = None
    data_runtime_identity_ref: str
    data_runtime_identity_hash: str
    code_revision: str
    workflow_version: str
    policy_version: str
    analyst_model_id: str
    analyst_prompt_hash: str
    writer_model_id: str
    writer_prompt_hash: str
    context_pack_schema_version: str
    packing_policy_version: str
    context_token_budget: int
    tokenizer_policy: str
    hypothesis_schema_version: str
    claim_schema_version: str
    max_corrective_rounds: int = MAX_CORRECTIVE_ROUNDS_PRODUCTION
    max_actions_per_batch: int = MAX_ACTIONS_PER_BATCH_PRODUCTION
    run_timeout_seconds: int = INITIAL_RUN_TIMEOUT_SECONDS
    provider_capability_revision: str
    runtime_configuration: RuntimeConfiguration
    # Optional user-selected web evidence mode. Omitted for normal runs so
    # their persisted manifest bytes and semantic identity remain unchanged.
    parent_run_id: str | None = None
    web_search_lead_id: str | None = None
    web_cache_rows_sha256: str | None = None
    # Present only when the optional local press-release archive is explicitly
    # disabled; an omitted field preserves the strict default.
    press_release_archive: Literal["off"] | None = None
    # Present only for an explicitly waived in-process evaluation run.
    filing_units: Literal["waived"] | None = None

    # Frozen-A3 §2.1 retrieval limits (WP-B B5). Each is optional and, when
    # unset, is *omitted* from ``model_dump`` by the serializer below so a
    # historical manifest keeps its exact bytes and admission hash
    # (``catalyst_app/runtime/admission.py`` binds
    # ``payload_sha256(manifest.model_dump(mode="json"))``). A V1.1 manifest
    # sets all six from the frozen data-core constants.
    retrieval_profile: str | None = None
    query_policy_version: str | None = None
    candidate_depth: int | None = None
    display_top_k: int | None = None
    max_chunks_per_document: int | None = None
    retrieval_policy_hash: str | None = None
    # Verifier / corrective-round / quote-rule identities the run executed
    # with (attribution/policy.py). Omitted when unset, like the retrieval
    # limits, so historical manifests keep their bytes and admission hash.
    attribution_policy: AttributionPolicy | None = None

    @field_validator(
        "request_hash",
        "data_runtime_identity_hash",
        "analyst_prompt_hash",
        "writer_prompt_hash",
    )
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value

    @field_validator(
        "candidate_depth", "display_top_k", "max_chunks_per_document"
    )
    @classmethod
    def _positive_when_set(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("must be a positive integer when set")
        return value

    @field_validator("retrieval_policy_hash")
    @classmethod
    def _policy_hash_hex(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError(
                "retrieval_policy_hash must be a lowercase SHA-256 hex digest"
            )
        return value

    @field_validator("web_cache_rows_sha256")
    @classmethod
    def _web_cache_hash_hex(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError("web_cache_rows_sha256 must be a lowercase SHA-256 hex digest")
        return value

    @model_serializer(mode="wrap")
    def _omit_unset_retrieval_limits(self, handler):
        data = handler(self)
        for field in (
            "retrieval_profile",
            "query_policy_version",
            "candidate_depth",
            "display_top_k",
            "max_chunks_per_document",
            "retrieval_policy_hash",
            "attribution_policy",
            "parent_run_id",
            "web_search_lead_id",
            "web_cache_rows_sha256",
            "stage2_temporal_identity",
            "press_release_archive",
            "filing_units",
        ):
            if data.get(field) is None:
                data.pop(field, None)
        return data

    @model_validator(mode="after")
    def _production_bounds(self) -> "RunManifest":
        if self.max_corrective_rounds != MAX_CORRECTIVE_ROUNDS_PRODUCTION:
            raise ValueError(
                "production freeze: max_corrective_rounds must equal 1"
            )
        if self.max_actions_per_batch != MAX_ACTIONS_PER_BATCH_PRODUCTION:
            raise ValueError(
                "production freeze: max_actions_per_batch must equal 1"
            )
        investigator_timeout = investigator_run_timeout(self.attribution_policy)
        allowed = (
            {investigator_timeout}
            if investigator_timeout
            else {INITIAL_RUN_TIMEOUT_SECONDS, LEGACY_INITIAL_RUN_TIMEOUT_SECONDS}
        )
        if self.run_timeout_seconds not in allowed:
            raise ValueError(
                f"initial run deadline is run_timeout_seconds={INITIAL_RUN_TIMEOUT_SECONDS}; narrower stage "
                "budgets belong in the typed runtime configuration"
            )
        if self.context_token_budget < 1:
            raise ValueError("context_token_budget must be positive")
        return self


__all__ = [
    "ContextBudget",
    "INITIAL_RUN_TIMEOUT_SECONDS",
    "INVESTIGATOR_RUN_TIMEOUT_SECONDS",
    "INVESTIGATOR_V3_RUN_TIMEOUT_SECONDS",
    "investigator_run_timeout",
    "MAX_ACTIONS_PER_BATCH_PRODUCTION",
    "MAX_CORRECTIVE_ROUNDS_PRODUCTION",
    "ObservationPolicyConfig",
    "RunManifest",
    "RuntimeConfiguration",
]
