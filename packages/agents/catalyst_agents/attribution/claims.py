"""V1.1 claim boundary contracts (M2-7, corrective).

ClaimPlan/ValidatedClaimPlan/WriterInput (Frozen §6.5; Final Migration TSD
§13). AGENT-01 lock: an ABSTAIN WriterInput always uses the fixed abstention
path — no PRIMARY/SECONDARY causal claims, no NO_MATERIAL attribution type,
and is_fixed_abstention validates the actual structure rather than a boolean
marker.
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

    @model_validator(mode="after")
    def _unique_and_disjoint_evidence_refs(self) -> "Claim":
        if len(self.support_evidence_ids) != len(set(self.support_evidence_ids)):
            raise ValueError("support_evidence_ids must be unique")
        if len(self.counter_evidence_ids) != len(set(self.counter_evidence_ids)):
            raise ValueError("counter_evidence_ids must be unique")
        overlap = set(self.support_evidence_ids) & set(self.counter_evidence_ids)
        if overlap:
            raise ValueError(
                "support and counter evidence refs cannot overlap: "
                f"{sorted(overlap)}"
            )
        return self


def _validate_claim_ids_and_primary(claims: tuple[Claim, ...]) -> None:
    claim_ids = [claim.claim_id for claim in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("claim IDs must be unique")
    primary_count = sum(1 for claim in claims if claim.role is ClaimRole.PRIMARY)
    if primary_count > 1:
        raise ValueError("at most one PRIMARY claim is permitted")


class ClaimPlan(BaseModel):
    """Minimal claim list binding claims to evidence (Frozen §6.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AttributionStatus
    attribution_type: AttributionType
    claims: tuple[Claim, ...] = ()

    @model_validator(mode="after")
    def _claim_invariants(self) -> "ClaimPlan":
        _validate_claim_ids_and_primary(self.claims)
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
    def _claim_invariants(self) -> "ValidatedClaimPlan":
        _validate_claim_ids_and_primary(self.claims)
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

    @model_validator(mode="after")
    def _plan_consistency(self) -> "WriterInput":
        if self.final_status is not self.validated_claim_plan.status:
            raise ValueError("final_status must match the ValidatedClaimPlan status")
        if self.attribution_type is not self.validated_claim_plan.attribution_type:
            raise ValueError(
                "attribution_type must match the ValidatedClaimPlan attribution type"
            )
        return self

    @model_validator(mode="after")
    def _fixed_abstention_structure(self) -> "WriterInput":
        if self.final_status is not AttributionStatus.ABSTAIN:
            return self
        if self.attribution_type is not AttributionType.EVIDENCE_BACKED_CAUSAL:
            raise ValueError(
                "ABSTAIN WriterInput cannot carry NO_MATERIAL_PUBLIC_CATALYST; "
                "NO_MATERIAL requires completed gates and status PARTIAL"
            )
        causal_roles = {ClaimRole.PRIMARY, ClaimRole.SECONDARY}
        if any(claim.role in causal_roles for claim in self.validated_claim_plan.claims):
            raise ValueError(
                "fixed abstention path forbids PRIMARY/SECONDARY causal claims"
            )
        return self

    @model_validator(mode="after")
    def _citation_map_resolves(self) -> "WriterInput":
        plan_claims = {
            claim.claim_id: claim for claim in self.validated_claim_plan.claims
        }
        for claim_id, evidence_ids in self.citation_map.items():
            claim = plan_claims.get(claim_id)
            if claim is None:
                raise ValueError(
                    f"citation_map references unknown claim {claim_id!r}"
                )
            allowed = set(claim.support_evidence_ids) | set(
                claim.counter_evidence_ids
            )
            unknown = set(evidence_ids) - allowed
            if unknown:
                raise ValueError(
                    f"citation_map references evidence not bound to claim "
                    f"{claim_id!r}: {sorted(unknown)}"
                )
        return self


def is_fixed_abstention(writer_input: WriterInput) -> bool:
    """True when WriterInput follows the fixed abstention path (AGENT-01).

    The path is determined structurally: final_status is ABSTAIN and the
    validated plan contains no PRIMARY/SECONDARY causal claims. It is not a
    stored boolean marker.
    """
    if writer_input.final_status is not AttributionStatus.ABSTAIN:
        return False
    causal_roles = {ClaimRole.PRIMARY, ClaimRole.SECONDARY}
    return not any(
        claim.role in causal_roles for claim in writer_input.validated_claim_plan.claims
    )


__all__ = [
    "Claim",
    "ClaimPlan",
    "ClaimRole",
    "ValidatedClaimPlan",
    "WriterInput",
    "is_fixed_abstention",
]
