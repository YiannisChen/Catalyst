"""V1.1 RunManifest contract (M2-7, corrective).

Agents-owned production run identity (Final Migration TSD §14–15; Frozen
§6.7). RunManifest references DataRuntimeIdentity by ref/hash and never
duplicates an independently recomputed data identity. Production corrective
bounds (1 round, 1 action) and the initial 60-second run deadline are
enforced; runtime configuration is typed, not an unrestricted dict.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_agents.attribution.context_pack import ContextBudget
from catalyst_data.canonical.temporal import TemporalIdentity

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

MAX_CORRECTIVE_ROUNDS_PRODUCTION = 1
MAX_ACTIONS_PER_BATCH_PRODUCTION = 1
INITIAL_RUN_TIMEOUT_SECONDS = 60


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
    """Typed run runtime configuration (Phase 3 §6/§14).

    Carries policy/budget values only; never an identity-bearing free-form
    dict and never a duplicate DataRuntimeIdentity. The context budget is the
    canonical agents-owned ContextBudget type so pack and manifest budgets
    cannot diverge.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_policy: ObservationPolicyConfig
    context_budget: ContextBudget


class RunManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    request_hash: str
    temporal_identity: TemporalIdentity
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
        if self.run_timeout_seconds != INITIAL_RUN_TIMEOUT_SECONDS:
            raise ValueError(
                "initial run deadline is run_timeout_seconds=60; narrower stage "
                "budgets belong in the typed runtime configuration"
            )
        if self.context_token_budget < 1:
            raise ValueError("context_token_budget must be positive")
        return self


__all__ = [
    "ContextBudget",
    "INITIAL_RUN_TIMEOUT_SECONDS",
    "MAX_ACTIONS_PER_BATCH_PRODUCTION",
    "MAX_CORRECTIVE_ROUNDS_PRODUCTION",
    "ObservationPolicyConfig",
    "RunManifest",
    "RuntimeConfiguration",
]
