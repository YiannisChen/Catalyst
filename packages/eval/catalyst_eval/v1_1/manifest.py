"""V1.1 EvalManifest contract (M2-10, corrective).

Eval-owned experiment identity (eval TSD §10; Frozen §7.3). All authoritative
identity and metric contracts are typed groups; DataRuntimeIdentity and
RunManifest are referenced by ID/hash and never reproduced. Outcome is
append-once: identity is frozen before execution, outcome starts absent, one
append completes the manifest, a second append fails.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

StageV1 = Literal["stage1", "stage2", "stage3"]
SplitV1 = Literal["dev", "validation", "holdout"]

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class EvaluationIdentity(BaseModel):
    """Written before execution; order and hash are immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_id: str
    schema_version: str
    dataset_id: str
    dataset_version: str
    stage: StageV1
    split: SplitV1
    ordered_case_ids: tuple[str, ...]
    case_list_sha256: str

    @field_validator("case_list_sha256")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def _ordered_cases_are_canonical(self) -> "EvaluationIdentity":
        if not self.ordered_case_ids:
            raise ValueError("ordered_case_ids must not be empty")
        if len(self.ordered_case_ids) != len(set(self.ordered_case_ids)):
            raise ValueError("ordered_case_ids must be unique")
        # The frozen documents require the stored case-list digest but do not
        # define its canonical byte serialization. M2 therefore validates its
        # shape and preserves it as an external identity, without inventing a
        # competing hash algorithm.
        return self


class PackageVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str


