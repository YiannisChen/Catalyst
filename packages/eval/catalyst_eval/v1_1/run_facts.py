"""Strict, recursive validation for persisted V1.1 run facts.

Persisted facts are an evidence boundary. This module accepts only already-
typed values and rejects malformed JSON values instead of repairing them
during report or audit reconstruction.
"""
from __future__ import annotations

import math
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from catalyst_eval.benchmark.pool_manifest import PoolManifest

RUN_FACTS_SCHEMA_VERSION = "v1_1_stage1_run_facts_v1"
OUTPUT_STATUSES = frozenset({"SUFFICIENT", "PARTIAL", "ABSTAIN"})
ATTRIBUTION_TYPES = frozenset(
    {"EVIDENCE_BACKED_CAUSAL", "NO_MATERIAL_PUBLIC_CATALYST"}
)
CLAIM_ROLES = frozenset({"PRIMARY", "SECONDARY", "CONTEXT", "LIMITATION"})
COST_METHODS = frozenset({"reported", "upper_bound_charged", "unavailable"})

OutputStatus = Literal["SUFFICIENT", "PARTIAL", "ABSTAIN"]
AttributionType = Literal[
    "EVIDENCE_BACKED_CAUSAL", "NO_MATERIAL_PUBLIC_CATALYST"
]
ClaimRole = Literal["PRIMARY", "SECONDARY", "CONTEXT", "LIMITATION"]
CostMethod = Literal["reported", "upper_bound_charged", "unavailable"]
StrictCost = StrictInt | StrictFloat


