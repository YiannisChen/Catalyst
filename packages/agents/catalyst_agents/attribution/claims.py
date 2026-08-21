"""V1.1 claim boundary contracts (M2-7, corrective; maximum public surface).

ClaimPlan/ValidatedClaimPlan/WriterInput (Frozen §6.5; Phase 4 TSD §§23, 27,
28; Final Migration TSD §13). The detailed Phase TSD shapes govern over the
abbreviated M2-7 field lists: claims carry source-hypothesis identity, copied
magnitude fit, conflict references, required citation IDs and deterministic
ordering metadata; plans carry assessment/ContextPack/EvidenceState identities
and hashes, required limitations, exact citation map, permitted claim/evidence
IDs, source-role/independence summary, ordering policy and plan hashes.
Maximum-public-surface seal: the canonical plan types cannot be instantiated
incompletely — artifact identity/hash fields, ordering policy, plan hash and
the validated source-role/independence summary are required; citation_map is
the exact per-claim representation of citation_evidence_ids; permitted IDs are
exact sets; WriterInput may not exceed the validated plan surface (snippet
keys ⊆ permitted evidence, required_limitations and citation_map equal the
plan, format/style contract required and bounded). Derivation of those values
(hash computation, deterministic construction, citation-map building) is M5
ClaimPlan construction; the schema contract lives here. AGENT-01 lock: an
ABSTAIN WriterInput always uses the fixed abstention path — no PRIMARY/SECONDARY
causal claims, no NO_MATERIAL attribution type, and is_fixed_abstention
validates the actual structure.
"""
from __future__ import annotations

import re
from enum import Enum
from pydantic import BaseModel, ConfigDict, model_validator

from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
    MagnitudeFit,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_FORMAT_ITEM_MAX_LENGTH = 200
_FORMAT_COLLECTION_MAX_ITEMS = 16


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
    source_hypothesis_id: str | None = None
    magnitude_fit: MagnitudeFit | None = None
    conflict_refs: tuple[str, ...] = ()
    citation_evidence_ids: tuple[str, ...] = ()
    order_index: int | None = None

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
        if len(self.conflict_refs) != len(set(self.conflict_refs)):
            raise ValueError("conflict_refs must be unique")
        if len(self.citation_evidence_ids) != len(set(self.citation_evidence_ids)):
            raise ValueError("citation_evidence_ids must be unique")
        allowed_citations = set(self.support_evidence_ids) | set(
            self.counter_evidence_ids
        )
        unknown_citations = set(self.citation_evidence_ids) - allowed_citations
        if unknown_citations:
            raise ValueError(
                f"citation_evidence_ids must be bound to claim {self.claim_id!r}: "
                f"{sorted(unknown_citations)}"
            )
        if self.order_index is not None and self.order_index < 0:
            raise ValueError("order_index must be non-negative")
        if self.role in (ClaimRole.PRIMARY, ClaimRole.SECONDARY):
            if self.source_hypothesis_id is None:
                raise ValueError(
                    f"causal claim {self.claim_id!r} requires source_hypothesis_id"
                )
            if self.magnitude_fit is None:
                raise ValueError(
                    f"causal claim {self.claim_id!r} requires magnitude_fit"
                )
        return self


