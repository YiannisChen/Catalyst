"""V1.1 EvalManifest contract (M2-10).

Eval-owned experiment identity (eval TSD §10; Frozen §7.3). It references
DataRuntimeIdentity and RunManifest by typed ID/hash references and never
reproduces their full authoritative fields or independently recomputes
runtime identity.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

StageV1 = Literal["stage1", "stage2", "stage3"]
SplitV1 = Literal["dev", "validation", "holdout"]

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class RunManifestBinding(BaseModel):
    """A typed id+hash reference to an authoritative agents RunManifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_manifest_id: str
    run_manifest_hash: str

    @field_validator("run_manifest_hash")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value


class EligibleExperiment(BaseModel):
    """A predeclared eligible experiment subset and its denominator (Frozen §7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    eligibility_predicate: str
    ordered_eligible_ids: tuple[str, ...]
    eligibility_hash: str
    minimum_eligible_denominator: int = Field(ge=1)
    observed_denominator: int | None = None

    @field_validator("eligibility_hash")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value


class EvalManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_id: str
    schema_version: str
    dataset_id: str
    dataset_version: str
    stage: StageV1
    split: SplitV1
    ordered_case_ids: tuple[str, ...]
    case_list_sha256: str
    code_revision: str
    provider: str
    model_id: str
    data_runtime_identity_ref: str
    data_runtime_identity_hash: str
    run_manifest_bindings: tuple[RunManifestBinding, ...] = ()
    retrieval_policy_version: str
    a1_policy_version: str
    a2_policy_version: str
    a3_policy_version: str
    a4_policy_version: str
    a5_policy_version: str
    metric_contract: dict[str, Any] = {}
    eligible_experiments: tuple[EligibleExperiment, ...] = ()
    outcome: dict[str, Any] | None = None

    @field_validator("case_list_sha256", "data_runtime_identity_hash")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value


__all__ = [
    "EligibleExperiment",
    "EvalManifest",
    "RunManifestBinding",
    "SplitV1",
    "StageV1",
]