def _non_empty(value: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _unique(values: list[str], field: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {field}")
    return values


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ClaimFacts(_StrictModel):
    claim_id: StrictStr
    material: StrictBool
    role: ClaimRole
    citation_ids: list[StrictStr]
    statement: StrictStr | None = None

    @field_validator("claim_id", mode="after")
    @classmethod
    def _claim_id_non_empty(cls, value: str) -> str:
        return _non_empty(value, "claim_id")

    @field_validator("citation_ids", mode="after")
    @classmethod
    def _citation_ids_valid(cls, value: list[str]) -> list[str]:
        for citation_id in value:
            _non_empty(citation_id, "citation_id")
        return _unique(value, "citation_ids")


class TrajectoryFacts(_StrictModel):
    corrective_triggered: StrictBool
    rounds_executed: StrictInt = Field(ge=0)
    gap_reason_codes: list[StrictStr]
    corrective_actions: list[StrictStr]
    research_fingerprints: list[StrictStr]
    evidence_delta_ids: list[StrictStr]
    produced_structure: StrictBool

    @field_validator(
        "gap_reason_codes",
        "corrective_actions",
        "research_fingerprints",
        "evidence_delta_ids",
        mode="after",
    )
    @classmethod
    def _identities_are_non_empty_and_unique(
        cls, value: list[str], info: Any
    ) -> list[str]:
        field = str(info.field_name)
        for item in value:
            _non_empty(item, field)
        return _unique(value, field)


class RetrievalFacts(_StrictModel):
    observed: StrictBool
    pool: dict[str, Any] | None
    ranked_evidence_ids: list[StrictStr]
    candidate_evidence_ids: list[StrictStr]
    reranker_contributed: StrictBool
    latency_ms: StrictInt | None = Field(default=None, ge=0)
    degraded: StrictBool
    ticker_violations: list[StrictStr]
    cutoff_violations: list[StrictStr]

    @field_validator(
        "ranked_evidence_ids",
        "candidate_evidence_ids",
        "ticker_violations",
        "cutoff_violations",
        mode="after",
    )
    @classmethod
    def _ids_are_strings(cls, value: list[str], info: Any) -> list[str]:
        field = str(info.field_name)
        for item in value:
            _non_empty(item, field)
        return _unique(value, field)

    @model_validator(mode="after")
    def _pool_and_observation_truth(self) -> "RetrievalFacts":
        if not self.observed:
            if (
                self.pool is not None
                or self.ranked_evidence_ids
                or self.candidate_evidence_ids
                or self.reranker_contributed
                or self.latency_ms is not None
                or self.degraded
                or self.ticker_violations
                or self.cutoff_violations
            ):
                raise ValueError(
                    "retrieval observed=false cannot carry measured results"
                )
            return self
        if self.pool is None:
            raise ValueError("retrieval observed=true requires a PoolManifest")
        pool = _validate_pool_manifest(self.pool)
        candidate_ids = set(self.candidate_evidence_ids)
        if candidate_ids != set(pool.chunk_inventory):
            raise ValueError(
                "retrieval candidate_evidence_ids must match pool.chunk_inventory"
            )
        if not set(self.ranked_evidence_ids).issubset(candidate_ids):
            raise ValueError(
                "retrieval ranked_evidence_ids must come from candidate_evidence_ids"
            )
        return self


class ProviderAccountingFacts(_StrictModel):
    provider_calls: StrictInt | None = Field(default=None, ge=0)
    tokens_in: StrictInt | None = Field(default=None, ge=0)
    tokens_out: StrictInt | None = Field(default=None, ge=0)
    cost_usd: StrictCost | None = None
    cost_method: CostMethod

    @field_validator("cost_usd", mode="after")
    @classmethod
    def _cost_is_finite(cls, value: int | float | None) -> int | float | None:
        if value is not None and (not math.isfinite(float(value)) or value < 0):
            raise ValueError("cost_usd must be a finite non-negative number")
        return value

    @model_validator(mode="after")
    def _cost_method_matches_presence(self) -> "ProviderAccountingFacts":
        if self.cost_usd is None and self.cost_method != "unavailable":
            raise ValueError("cost_method must be unavailable when cost_usd is null")
        if self.cost_usd is not None and self.cost_method == "unavailable":
            raise ValueError(
                "cost_method cannot be unavailable when cost_usd is present"
            )
        return self


class RunFactsModel(_StrictModel):
    schema_version: StrictStr
    case_id: StrictStr
    output_status: OutputStatus
    attribution_type: AttributionType
    refusal_reason: StrictStr | None
    refusal_reason_available: StrictBool
    claims: list[ClaimFacts]
    sanity_tasks_completed: list[StrictStr]
    latency_ms: StrictInt | None = Field(default=None, ge=0)
    tokens: StrictInt | None = Field(default=None, ge=0)
    cost_usd: StrictCost | None = None
    model_limited: StrictBool
    trajectory: TrajectoryFacts
    retrieval: RetrievalFacts
    provider_accounting: ProviderAccountingFacts

    # Persisted adapter metadata is allowed but never used to repair metrics.
    status_ceiling: StrictStr | None = None
    diagnostics_artifact_hash: StrictStr | None = None
    run_id: StrictStr | None = None
    terminal_status: StrictStr | None = None
    terminal_failure_code: StrictStr | None = None
    provider_calls: StrictInt | None = Field(default=None, ge=0)

    @field_validator("schema_version", mode="after")
    @classmethod
    def _schema_version_is_binding(cls, value: str) -> str:
        if value != RUN_FACTS_SCHEMA_VERSION:
            raise ValueError("schema_version is not the binding run-facts version")
        return value

    @field_validator(
        "case_id",
        "refusal_reason",
        "status_ceiling",
        "diagnostics_artifact_hash",
        "run_id",
        "terminal_status",
        "terminal_failure_code",
        mode="after",
    )
    @classmethod
    def _optional_strings_non_empty(cls, value: str | None, info: Any) -> str | None:
        if value is not None:
            _non_empty(value, str(info.field_name))
        return value

    @field_validator("sanity_tasks_completed", mode="after")
    @classmethod
    def _sanity_tasks_are_unique(cls, value: list[str]) -> list[str]:
        for task_id in value:
            _non_empty(task_id, "sanity_tasks_completed")
        return _unique(value, "sanity_tasks_completed")

    @field_validator("claims", mode="after")
    @classmethod
    def _claim_ids_are_unique(cls, value: list[ClaimFacts]) -> list[ClaimFacts]:
        claim_ids = [claim.claim_id for claim in value]
        _unique(claim_ids, "claim_id")
        return value

    @field_validator("cost_usd", mode="after")
    @classmethod
    def _top_level_cost_is_finite(cls, value: int | float | None) -> int | float | None:
        if value is not None and (not math.isfinite(float(value)) or value < 0):
            raise ValueError("cost_usd must be a finite non-negative number")
        return value

    @model_validator(mode="after")
    def _cross_field_truth(self) -> "RunFactsModel":
        if self.refusal_reason is not None and not self.refusal_reason_available:
            raise ValueError("refusal_reason requires refusal_reason_available=true")
        if (
            self.provider_calls is not None
            and self.provider_accounting.provider_calls != self.provider_calls
        ):
            raise ValueError(
                "provider_calls does not match provider_accounting.provider_calls"
            )

        provider = self.provider_accounting
        if provider.tokens_in is None or provider.tokens_out is None:
            if self.tokens is not None:
                raise ValueError(
                    "tokens must be null when provider token usage is incomplete"
                )
        elif self.tokens != provider.tokens_in + provider.tokens_out:
            raise ValueError("tokens does not match provider token sum")
        if self.cost_usd != provider.cost_usd:
            raise ValueError("cost_usd does not match provider_accounting.cost_usd")
        if self.retrieval.pool is not None:
            pool = _validate_pool_manifest(self.retrieval.pool)
            if pool.case_id != self.case_id:
                raise ValueError("retrieval.pool.case_id does not match run case_id")
        return self


def _validate_pool_manifest(pool: Mapping[str, Any]) -> PoolManifest:
    """Validate the nested PoolManifest without accepting scalar coercion."""
    required = {
        "schema_version",
        "pool_id",
        "case_id",
        "arms",
        "chunk_inventory",
        "corpus_manifest_id",
        "index_manifest_id",
        "source_artifact_id",
        "created_at",
    }
    if set(pool) != required:
        raise ValueError("retrieval.pool must be a complete PoolManifest")
    for field in (
        "schema_version",
        "pool_id",
        "case_id",
        "corpus_manifest_id",
        "source_artifact_id",
    ):
        if type(pool[field]) is not str:
            raise ValueError(f"retrieval.pool.{field} must be a strict string")
    if pool["index_manifest_id"] is not None and type(pool["index_manifest_id"]) is not str:
        raise ValueError("retrieval.pool.index_manifest_id must be a string or null")
    if not isinstance(pool["arms"], (list, tuple)):
        raise ValueError("retrieval.pool.arms must be a sequence")
    for arm in pool["arms"]:
        if not isinstance(arm, Mapping) or set(arm) != {"arm", "version", "top_k"}:
            raise ValueError("retrieval.pool.arms must contain complete PoolArm objects")
        if type(arm["arm"]) is not str or type(arm["version"]) is not str:
            raise ValueError("retrieval.pool arm identity fields must be strict strings")
        if type(arm["top_k"]) is not int or isinstance(arm["top_k"], bool):
            raise ValueError("retrieval.pool arm top_k must be a strict int")
    if not isinstance(pool["chunk_inventory"], (list, tuple)):
        raise ValueError("retrieval.pool.chunk_inventory must be a sequence")
    for item in pool["chunk_inventory"]:
        if type(item) is not str or not item.strip():
            raise ValueError(
                "retrieval.pool.chunk_inventory must contain non-empty strings"
            )
    if type(pool["created_at"]) is not str or not pool["created_at"].strip():
        raise ValueError("retrieval.pool.created_at must be a persisted ISO string")
    try:
        return PoolManifest.model_validate(pool)
    except ValidationError as exc:
        raise ValueError(f"retrieval.pool is not a valid PoolManifest: {exc}") from exc


def validate_run_facts(
    facts: Mapping[str, Any],
    *,
    expected_case_id: str | None = None,
    row_provider_calls: int | None = None,
) -> RunFactsModel:
    """Strictly validate persisted run facts and return typed facts."""
    if not isinstance(facts, Mapping):
        raise ValueError("run facts must be an object")
    try:
        model = RunFactsModel.model_validate(dict(facts))
    except ValidationError as exc:
        messages = "; ".join(_validation_message(error) for error in exc.errors())
        raise ValueError(f"invalid persisted run facts: {messages}") from exc
    if expected_case_id is not None and model.case_id != expected_case_id:
        raise ValueError("run facts case_id does not match the expected case")
    if row_provider_calls is not None and model.provider_accounting.provider_calls != row_provider_calls:
        raise ValueError(
            "row provider_calls does not match provider_accounting.provider_calls"
        )
    return model


def _validation_message(error: dict[str, Any]) -> str:
    """Keep fail-closed errors tied to the persisted field, not Pydantic jargon."""
    location = ".".join(str(item) for item in error.get("loc", ()))
    error_type = str(error.get("type", ""))
    if error_type == "bool_type":
        return f"{location} must be a strict bool"
    if error_type in {"int_type", "greater_than_equal"}:
        return f"{location} must be a non-negative int"
    if error_type in {"float_type", "int_parsing", "float_parsing"}:
        return f"{location} must be a finite non-negative number"
    if error_type == "literal_error" and location == "attribution_type":
        return "attribution_type must be a legal non-empty enum"
    if location.startswith("sanity_tasks_completed"):
        return "sanity_tasks_completed must contain non-empty strings"
    if location.endswith("cost_usd"):
        return f"{location} must be a finite non-negative number"
    return f"{location} {error.get('msg', 'is invalid')}".strip()


__all__ = [
    "ATTRIBUTION_TYPES",
    "CLAIM_ROLES",
    "COST_METHODS",
    "OUTPUT_STATUSES",
    "RUN_FACTS_SCHEMA_VERSION",
    "RunFactsModel",
    "validate_run_facts",
]