def _validate_claim_ids_and_primary(claims: tuple[Claim, ...]) -> None:
    claim_ids = [claim.claim_id for claim in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("claim IDs must be unique")
    primary_count = sum(1 for claim in claims if claim.role is ClaimRole.PRIMARY)
    if primary_count > 1:
        raise ValueError("at most one PRIMARY claim is permitted")
    order_indices = [
        claim.order_index for claim in claims if claim.order_index is not None
    ]
    if len(order_indices) != len(set(order_indices)):
        raise ValueError("claim order_index values must be unique within the plan")


def _validate_sha256_hashes(**hashes: str) -> None:
    for name, value in hashes.items():
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


class SourceRoleIndependenceSummary(BaseModel):
    """Source-role and independence summary needed by assurance (Phase 4 §27)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    direct_primary_support_count: int = 0
    primary_authority_support_count: int = 0
    known_independent_report_group_count: int = 0
    unknown_independence_support_count: int = 0
    commentary_lead_support_count: int = 0

    @model_validator(mode="after")
    def _non_negative(self) -> "SourceRoleIndependenceSummary":
        for field, value in self.model_dump().items():
            if value < 0:
                raise ValueError(f"{field} must be non-negative")
        return self


class ClaimPlan(BaseModel):
    """Deterministic pre-validation claim proposal (Frozen §6.5; Phase 4 §23).

    Artifact identity/hash fields, ordering policy and plan hash are required:
    an incomplete ClaimPlan cannot be instantiated (maximum-public-surface
    seal). Value derivation is M5 ClaimPlan construction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AttributionStatus
    attribution_type: AttributionType
    claims: tuple[Claim, ...] = ()
    assessment_hash: str
    context_pack_sha256: str
    evidence_state_hash: str
    required_limitations: tuple[str, ...] = ()
    ordering_policy_version: str
    plan_hash: str

    @model_validator(mode="after")
    def _claim_invariants(self) -> "ClaimPlan":
        _validate_claim_ids_and_primary(self.claims)
        _validate_sha256_hashes(
            assessment_hash=self.assessment_hash,
            context_pack_sha256=self.context_pack_sha256,
            evidence_state_hash=self.evidence_state_hash,
            plan_hash=self.plan_hash,
        )
        return self


class ValidatedClaimPlan(BaseModel):
    """Maximum public semantic surface the Writer may express (Phase 4 §27).

    Contains no numeric confidence or probability fields. The citation map is
    the exact per-claim representation of citation_evidence_ids (including
    explicit empty tuples); permitted claim/evidence IDs are exact sets.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AttributionStatus
    attribution_type: AttributionType
    claims: tuple[Claim, ...] = ()
    assessment_hash: str
    context_pack_sha256: str
    evidence_state_hash: str
    required_limitations: tuple[str, ...] = ()
    citation_map: dict[str, tuple[str, ...]] = {}
    permitted_claim_ids: tuple[str, ...] = ()
    permitted_evidence_ids: tuple[str, ...] = ()
    source_role_independence_summary: SourceRoleIndependenceSummary
    ordering_policy_version: str
    plan_hash: str

    @model_validator(mode="after")
    def _claim_invariants(self) -> "ValidatedClaimPlan":
        _validate_claim_ids_and_primary(self.claims)
        _validate_sha256_hashes(
            assessment_hash=self.assessment_hash,
            context_pack_sha256=self.context_pack_sha256,
            evidence_state_hash=self.evidence_state_hash,
            plan_hash=self.plan_hash,
        )
        plan_claims = {claim.claim_id: claim for claim in self.claims}
        if set(self.citation_map) != set(plan_claims):
            raise ValueError(
                "citation_map must contain exactly one entry per plan claim"
            )
        for claim_id, claim in plan_claims.items():
            mapped = tuple(self.citation_map[claim_id])
            if mapped != claim.citation_evidence_ids:
                raise ValueError(
                    f"citation_map for {claim_id!r} must exactly equal its "
                    "citation_evidence_ids"
                )
        if set(self.permitted_claim_ids) != set(plan_claims):
            raise ValueError(
                "permitted_claim_ids must equal the validated plan claim IDs"
            )
        used_evidence = set()
        for claim in self.claims:
            used_evidence.update(claim.support_evidence_ids)
            used_evidence.update(claim.counter_evidence_ids)
            used_evidence.update(claim.citation_evidence_ids)
        if set(self.permitted_evidence_ids) != used_evidence:
            raise ValueError(
                "permitted_evidence_ids must exactly equal the union of claim "
                "evidence refs (no missing IDs and no arbitrary superset)"
            )
        return self


class WriterFormatStyleContract(BaseModel):
    """Bounded required section/format and style instructions (Phase 4 §28)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required_sections: tuple[str, ...] = ()
    style_instructions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _bounded(self) -> "WriterFormatStyleContract":
        for field in ("required_sections", "style_instructions"):
            items = getattr(self, field)
            if not items:
                raise ValueError(f"{field} must not be empty")
            if len(items) > _FORMAT_COLLECTION_MAX_ITEMS:
                raise ValueError(
                    f"{field} must not exceed {_FORMAT_COLLECTION_MAX_ITEMS} items"
                )
            if len(items) != len(set(items)):
                raise ValueError(f"{field} items must be unique")
            for item in items:
                if len(item) > _FORMAT_ITEM_MAX_LENGTH:
                    raise ValueError(f"{field} items must be length-bounded")
        return self


class WriterInput(BaseModel):
    """Narrow Writer input over the ValidatedClaimPlan (Phase 4 §28).

    The Writer may not exceed the validated plan surface: final status/type
    match the plan, citation_map and required_limitations equal the plan, and
    supporting snippet keys stay within the permitted evidence surface.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    final_status: AttributionStatus
    attribution_type: AttributionType
    observed_move: str | None = None
    validated_claim_plan: ValidatedClaimPlan
    narrowly_bound_supporting_snippets: dict[str, str]
    citation_map: dict[str, tuple[str, ...]]
    required_limitations: tuple[str, ...]
    format_style_contract: WriterFormatStyleContract

    @model_validator(mode="after")
    def _plan_consistency(self) -> "WriterInput":
        if self.final_status is not self.validated_claim_plan.status:
            raise ValueError("final_status must match the ValidatedClaimPlan status")
        if self.attribution_type is not self.validated_claim_plan.attribution_type:
            raise ValueError(
                "attribution_type must match the ValidatedClaimPlan attribution type"
            )
        if self.citation_map != self.validated_claim_plan.citation_map:
            raise ValueError(
                "WriterInput citation_map must equal the ValidatedClaimPlan citation map"
            )
        if self.required_limitations != self.validated_claim_plan.required_limitations:
            raise ValueError(
                "WriterInput required_limitations must equal the ValidatedClaimPlan "
                "required limitations"
            )
        allowed_snippet_keys = set(self.validated_claim_plan.permitted_evidence_ids)
        unknown_snippet_keys = set(self.narrowly_bound_supporting_snippets) - (
            allowed_snippet_keys
        )
        if unknown_snippet_keys:
            raise ValueError(
                "supporting snippet keys must stay within the permitted evidence "
                f"surface: {sorted(unknown_snippet_keys)}"
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
    "SourceRoleIndependenceSummary",
    "ValidatedClaimPlan",
    "WriterFormatStyleContract",
    "WriterInput",
    "is_fixed_abstention",
]
