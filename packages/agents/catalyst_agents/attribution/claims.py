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
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
    MagnitudeFit,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_FORMAT_ITEM_MAX_LENGTH = 200
_FORMAT_COLLECTION_MAX_ITEMS = 16
_CLAIM_TEXT_MAX_LENGTH = 4_000
_SNIPPET_TEXT_MAX_LENGTH = 4_000
_CLAIM_COLLECTION_MAX_ITEMS = 16
_CLAIM_REFERENCE_MAX_ITEMS = 16
_CLAIM_REFERENCE_ID_MAX_LENGTH = 256


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
    order_index: int

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
        if not self.claim_id or not self.statement:
            raise ValueError("claim_id and statement must not be empty")
        if len(self.statement) > _CLAIM_TEXT_MAX_LENGTH:
            raise ValueError("claim statement must be length-bounded")
        if self.mechanism is not None and (
            not self.mechanism or len(self.mechanism) > _CLAIM_TEXT_MAX_LENGTH
        ):
            raise ValueError("claim mechanism must be non-empty and length-bounded")
        for field in (
            "support_evidence_ids",
            "counter_evidence_ids",
            "limitations",
            "conflict_refs",
            "citation_evidence_ids",
        ):
            values = getattr(self, field)
            if len(values) != len(set(values)) or any(not value for value in values):
                raise ValueError(f"{field} must contain unique non-empty values")
            if len(values) > _CLAIM_REFERENCE_MAX_ITEMS:
                raise ValueError(f"{field} must be collection-bounded")
            if field != "limitations" and any(
                len(value) > _CLAIM_REFERENCE_ID_MAX_LENGTH for value in values
            ):
                raise ValueError(f"{field} IDs must be length-bounded")
            if field == "limitations" and any(
                len(value) > _CLAIM_TEXT_MAX_LENGTH for value in values
            ):
                raise ValueError("claim limitations must be length-bounded")
        if self.source_hypothesis_id == "":
            raise ValueError("source_hypothesis_id must not be empty")
        if self.order_index < 0:
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
    if len(claims) > _CLAIM_COLLECTION_MAX_ITEMS:
        raise ValueError("claim plans must be collection-bounded")
    claim_ids = [claim.claim_id for claim in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("claim IDs must be unique")
    primary_count = sum(1 for claim in claims if claim.role is ClaimRole.PRIMARY)
    if primary_count > 1:
        raise ValueError("at most one PRIMARY claim is permitted")
    order_indices = [claim.order_index for claim in claims]
    if order_indices != list(range(len(claims))):
        raise ValueError("claim order_index values must be contiguous and match tuple order")


def _validate_sha256_hashes(**hashes: str) -> None:
    for name, value in hashes.items():
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


def _validate_required_limitations(limitations: tuple[str, ...]) -> None:
    if len(limitations) > _CLAIM_COLLECTION_MAX_ITEMS:
        raise ValueError("required_limitations must be collection-bounded")
    if len(limitations) != len(set(limitations)) or any(not value for value in limitations):
        raise ValueError("required_limitations must contain unique non-empty values")
    if any(len(value) > _CLAIM_TEXT_MAX_LENGTH for value in limitations):
        raise ValueError("required_limitations items must be length-bounded")


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
        _validate_required_limitations(self.required_limitations)
        _validate_sha256_hashes(
            assessment_hash=self.assessment_hash,
            context_pack_sha256=self.context_pack_sha256,
            evidence_state_hash=self.evidence_state_hash,
            plan_hash=self.plan_hash,
        )
        return self


class CitationMapEntry(BaseModel):
    """One immutable exact citation mapping for a validated claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    citation_evidence_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _canonical(self) -> "CitationMapEntry":
        if not self.claim_id or len(self.citation_evidence_ids) != len(
            set(self.citation_evidence_ids)
        ) or any(not evidence_id for evidence_id in self.citation_evidence_ids):
            raise ValueError("citation-map entry must contain canonical non-empty IDs")
        return self


class SupportingSnippet(BaseModel):
    """Bounded source-addressable Writer snippet (Phase 4 §28)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    snippet_text: str
    content_sha256: str
    source_start_offset: int = 0
    source_end_offset: int

    @model_validator(mode="after")
    def _bound_identity(self) -> "SupportingSnippet":
        if not self.evidence_id or not self.snippet_text:
            raise ValueError("supporting snippet identity and text must not be empty")
        if len(self.snippet_text) > _SNIPPET_TEXT_MAX_LENGTH:
            raise ValueError("supporting snippet text must be length-bounded")
        if _SHA256_RE.fullmatch(self.content_sha256) is None:
            raise ValueError("supporting snippet content_sha256 must be a lowercase SHA-256 hex digest")
        if self.source_start_offset < 0 or self.source_end_offset <= self.source_start_offset:
            raise ValueError("supporting snippet offsets must be non-negative and ordered")
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
    citation_map: tuple[CitationMapEntry, ...]
    permitted_claim_ids: tuple[str, ...] = ()
    permitted_evidence_ids: tuple[str, ...] = ()
    source_role_independence_summary: SourceRoleIndependenceSummary
    ordering_policy_version: str
    plan_hash: str

    @field_validator("citation_map", mode="before")
    @classmethod
    def _parse_citation_map(cls, value: object) -> object:
        """Accept JSON-object input but store the canonical immutable entries."""
        if isinstance(value, dict):
            return tuple(
                CitationMapEntry(
                    claim_id=claim_id, citation_evidence_ids=tuple(evidence_ids)
                )
                for claim_id, evidence_ids in value.items()
            )
        return value

    @model_validator(mode="after")
    def _claim_invariants(self) -> "ValidatedClaimPlan":
        _validate_claim_ids_and_primary(self.claims)
        _validate_required_limitations(self.required_limitations)
        _validate_sha256_hashes(
            assessment_hash=self.assessment_hash,
            context_pack_sha256=self.context_pack_sha256,
            evidence_state_hash=self.evidence_state_hash,
            plan_hash=self.plan_hash,
        )
        plan_claims = {claim.claim_id: claim for claim in self.claims}
        map_by_claim = {entry.claim_id: entry for entry in self.citation_map}
        if len(map_by_claim) != len(self.citation_map) or set(map_by_claim) != set(plan_claims):
            raise ValueError(
                "citation_map must contain exactly one entry per plan claim"
            )
        for claim_id, claim in plan_claims.items():
            mapped = map_by_claim[claim_id].citation_evidence_ids
            if mapped != claim.citation_evidence_ids:
                raise ValueError(
                    f"citation_map for {claim_id!r} must exactly equal its "
                    "citation_evidence_ids"
                )
        if self.permitted_claim_ids != tuple(claim.claim_id for claim in self.claims):
            raise ValueError(
                "permitted_claim_ids must canonically equal the validated plan claim IDs"
            )
        used_evidence = set()
        for claim in self.claims:
            used_evidence.update(claim.support_evidence_ids)
            used_evidence.update(claim.counter_evidence_ids)
            used_evidence.update(claim.citation_evidence_ids)
        if len(self.permitted_evidence_ids) != len(set(self.permitted_evidence_ids)) or (
            self.permitted_evidence_ids != tuple(sorted(used_evidence))
        ):
            raise ValueError(
                "permitted_evidence_ids must canonically equal the union of claim "
                "evidence refs (no missing IDs and no arbitrary superset)"
            )
        return self


class WriterFormatKind(str, Enum):
    CAUSAL = "CAUSAL"
    FIXED_ABSTENTION = "FIXED_ABSTENTION"


class WriterSection(str, Enum):
    SUMMARY = "SUMMARY"
    CAUSAL_EXPLANATION = "CAUSAL_EXPLANATION"
    OBSERVED_MOVE = "OBSERVED_MOVE"
    LIMITATIONS = "LIMITATIONS"


class WriterFormatStyleContract(BaseModel):
    """Bounded required section/format and style instructions (Phase 4 §28)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format_kind: WriterFormatKind = WriterFormatKind.CAUSAL
    required_sections: tuple[WriterSection, ...] = ()
    style_instructions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _bounded(self) -> "WriterFormatStyleContract":
        if self.format_kind is WriterFormatKind.FIXED_ABSTENTION:
            if self.required_sections != (
                WriterSection.OBSERVED_MOVE,
                WriterSection.LIMITATIONS,
            ) or self.style_instructions:
                raise ValueError("fixed abstention format uses only the typed observed-move and limitations sections")
            return self
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
                if not item:
                    raise ValueError(f"{field} items must not be empty")
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
    observed_move: str
    validated_claim_plan: ValidatedClaimPlan
    narrowly_bound_supporting_snippets: tuple[SupportingSnippet, ...]
    citation_map: tuple[CitationMapEntry, ...]
    required_limitations: tuple[str, ...]
    format_style_contract: WriterFormatStyleContract

    @field_validator("citation_map", mode="before")
    @classmethod
    def _parse_citation_map(cls, value: object) -> object:
        return ValidatedClaimPlan._parse_citation_map(value)

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
        if not self.observed_move or len(self.observed_move) > _CLAIM_TEXT_MAX_LENGTH:
            raise ValueError("observed_move must be non-empty and length-bounded")
        if len(self.narrowly_bound_supporting_snippets) > _CLAIM_COLLECTION_MAX_ITEMS:
            raise ValueError("supporting snippets must be collection-bounded")
        allowed_snippet_keys = set(self.validated_claim_plan.permitted_evidence_ids)
        snippet_ids = tuple(snippet.evidence_id for snippet in self.narrowly_bound_supporting_snippets)
        if len(snippet_ids) != len(set(snippet_ids)):
            raise ValueError("supporting snippet evidence IDs must be unique")
        unknown_snippet_keys = set(snippet_ids) - allowed_snippet_keys
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
        if not self.required_limitations:
            raise ValueError("fixed abstention WriterInput requires limitations")
        if self.narrowly_bound_supporting_snippets:
            raise ValueError("fixed abstention WriterInput cannot carry supporting snippets")
        if self.format_style_contract.format_kind is not WriterFormatKind.FIXED_ABSTENTION:
            raise ValueError("ABSTAIN WriterInput requires the typed fixed abstention format")
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
    "CitationMapEntry",
    "SourceRoleIndependenceSummary",
    "SupportingSnippet",
    "ValidatedClaimPlan",
    "WriterFormatStyleContract",
    "WriterFormatKind",
    "WriterSection",
    "WriterInput",
    "is_fixed_abstention",
]
