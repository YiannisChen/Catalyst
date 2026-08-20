"""V1.1 GoldenCase contract (M2-10).

Eval-owned human truth (eval TSD §6; Frozen §7.2). Value spaces are mirrored
with eval-local Literal aliases so eval does not add a reverse production
dependency merely to reuse agents/data-core classes. GoldenCase lives in eval
only and never enters production packages, prompts, RunManifest, or DTOs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

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


class AcceptableCauseLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cause_type: CauseTypeV1
    label: str
    direction: DirectionV1
    materiality: MaterialityV1


class EvidenceJudgmentV1(BaseModel):
    """Human judgment keyed by canonical evidence/chunk id (eval TSD §6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    canonical_asset_id: str
    role: EvidenceJudgmentRoleV1
    support: bool
    materiality: MaterialityV1
    temporal_eligible: bool
    independence_group: str | None = None


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
    lineage: dict[str, str]


__all__ = [
    "AcceptableCauseLabel",
    "AcceptableCorrectiveAction",
    "AttributionTypeV1",
    "CauseTypeV1",
    "DirectionV1",
    "EvidenceJudgmentRoleV1",
    "EvidenceJudgmentV1",
    "EvidenceNeedV1",
    "ExpectedResearchBehavior",
    "GapReasonCodeV1",
    "GoldenCase",
    "MaterialityV1",
    "OracleStatusV1",
    "TimeScopeV1",
]
