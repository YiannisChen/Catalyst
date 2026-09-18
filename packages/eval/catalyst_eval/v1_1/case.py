"""V1.1 GoldenCase contract (M2-10, corrective).

Eval-owned human truth (eval TSD §6; Frozen §7.2). Judgments preserve
canonical asset + content/chunk/fact + evidence-group identity; materiality
and temporal eligibility stay separate; typed human rationale/lineage fields
are retained. Value spaces are mirrored with eval-local Literal aliases so
eval does not add a reverse production dependency merely to reuse
agents/data-core classes. GoldenCase lives in eval only and never enters
production packages, prompts, RunManifest, or DTOs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OracleStatusV1 = Literal["SUFFICIENT", "PARTIAL", "ABSTAIN"]
AttributionTypeV1 = Literal[
    "EVIDENCE_BACKED_CAUSAL",
    "NO_MATERIAL_PUBLIC_CATALYST",
]
CauseTypeV1 = Literal[
    "COMPANY_SPECIFIC_CATALYST",
    "CONTINUATION",
    "SECTOR_MOVE",
    "MACRO_EVENT",
    "FUNDAMENTAL_REPRICING",
    "REPORTING_OR_ANALYST_CONTINUATION",
]
DirectionV1 = Literal["positive", "negative", "mixed", "unknown"]
MaterialityV1 = Literal["material", "non_material"]
EvidenceNeedV1 = Literal[
    "COMPANY_PRIMARY",
    "COMPANY_NEWS",
    "SECTOR_NEWS",
    "MACRO_EVENT",
    "MACRO_SERIES",
    "FUNDAMENTALS",
    "MARKET_STRUCTURE",
]
TimeScopeV1 = Literal[
    "SESSION_INFORMATION_WINDOW",
    "PRIOR_SESSION",
    "LOOKBACK_SESSIONS",
]
GapReasonCodeV1 = Literal[
    "MISSING_PRIMARY_CONFIRMATION",
    "MISSING_INDEPENDENT_CORROBORATION",
    "MISSING_PRIOR_SESSION_CONTEXT",
    "MISSING_SECTOR_CONTEXT",
    "MISSING_MACRO_CONTEXT",
    "MISSING_FUNDAMENTAL_CONTEXT",
    "CONFLICT_REQUIRES_RESOLUTION",
    "MARKET_STRUCTURE_UNSUPPORTED",
    "LOCAL_COVERAGE_GAP",
]
EvidenceJudgmentRoleV1 = Literal[
    "primary_support",
    "secondary_support",
    "contradiction",
    "lead_only",
    "irrelevant",
    "ineligible",
]
AdjudicationStateV1 = Literal["resolved", "pending", "second_pass_required"]


class AcceptableCauseLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cause_type: CauseTypeV1
    label: str
    direction: DirectionV1
    materiality: MaterialityV1


class EvidenceJudgmentV1(BaseModel):
    """Human judgment keyed by canonical evidence/chunk/fact id (eval TSD §6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    canonical_asset_id: str
    canonical_content_version_id: str
    chunk_id: str | None = None
    fact_id: str | None = None
    role: EvidenceJudgmentRoleV1
    support: bool
    materiality: MaterialityV1
    temporal_eligible: bool
    independence_group: str | None = None
    rationale: str | None = Field(default=None, max_length=500)
    annotator: str | None = None
    annotated_at: datetime | None = None

    @model_validator(mode="after")
    def _identity_invariants(self) -> "EvidenceJudgmentV1":
        if (self.chunk_id is None) == (self.fact_id is None):
            raise ValueError("exactly one of chunk_id or fact_id must be set")
        if self.chunk_id is not None and self.evidence_id != self.chunk_id:
            raise ValueError("text evidence_id must equal chunk_id")
        if self.fact_id is not None and self.evidence_id != self.fact_id:
            raise ValueError("structured evidence_id must equal fact_id")
        return self


class AcceptableCorrectiveAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_need: EvidenceNeedV1
    time_scope: TimeScopeV1


class ExpectedResearchBehavior(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    acceptable_initial_tasks: tuple[EvidenceNeedV1, ...] = ()
    expected_gap_reason_codes: tuple[GapReasonCodeV1, ...] = ()
    acceptable_corrective_actions: tuple[AcceptableCorrectiveAction, ...] = ()
    corrective_recoverable: bool
    corrective_required: bool


class GoldenCaseLineage(BaseModel):
    """Typed lineage/adjudication metadata (eval TSD §6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    model_assisted_fields: tuple[str, ...] = ()
    human_confirmed_fields: tuple[str, ...] = ()
    annotated_at: datetime
    adjudication_state: AdjudicationStateV1


class GoldenCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    ticker: str
    session_date: str
    cutoff: datetime
    question: str
    oracle_status: OracleStatusV1
    acceptable_cause_labels: tuple[AcceptableCauseLabel, ...] = ()
    evidence_judgments: tuple[EvidenceJudgmentV1, ...] = ()
    expected_primary_evidence: tuple[str, ...] = ()
    expected_refusal_reason: str | None = None
    expected_attribution_type: AttributionTypeV1 | None = None
    expected_research_behavior: ExpectedResearchBehavior
    notes: str | None = None
    dataset_version: str
    lineage: GoldenCaseLineage


# New V1.1 public surface terminology: the benchmark case contract is the same
# frozen human-truth contract as ``GoldenCase``; ``GoldenCase`` stays exported
# for legacy V1.1 callers/adapters (sealed compatibility).
BenchmarkCase = GoldenCase

__all__ = [
    "AcceptableCauseLabel",
    "AcceptableCorrectiveAction",
    "AdjudicationStateV1",
    "AttributionTypeV1",
    "CauseTypeV1",
    "DirectionV1",
    "EvidenceJudgmentRoleV1",
    "EvidenceJudgmentV1",
    "EvidenceNeedV1",
    "ExpectedResearchBehavior",
    "GapReasonCodeV1",
    "BenchmarkCase",
    "GoldenCase",
    "GoldenCaseLineage",
    "MaterialityV1",
    "OracleStatusV1",
    "TimeScopeV1",
]
