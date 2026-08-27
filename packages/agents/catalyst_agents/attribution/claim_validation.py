"""V1.1 ClaimValidator — deterministic, no LLM repair (M2-7 taxonomy, M5-6 impl).

Final TSD §13; Phase 4 TSD §§25–27. ``validate_claim_plan`` checks same-run
identity, temporal eligibility, materiality/source-role ceilings, canonical
independence relations, citation ownership, status ceiling, required
limitations, artifact hashes, and that support/counter/conflict refs are
exactly the typed bindings from the normalized EvidenceAssessment. CLAIM-01:
the validator performs no keyword/NLI/embedding/semantic inference — only
structural equality and enum/reference consistency.

Attribution-support failure drops/downgrades claims/roles/status and may yield
ABSTAIN. Integrity/system failure raises ``IntegritySystemFailure`` and never
becomes ABSTAIN. No LLM repair path exists.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from catalyst_agents.attribution.analyst import AttributionStatus
from catalyst_agents.attribution.claims import (
    CitationMapEntry,
    Claim,
    ClaimPlan,
    ClaimRole,
    SourceRoleIndependenceSummary,
    ValidatedClaimPlan,
)


class ClaimValidatorFailureType(str, Enum):
    ATTRIBUTION_SUPPORT = "ATTRIBUTION_SUPPORT"
    INTEGRITY_SYSTEM = "INTEGRITY_SYSTEM"


class ClaimValidatorFailure(Exception):
    """Base ClaimValidator failure carrying a typed failure class."""

    failure_type: ClaimValidatorFailureType

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"{self.failure_type.value}: {code}" + (f": {message}" if message else ""))


class AttributionSupportFailure(ClaimValidatorFailure):
    """Insufficient material support/independence; drops or downgrades, may ABSTAIN."""

    failure_type = ClaimValidatorFailureType.ATTRIBUTION_SUPPORT


class IntegritySystemFailure(ClaimValidatorFailure):
    """Missing evidence ID, cross-run evidence, schema invariant; fails the run."""

    failure_type = ClaimValidatorFailureType.INTEGRITY_SYSTEM


_STATUS_RANK = {
    AttributionStatus.ABSTAIN: 0,
    AttributionStatus.PARTIAL: 1,
    AttributionStatus.SUFFICIENT: 2,
}


def _integrity(message: str) -> IntegritySystemFailure:
    return IntegritySystemFailure("CLAIM_INTEGRITY_FAILURE", message)


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validated_plan_hash(
    *,
    status: AttributionStatus,
    claims: tuple[Claim, ...],
    assessment_hash: str,
    context_pack_sha256: str,
    evidence_state_hash: str,
    required_limitations: tuple[str, ...],
    ordering_policy_version: str,
) -> str:
    payload = {
        "status": status.value,
        "claims": [claim.model_dump(mode="json") for claim in claims],
        "assessment_hash": assessment_hash,
        "context_pack_sha256": context_pack_sha256,
        "evidence_state_hash": evidence_state_hash,
        "required_limitations": sorted(required_limitations),
        "ordering_policy_version": ordering_policy_version,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _source_role_summary(
    claims: tuple[Claim, ...],
    inventory_by_id: dict[str, Any],
) -> SourceRoleIndependenceSummary:
    direct = 0
    primary_authority = 0
    unknown = 0
    commentary = 0
    known_groups: set[str] = set()
    for claim in claims:
        for evidence_id in claim.support_evidence_ids:
            item = inventory_by_id.get(evidence_id)
            if item is None:
                continue
            role = getattr(item, "evidence_role", "")
            if role == "DIRECT_PRIMARY":
                direct += 1
            elif role == "PRIMARY_AUTHORITY":
                primary_authority += 1
            elif role == "COMMENTARY_LEAD":
                commentary += 1
            if getattr(item, "independence_status", None) == "UNKNOWN":
                unknown += 1
            if (
                getattr(item, "independence_status", None) == "KNOWN_GROUP"
                and getattr(item, "independence_group_id", None)
            ):
                known_groups.add(getattr(item, "independence_group_id"))
    return SourceRoleIndependenceSummary(
        direct_primary_support_count=direct,
        primary_authority_support_count=primary_authority,
        known_independent_report_group_count=len(known_groups),
        unknown_independence_support_count=unknown,
        commentary_lead_support_count=commentary,
    )


def _as_context(claim: Claim) -> Claim:
    return claim.model_copy(
        update={"role": ClaimRole.CONTEXT, "magnitude_fit": None}
    )


def validate_claim_plan(
    plan: ClaimPlan,
    assessment: Any,
    runtime_identity: Any,
    temporal_identity: Any,
    *,
    evidence_inventory: tuple[Any, ...] = (),
) -> ValidatedClaimPlan:
    """Deterministically validate a ClaimPlan into the maximum public surface.

    Integrity failures raise ``IntegritySystemFailure`` (the run fails, never
    ABSTAIN). Attribution-support failures downgrade/drop claims or status in
    the returned ValidatedClaimPlan.
    """
    # 1. Same-run artifact identity.
    if plan.assessment_hash != assessment.assessment_hash:
        raise _integrity("plan assessment_hash does not match the normalized assessment")
    if plan.context_pack_sha256 != assessment.context_pack_sha256:
        raise _integrity("plan context_pack_sha256 does not match the assessment")
    if plan.evidence_state_hash != assessment.evidence_state_hash:
        raise _integrity("cross-run evidence identity: plan evidence_state_hash mismatch")

    # 2. Runtime identity binding is unconditional: the assessment carries the
    #    typed DataRuntimeIdentity and must equal the injected runtime identity.
    if assessment.data_runtime_identity != runtime_identity:
        raise _integrity("runtime identity mismatch between plan/assessment and runtime")

    inventory_by_id = {item.evidence_id: item for item in evidence_inventory}
    assessment_evidence_ids = {
        decision.evidence_id for decision in assessment.normalized_evidence_decisions
    }
    hypothesis_by_id = {
        hypothesis.hypothesis_id: hypothesis
        for hypothesis in assessment.normalized_hypotheses
    }

    # 3. Evidence refs must resolve to the current assessment/inventory.
    for claim in plan.claims:
        refs = (
            set(claim.support_evidence_ids)
            | set(claim.counter_evidence_ids)
            | set(claim.citation_evidence_ids)
        )
        unknown = refs - assessment_evidence_ids
        if unknown:
            raise _integrity(f"claim cites unknown evidence ids: {sorted(unknown)}")
        if inventory_by_id:
            missing_inventory = refs - set(inventory_by_id)
            if missing_inventory:
                raise _integrity(
                    f"claim cites evidence missing from the inventory: {sorted(missing_inventory)}"
                )

    # 4. Support/counter/conflict refs must EXACTLY equal the typed bindings of
    #    the source hypothesis in the normalized assessment (CLAIM-01: no
    #    inference). Omitted counter-evidence and injected evidence both fail.
    for claim in plan.claims:
        if claim.source_hypothesis_id is None:
            continue
        hypothesis = hypothesis_by_id.get(claim.source_hypothesis_id)
        if hypothesis is None:
            raise _integrity(f"claim references unknown source hypothesis {claim.source_hypothesis_id}")
        if set(claim.support_evidence_ids) != set(hypothesis.supporting_evidence_ids):
            raise _integrity(
                f"claim {claim.claim_id} support refs are not EXACTLY the typed "
                f"bindings of {claim.source_hypothesis_id}"
            )
        if set(claim.counter_evidence_ids) != set(hypothesis.contradicting_evidence_ids):
            raise _integrity(
                f"claim {claim.claim_id} counter refs are not EXACTLY the typed "
                f"bindings of {claim.source_hypothesis_id}"
            )
        if claim.role in (ClaimRole.PRIMARY, ClaimRole.SECONDARY):
            if set(claim.conflict_refs) != set(assessment.normalized_conflicts):
                raise _integrity(
                    f"claim {claim.claim_id} conflict refs are not EXACTLY the "
                    "assessment's normalized conflicts"
                )

    # 5. Temporal eligibility: every evidence ref that can affect a claim
    #    (support, counter, and citation) must be eligible at cutoff.
    if inventory_by_id:
        for claim in plan.claims:
            refs = (
                set(claim.support_evidence_ids)
                | set(claim.counter_evidence_ids)
                | set(claim.citation_evidence_ids)
            )
            for evidence_id in refs:
                item = inventory_by_id.get(evidence_id)
                if item is not None and getattr(item, "eligible_at", None) is not None:
                    if item.eligible_at > temporal_identity.cutoff_at:
                        raise _integrity(
                            f"post-cutoff evidence {evidence_id} on claim {claim.claim_id}"
                        )

    # 6. Materiality + source-role ceilings: causal claims need material support.
    validated_claims: list[Claim] = []
    for claim in plan.claims:
        if claim.role in (ClaimRole.PRIMARY, ClaimRole.SECONDARY):
            support_items = [
                inventory_by_id[eid]
                for eid in claim.support_evidence_ids
                if eid in inventory_by_id
            ]
            if not any(
                getattr(item, "material_capability", None) == "MATERIAL_CAPABLE"
                for item in support_items
            ):
                claim = _as_context(claim)
        validated_claims.append(claim)

    # 7. Status constraints: SUFFICIENT requires primary-grade or two known
    #    independent news groups and no unknown-lineage support.
    status = plan.status
    if status is AttributionStatus.SUFFICIENT:
        primary_support: list[Any] = []
        for claim in validated_claims:
            if claim.role is ClaimRole.PRIMARY:
                primary_support.extend(
                    inventory_by_id[eid]
                    for eid in claim.support_evidence_ids
                    if eid in inventory_by_id
                )
        has_primary_grade = any(
            getattr(item, "evidence_role", None) in {"DIRECT_PRIMARY", "PRIMARY_AUTHORITY"}
            for item in primary_support
        )
        known_groups = {
            getattr(item, "independence_group_id", None)
            for item in primary_support
            if getattr(item, "evidence_role", None) == "INDEPENDENT_REPORT"
            and getattr(item, "independence_status", None) == "KNOWN_GROUP"
            and getattr(item, "independence_group_id", None)
        }
        unknown_lineage = any(
            getattr(item, "independence_status", None) == "UNKNOWN"
            and getattr(item, "evidence_role", None)
            not in {"DIRECT_PRIMARY", "PRIMARY_AUTHORITY"}
            for item in primary_support
        )
        if (not has_primary_grade and len(known_groups) < 2) or unknown_lineage:
            status = AttributionStatus.PARTIAL

    # 8. Status ceiling (locked rank formula).
    if _STATUS_RANK[status] > _STATUS_RANK[assessment.status_ceiling]:
        status = assessment.status_ceiling

    # 9. Required limitations must be present when conflicts/gaps exist.
    required_limitations = plan.required_limitations
    if assessment.normalized_conflicts or assessment.validated_missing_evidence:
        if not required_limitations:
            status = AttributionStatus.PARTIAL

    # 10. If no publishable causal claim remains, the result becomes ABSTAIN
    #     (fixed abstention path; never a system failure).
    causal_roles = {ClaimRole.PRIMARY, ClaimRole.SECONDARY}
    if status is not AttributionStatus.ABSTAIN and not any(
        claim.role in causal_roles for claim in validated_claims
    ):
        status = AttributionStatus.ABSTAIN
    if status is AttributionStatus.ABSTAIN:
        validated_claims = [
            _as_context(claim)
            if claim.role in causal_roles
            else claim
            for claim in validated_claims
        ]

    claims_tuple = tuple(validated_claims)
    citation_map = tuple(
        CitationMapEntry(claim_id=claim.claim_id, citation_evidence_ids=claim.citation_evidence_ids)
        for claim in claims_tuple
    )
    permitted_claim_ids = tuple(claim.claim_id for claim in claims_tuple)
    used_evidence: set[str] = set()
    for claim in claims_tuple:
        used_evidence.update(claim.support_evidence_ids)
        used_evidence.update(claim.counter_evidence_ids)
        used_evidence.update(claim.citation_evidence_ids)
    permitted_evidence_ids = tuple(sorted(used_evidence))
    summary = _source_role_summary(claims_tuple, inventory_by_id)
    plan_hash = _validated_plan_hash(
        status=status,
        claims=claims_tuple,
        assessment_hash=plan.assessment_hash,
        context_pack_sha256=plan.context_pack_sha256,
        evidence_state_hash=plan.evidence_state_hash,
        required_limitations=required_limitations,
        ordering_policy_version=plan.ordering_policy_version,
    )
    return ValidatedClaimPlan(
        status=status,
        attribution_type=plan.attribution_type,
        claims=claims_tuple,
        assessment_hash=plan.assessment_hash,
        context_pack_sha256=plan.context_pack_sha256,
        evidence_state_hash=plan.evidence_state_hash,
        required_limitations=required_limitations,
        citation_map=citation_map,
        permitted_claim_ids=permitted_claim_ids,
        permitted_evidence_ids=permitted_evidence_ids,
        source_role_independence_summary=summary,
        ordering_policy_version=plan.ordering_policy_version,
        plan_hash=plan_hash,
    )


__all__ = [
    "AttributionSupportFailure",
    "ClaimValidatorFailure",
    "ClaimValidatorFailureType",
    "IntegritySystemFailure",
    "validate_claim_plan",
]