class ModelByRole(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    model_id: str
    prompt_template_version: str
    prompt_template_sha256: str

    @field_validator("prompt_template_sha256")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value


class CodeProviderIdentity(BaseModel):
    """Code/package/harness/provider identity; models by role and seed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code_git_sha: str
    package_versions: tuple[PackageVersion, ...] = ()
    harness_revision: str
    provider_versions: tuple[PackageVersion, ...] = ()
    models_by_role: tuple[ModelByRole, ...] = ()
    random_seed: int


class DataRuntimeIdentityReference(BaseModel):
    """Typed reference to the data-core DataRuntimeIdentity; never a copy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data_runtime_identity_ref: str
    data_runtime_identity_hash: str

    @field_validator("data_runtime_identity_hash")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value


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


class EvalArtifactRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    artifact_hash: str | None = None


class RunArtifactIdentity(BaseModel):
    """Ordered RunManifest/artifact/ContextPack/claim/assurance refs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_manifest_bindings: tuple[RunManifestBinding, ...] = ()
    context_pack_refs: tuple[EvalArtifactRef, ...] = ()
    claim_plan_refs: tuple[EvalArtifactRef, ...] = ()
    assurance_refs: tuple[EvalArtifactRef, ...] = ()


class RetrievalPolicyIdentity(BaseModel):
    """Arm names/order, top-K, pool, dedup/independence/reranker policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm_names: tuple[str, ...]
    arm_order: tuple[str, ...]
    top_k: int = Field(ge=1)
    candidate_pool_id: str
    dedup_policy_version: str
    independence_policy_version: str
    reranker_policy_version: str
    latency_bound_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _arm_names_match_order(self) -> "RetrievalPolicyIdentity":
        if not self.arm_names or not self.arm_order:
            raise ValueError("arm_names and arm_order must not be empty")
        if len(self.arm_names) != len(set(self.arm_names)):
            raise ValueError("arm_names must be unique")
        if len(self.arm_order) != len(set(self.arm_order)):
            raise ValueError("arm_order must be unique")
        if set(self.arm_names) != set(self.arm_order):
            raise ValueError("arm_order must contain exactly the arm_names")
        return self


class AgentPolicyIdentity(BaseModel):
    """Observation/ContextPack/Analyst/Writer/corrective and A1–A5 seams."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_policy_version: str
    context_pack_policy_version: str
    analyst_policy_version: str
    writer_policy_version: str
    token_budget: int | None = Field(default=None, ge=0)
    corrective_rounds: int | None = Field(default=None, ge=0)
    actions_per_batch: int | None = Field(default=None, ge=0)
    a1_policy_version: str
    a2_policy_version: str
    a3_policy_version: str
    a4_policy_version: str
    a5_policy_version: str


class MetricDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_id: str
    numerator_rule: str
    denominator_rule: str
    eligible_count: int = 0
    excluded_count: int = 0
    non_scorable_count: int = 0
    hard_gate: bool = False

    @model_validator(mode="after")
    def _counts_non_negative(self) -> "MetricDefinition":
        for field in ("eligible_count", "excluded_count", "non_scorable_count"):
            if getattr(self, field) < 0:
                raise ValueError(f"{field} must be non-negative")
        return self


class MetricContract(BaseModel):
    """Typed metric contract; no dict[str, Any] for authoritative metrics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_spec_version: str
    definitions: tuple[MetricDefinition, ...] = ()


class EligibleExperiment(BaseModel):
    """A predeclared eligible experiment subset and its denominator (Frozen §7.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    eligibility_predicate: str
    ordered_eligible_ids: tuple[str, ...]
    eligibility_hash: str
    minimum_eligible_denominator: int = Field(ge=1)
    observed_denominator: int | None = Field(default=None, ge=0)

    @field_validator("eligibility_hash")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value


class CaseResultRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    run_manifest_id: str
    run_manifest_hash: str
    result_artifact_id: str

    @field_validator("run_manifest_hash")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value


class MetricAggregate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_id: str
    numerator: int = 0
    denominator: int = Field(ge=1)
    eligible_count: int = 0
    excluded_count: int = 0
    non_scorable_count: int = 0
    hard_gate_passed: bool | None = None

    @model_validator(mode="after")
    def _counts_non_negative(self) -> "MetricAggregate":
        for field in ("numerator", "eligible_count", "excluded_count", "non_scorable_count"):
            if getattr(self, field) < 0:
                raise ValueError(f"{field} must be non-negative")
        if self.numerator > self.denominator:
            raise ValueError("numerator must not exceed denominator")
        return self


class LatencyTokensCost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_latency_ms: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    total_cost: float = Field(ge=0)


class GateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: str
    passed: bool
    detail: str | None = None


class EvalOutcome(BaseModel):
    """Appended after execution; identity fields never change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    completed_at: datetime
    observed_run_artifact_identity: RunArtifactIdentity
    per_case_result_refs: tuple[CaseResultRef, ...] = ()
    aggregate_metrics: tuple[MetricAggregate, ...] = ()
    latency_tokens_cost: LatencyTokensCost
    gate_results: tuple[GateResult, ...] = ()

    @model_validator(mode="after")
    def _result_refs_are_unique(self) -> "EvalOutcome":
        observed = [ref.case_id for ref in self.per_case_result_refs]
        if len(observed) != len(set(observed)):
            raise ValueError("per_case_result_refs case_ids must be unique")
        return self

    @model_validator(mode="after")
    def _bindings_match_refs_one_to_one(self) -> "EvalOutcome":
        """Ordered per-case refs must bind one-to-one to the observed
        RunManifest id+hash; missing, duplicate, reordered, extra, or
        mismatched bindings fail closed (M7 execution lock, Batch-B)."""
        refs = list(self.per_case_result_refs)
        bindings = list(self.observed_run_artifact_identity.run_manifest_bindings)
        if len(refs) != len(bindings):
            raise ValueError(
                "observed RunManifest bindings must match per_case_result_refs "
                f"one-to-one (refs={len(refs)} bindings={len(bindings)})"
            )
        for index, (ref, binding) in enumerate(zip(refs, bindings)):
            if (
                ref.run_manifest_id != binding.run_manifest_id
                or ref.run_manifest_hash != binding.run_manifest_hash
            ):
                raise ValueError(
                    f"binding at index {index} does not match ref {ref.case_id!r}: "
                    f"ref=({ref.run_manifest_id},{ref.run_manifest_hash}) "
                    f"binding=({binding.run_manifest_id},{binding.run_manifest_hash})"
                )
        return self


class EvalManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_identity: EvaluationIdentity
    code_provider_identity: CodeProviderIdentity
    data_identity: DataRuntimeIdentityReference
    run_artifact_identity: RunArtifactIdentity
    retrieval_policy: RetrievalPolicyIdentity
    agent_policy: AgentPolicyIdentity
    metric_contract: MetricContract
    eligible_experiments: tuple[EligibleExperiment, ...] = ()
    outcome: EvalOutcome | None = None

    def model_copy(self, *, update: dict[str, object] | None = None, deep: bool = False) -> "EvalManifest":
        """Identity is write-once; public update copies cannot replace it."""
        if update:
            raise TypeError("EvalManifest does not permit public model_copy updates")
        return super().model_copy(deep=deep)

    def append_outcome(self, outcome: EvalOutcome) -> "EvalManifest":
        """Append the outcome exactly once; a second append fails.

        The ordered observed bindings must match ``ordered_case_ids``
        one-to-one: missing, duplicate, reordered, identity-mismatched, or
        extra bindings fail closed (M7 execution lock).
        """
        if self.outcome is not None:
            raise ValueError("EvalManifest outcome can only be appended once")
        if not isinstance(outcome, EvalOutcome):
            raise TypeError("EvalManifest append_outcome requires an EvalOutcome")
        expected = list(self.evaluation_identity.ordered_case_ids)
        observed = [ref.case_id for ref in outcome.per_case_result_refs]
        if observed != expected:
            raise ValueError(
                "per_case_result_refs must match ordered_case_ids one-to-one "
                f"in order (expected {expected}, observed {observed})"
            )
        return type(self).model_validate(
            {**self.model_dump(mode="python"), "outcome": outcome.model_dump(mode="python")}
        )


__all__ = [
    "AgentPolicyIdentity",
    "CaseResultRef",
    "CodeProviderIdentity",
    "DataRuntimeIdentityReference",
    "EligibleExperiment",
    "EvalArtifactRef",
    "EvalManifest",
    "EvalOutcome",
    "EvaluationIdentity",
    "GateResult",
    "LatencyTokensCost",
    "MetricAggregate",
    "MetricContract",
    "MetricDefinition",
    "ModelByRole",
    "PackageVersion",
    "RetrievalPolicyIdentity",
    "RunArtifactIdentity",
    "RunManifestBinding",
    "SplitV1",
    "StageV1",
]
