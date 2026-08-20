"""V1.1 claim boundary contracts (M2-7).

ClaimPlan/ValidatedClaimPlan/WriterInput (Frozen §6.5; Final Migration TSD
§13). AGENT-01 lock: an ABSTAIN WriterInput always uses the fixed abstention
path regardless of attribution_type and carries an abstention marker instead
of causal language.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
)


class ClaimRole(str, Enum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    CONTEXT = "CONTEXT"
    LIMITATION = "LIMITATION"


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    role: ClaimRole
    statement: str
    mechanism: str | None = None
    support_evidence_ids: tuple[str, ...] = ()
    counter_evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class ClaimPlan(BaseModel):
    """Minimal claim list binding claims to evidence (Frozen §6.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AttributionStatus
    attribution_type: AttributionType
    claims: tuple[Claim, ...] = ()

    @model_validator(mode="after")
    def _at_most_one_primary(self) -> "ClaimPlan":
        primary_count = sum(1 for claim in self.claims if claim.role is ClaimRole.PRIMARY)
        if primary_count > 1:
            raise ValueError("at most one PRIMARY claim is permitted")
        return self


class ValidatedClaimPlan(BaseModel):
    """Maximum public semantic surface the Writer may express (Frozen §6.5).

    Contains no numeric confidence or probability fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AttributionStatus
    attribution_type: AttributionType
    claims: tuple[Claim, ...] = ()

    @model_validator(mode="after")
    def _at_most_one_primary(self) -> "ValidatedClaimPlan":
        primary_count = sum(1 for claim in self.claims if claim.role is ClaimRole.PRIMARY)
        if primary_count > 1:
            raise ValueError("at most one PRIMARY claim is permitted")
        return self


class WriterInput(BaseModel):
    """Narrow Writer input over the ValidatedClaimPlan (Final Migration TSD §13)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    final_status: AttributionStatus
    attribution_type: AttributionType
    observed_move: str | None = None
    validated_claim_plan: ValidatedClaimPlan
    narrowly_bound_supporting_snippets: dict[str, str]
    citation_map: dict[str, tuple[str, ...]]
    required_limitations: tuple[str, ...] = ()
    format_style_constraints: dict[str, Any] = {}
    abstention_marker: bool

    @model_validator(mode="before")
    @classmethod
    def _derive_abstention_marker(cls, data: Any) -> Any:
        if isinstance(data, dict) and "abstention_marker" not in data:
            data = {
                **data,
                "abstention_marker": data.get("final_status") == AttributionStatus.ABSTAIN,
            }
        return data

    @model_validator(mode="after")
    def _abstention_marker_consistent(self) -> "WriterInput":
        if (self.final_status is AttributionStatus.ABSTAIN) != self.abstention_marker:
            raise ValueError(
                "abstention_marker must be True exactly when final_status is ABSTAIN"
            )
        return self


def is_fixed_abstention(writer_input: WriterInput) -> bool:
    """True when WriterInput follows the fixed abstention path.

    The fixed abstention path applies whenever final_status is ABSTAIN,
    regardless of attribution_type (AGENT-01).
    """
    return writer_input.final_status is AttributionStatus.ABSTAIN


__all__ = [
    "Claim",
    "ClaimPlan",
    "ClaimRole",
    "ValidatedClaimPlan",
    "WriterInput",
    "is_fixed_abstention",
]
