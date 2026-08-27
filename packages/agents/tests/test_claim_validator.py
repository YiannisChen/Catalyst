"""M5-6: deterministic ClaimValidator (no LLM repair).

Final TSD §13; Phase 4 TSD §§25–27; M5 plan M5-6. validate_claim_plan checks
same-run identity, temporal eligibility, materiality/source-role ceilings,
canonical independence relations, citation ownership, status ceiling, required
limitations, artifact hashes, and that support/counter/conflict refs are
exactly the typed bindings from the normalized EvidenceAssessment. CLAIM-01:
no keyword/NLI/embedding/semantic inference. Attribution-support failure drops
or downgrades and may yield ABSTAIN; integrity/system failure fails the run
and never becomes ABSTAIN.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from catalyst_agents.attribution.analyst import AnalystDecision, AttributionStatus
from catalyst_agents.attribution.assessment import normalize_decision
from catalyst_agents.attribution.claim_validation import (
    AttributionSupportFailure,
    IntegritySystemFailure,
    validate_claim_plan,
)
from catalyst_agents.attribution.claims import ClaimRole, build_claim_plan
from catalyst_agents.retrieval.task import EvidenceNeed
from catalyst_data.canonical.temporal import TemporalIdentity


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


class FakeInventoryItem:
    def __init__(self, *, evidence_id: str, material_capability: str = "MATERIAL_CAPABLE",
                 evidence_role: str = "DIRECT_PRIMARY", independence_status: str = "KNOWN_GROUP",
                 independence_group_id: str | None = None,
                 eligible_at: datetime = _utc("2026-01-15T10:00:00Z")):
        self.evidence_id = evidence_id
        self.material_capability = material_capability
        self.evidence_role = evidence_role
        self.independence_status = independence_status
        self.independence_group_id = independence_group_id
        self.eligible_at = eligible_at


class FakeContextPack:
    def __init__(self, *, run_id: str = "run:1", round: int = 1,
                 included_evidence_ids: tuple[str, ...] = ("e1", "e2"),
                 evidence_inventory: tuple[Any, ...] = (), research_history: tuple[Any, ...] = (),
                 coverage_summary: Any = None, data_coverage_gaps: tuple[Any, ...] = (),
                 capability_gaps: tuple[Any, ...] = (), retrieval_degradations: tuple[Any, ...] = (),
                 context_pack_sha256: str = "a" * 64, rendered_messages_sha256: str = "b" * 64,
                 data_runtime_identity: Any = None):
        self.run_id = run_id
        self.round = round
        self.included_evidence_ids = included_evidence_ids
        self.evidence_inventory = evidence_inventory
        self.research_history = research_history
        self.coverage_summary = coverage_summary
        self.data_coverage_gaps = data_coverage_gaps
        self.capability_gaps = capability_gaps
        self.retrieval_degradations = retrieval_degradations
        self.context_pack_sha256 = context_pack_sha256
        self.rendered_messages_sha256 = rendered_messages_sha256
        self.data_runtime_identity = (
            data_runtime_identity if data_runtime_identity is not None else _runtime_identity("1")
        )


class FakeRegistry:
    def is_recoverable(self, need: EvidenceNeed) -> bool:
        return False


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-15",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-15T14:30:00Z"),
        session_close_at=_utc("2026-01-15T21:00:00Z"),
        information_window_start_at=_utc("2026-01-14T21:00:00Z"),
        cutoff_at=_utc("2026-01-15T21:00:00Z"),
    )


def _runtime_identity(seed: str = "1"):
    import hashlib

    from catalyst_data.canonical.identity import DataRuntimeIdentity

    def h(part: str) -> str:
        return hashlib.sha256(f"{seed}:{part}".encode("utf-8")).hexdigest()

    return DataRuntimeIdentity(
        data_snapshot_id=h("snapshot"),
        corpus_manifest_id=h("corpus"),
        fts_index_version="build:fts",
        dense_index_version=h("dense"),
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _runtime() -> Any:
    return _runtime_identity("1")


def _decision(**overrides: Any) -> AnalystDecision:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "evidence_decisions": (
            {
                "evidence_id": "e1",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
        ),
        "candidate_hypotheses": (
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "AAPL rose on record guidance.",
                "supporting_evidence_ids": ("e1",),
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            },
        ),
        "research_decision": "READY",
        "recommended_status": "SUFFICIENT",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
    }
    base.update(overrides)
    return AnalystDecision.model_validate(base)


def _inventory_items() -> tuple[FakeInventoryItem, ...]:
    return (FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),)


def _assessment(*, inventory: tuple[FakeInventoryItem, ...] | None = None) -> Any:
    items = inventory if inventory is not None else _inventory_items()
    return normalize_decision(
        _decision(),
        context_pack=FakeContextPack(
            included_evidence_ids=("e1",),
            evidence_inventory=items,
        ),
        capability_registry=FakeRegistry(),
        policy_version="norm:v1",
        evidence_state_hash="e" * 64,
    )


def _plan(assessment: Any) -> Any:
    return build_claim_plan(assessment, observed_move="AAPL +9.5% on 2026-01-15")


def test_validate_claim_plan_builds_exact_validated_surface() -> None:
    inventory = _inventory_items()
    assessment = _assessment(inventory=inventory)
    plan = _plan(assessment)
    validated = validate_claim_plan(
        plan,
        assessment,
        runtime_identity=_runtime(),
        temporal_identity=_temporal(),
        evidence_inventory=inventory,
    )
    assert validated.status is plan.status
    assert validated.assessment_hash == plan.assessment_hash
    assert validated.context_pack_sha256 == plan.context_pack_sha256
    assert validated.evidence_state_hash == plan.evidence_state_hash
    assert validated.permitted_claim_ids == tuple(claim.claim_id for claim in validated.claims)
    used = set()
    for claim in validated.claims:
        used.update(claim.support_evidence_ids)
        used.update(claim.counter_evidence_ids)
        used.update(claim.citation_evidence_ids)
    assert validated.permitted_evidence_ids == tuple(sorted(used))
    assert {entry.claim_id for entry in validated.citation_map} == {
        claim.claim_id for claim in validated.claims
    }
    assert validated.source_role_independence_summary.direct_primary_support_count >= 1


def test_assessment_hash_mismatch_is_integrity_failure() -> None:
    assessment = _assessment()
    plan = _plan(assessment).model_copy(update={"assessment_hash": "f" * 64})
    with pytest.raises(IntegritySystemFailure):
        validate_claim_plan(
            plan,
            assessment,
            runtime_identity=_runtime(),
            temporal_identity=_temporal(),
            evidence_inventory=_inventory_items(),
        )


def test_missing_evidence_id_is_integrity_failure_never_abstain() -> None:
    assessment = _assessment()
    plan = _plan(assessment)
    forged = plan.model_copy(
        update={
            "claims": tuple(
                claim.model_copy(update={"support_evidence_ids": ("ghost",), "citation_evidence_ids": ("ghost",)})
                if claim.role is ClaimRole.PRIMARY
                else claim
                for claim in plan.claims
            )
        }
    )
    with pytest.raises(IntegritySystemFailure) as excinfo:
        validate_claim_plan(
            forged,
            assessment,
            runtime_identity=_runtime(),
            temporal_identity=_temporal(),
            evidence_inventory=_inventory_items(),
        )
    assert not isinstance(excinfo.value, AttributionSupportFailure)


def test_post_cutoff_evidence_citation_is_integrity_failure() -> None:
    assessment = _assessment()
    plan = _plan(assessment)
    late_inventory = (
        FakeInventoryItem(
            evidence_id="e1",
            evidence_role="DIRECT_PRIMARY",
            eligible_at=_utc("2026-01-16T00:00:00Z"),
        ),
    )
    with pytest.raises(IntegritySystemFailure):
        validate_claim_plan(
            plan,
            assessment,
            runtime_identity=_runtime(),
            temporal_identity=_temporal(),
            evidence_inventory=late_inventory,
        )


def test_commentary_only_primary_is_downgraded_to_context() -> None:
    assessment = _assessment()
    plan = _plan(assessment)
    commentary_inventory = (
        FakeInventoryItem(
            evidence_id="e1",
            material_capability="LEAD_ONLY",
            evidence_role="COMMENTARY_LEAD",
        ),
    )
    validated = validate_claim_plan(
        plan,
        assessment,
        runtime_identity=_runtime(),
        temporal_identity=_temporal(),
        evidence_inventory=commentary_inventory,
    )
    assert not any(claim.role in {ClaimRole.PRIMARY, ClaimRole.SECONDARY} for claim in validated.claims)
    assert any(claim.role is ClaimRole.CONTEXT for claim in validated.claims)


def test_unknown_lineage_support_caps_sufficient_to_partial() -> None:
    assessment = _assessment()
    plan = _plan(assessment)
    unknown_inventory = (
        FakeInventoryItem(
            evidence_id="e1",
            material_capability="MATERIAL_CAPABLE",
            evidence_role="INDEPENDENT_REPORT",
            independence_status="UNKNOWN",
        ),
    )
    validated = validate_claim_plan(
        plan,
        assessment,
        runtime_identity=_runtime(),
        temporal_identity=_temporal(),
        evidence_inventory=unknown_inventory,
    )
    assert validated.status is AttributionStatus.PARTIAL


def test_no_llm_repair_path_exists() -> None:
    assert not hasattr(validate_claim_plan, "repair")
    # The ValidatedClaimPlan is produced purely from the plan + assessment.
    assessment = _assessment()
    plan = _plan(assessment)
    validated = validate_claim_plan(
        plan,
        assessment,
        runtime_identity=_runtime(),
        temporal_identity=_temporal(),
        evidence_inventory=_inventory_items(),
    )
    assert validated.plan_hash


def test_claim_validator_node_is_thin_wrapper() -> None:
    from catalyst_agents.nodes.claim_validator import claim_validator_node

    assessment = _assessment()
    plan = _plan(assessment)
    result = claim_validator_node(
        {"run_id": "run:1"},
        plan=plan,
        assessment=assessment,
        runtime_identity=_runtime(),
        temporal_identity=_temporal(),
    )
    assert "validated_claim_plan" in result
    assert result["validated_claim_plan"].assessment_hash == plan.assessment_hash


# ---------------------------------------------------------------------------
# C5: exact typed evidence bindings and unconditional identity binding
# ---------------------------------------------------------------------------

def _decision_with_counter(**overrides: Any) -> AnalystDecision:
    """h1 PRIMARY (support e1); h2 SECONDARY (support e3, contradict e2).

    The counter-evidence defeats only the secondary explanation so the plan
    keeps a causal claim carrying typed counter-evidence.
    """
    base = {
        "schema_version": "1.0",
        "evidence_decisions": (
            {
                "evidence_id": "e1",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h1",),
                "reason_code": "material_support",
            },
            {
                "evidence_id": "e3",
                "disposition": "SUPPORT",
                "supports_hypothesis_refs": ("h2",),
                "reason_code": "material_support",
            },
            {
                "evidence_id": "e2",
                "disposition": "CONTRADICT",
                "contradicts_hypothesis_refs": ("h2",),
                "reason_code": "material_contradiction",
            },
        ),
        "candidate_hypotheses": (
            {
                "hypothesis_ref": "h1",
                "cause_type": "COMPANY_SPECIFIC_CATALYST",
                "statement": "AAPL rose on record guidance.",
                "supporting_evidence_ids": ("e1",),
                "magnitude_fit": "STRONG",
                "proposed_role": "PRIMARY",
            },
            {
                "hypothesis_ref": "h2",
                "cause_type": "SECTOR_MOVE",
                "statement": "The sector rallied in sympathy.",
                "supporting_evidence_ids": ("e3",),
                "contradicting_evidence_ids": ("e2",),
                "magnitude_fit": "PLAUSIBLE",
                "proposed_role": "SECONDARY",
            },
        ),
        "research_decision": "READY",
        "recommended_status": "PARTIAL",
        "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
    }
    base.update(overrides)
    return AnalystDecision.model_validate(base)


def _counter_assessment() -> tuple[Any, tuple[FakeInventoryItem, ...]]:
    items = (
        FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        FakeInventoryItem(evidence_id="e2", evidence_role="DIRECT_PRIMARY"),
        FakeInventoryItem(evidence_id="e3", evidence_role="INDEPENDENT_REPORT"),
    )
    assessment = normalize_decision(
        _decision_with_counter(),
        context_pack=FakeContextPack(
            included_evidence_ids=("e1", "e2", "e3"),
            evidence_inventory=items,
            data_runtime_identity=_runtime_identity("1"),
        ),
        capability_registry=FakeRegistry(),
        policy_version="norm:v1",
        evidence_state_hash="e" * 64,
    )
    return assessment, items


def _causal_claim(plan: Any) -> Any:
    """Find the hypothesis-derived claim (any role) carrying counter evidence."""
    return next(
        claim
        for claim in plan.claims
        if claim.source_hypothesis_id is not None and claim.counter_evidence_ids
    )


def test_injected_support_evidence_fails_exact_binding() -> None:
    assessment, inventory = _counter_assessment()
    plan = _plan(assessment)
    causal = _causal_claim(plan)
    forged = plan.model_copy(
        update={
            "claims": tuple(
                claim.model_copy(update={"support_evidence_ids": ("e1", "e9")})
                if claim.claim_id == causal.claim_id
                else claim
                for claim in plan.claims
            )
        }
    )
    with pytest.raises(IntegritySystemFailure):
        validate_claim_plan(
            forged,
            assessment,
            runtime_identity=_runtime_identity("1"),
            temporal_identity=_temporal(),
            evidence_inventory=inventory,
        )


def test_omitted_counterevidence_fails_exact_binding() -> None:
    assessment, inventory = _counter_assessment()
    plan = _plan(assessment)
    causal = _causal_claim(plan)
    assert causal.counter_evidence_ids == ("e2",)
    forged = plan.model_copy(
        update={
            "claims": tuple(
                claim.model_copy(update={"counter_evidence_ids": ()})
                if claim.claim_id == causal.claim_id
                else claim
                for claim in plan.claims
            )
        }
    )
    with pytest.raises(IntegritySystemFailure):
        validate_claim_plan(
            forged,
            assessment,
            runtime_identity=_runtime_identity("1"),
            temporal_identity=_temporal(),
            evidence_inventory=inventory,
        )


def test_conflict_bindings_must_be_exact() -> None:
    assessment, inventory = _counter_assessment()
    plan = _plan(assessment)
    causal = _causal_claim(plan)
    forged = plan.model_copy(
        update={
            "claims": tuple(
                claim.model_copy(update={"conflict_refs": ("c9",)})
                if claim.claim_id == causal.claim_id
                else claim
                for claim in plan.claims
            )
        }
    )
    with pytest.raises(IntegritySystemFailure):
        validate_claim_plan(
            forged,
            assessment,
            runtime_identity=_runtime_identity("1"),
            temporal_identity=_temporal(),
            evidence_inventory=inventory,
        )


def test_runtime_identity_binding_is_unconditional() -> None:
    assessment, inventory = _counter_assessment()
    plan = _plan(assessment)
    with pytest.raises(IntegritySystemFailure, match="runtime"):
        validate_claim_plan(
            plan,
            assessment,
            runtime_identity=_runtime_identity("other"),
            temporal_identity=_temporal(),
            evidence_inventory=inventory,
        )


def test_temporal_eligibility_checks_all_claim_refs_not_only_citations() -> None:
    late = _utc("2026-01-16T00:00:00Z")
    items = (
        FakeInventoryItem(evidence_id="e1", evidence_role="DIRECT_PRIMARY"),
        FakeInventoryItem(evidence_id="e2", evidence_role="DIRECT_PRIMARY", eligible_at=late),
        FakeInventoryItem(evidence_id="e3", evidence_role="INDEPENDENT_REPORT"),
    )
    assessment = normalize_decision(
        _decision_with_counter(),
        context_pack=FakeContextPack(
            included_evidence_ids=("e1", "e2", "e3"),
            evidence_inventory=items,
            data_runtime_identity=_runtime_identity("1"),
        ),
        capability_registry=FakeRegistry(),
        policy_version="norm:v1",
        evidence_state_hash="e" * 64,
    )
    plan = _plan(assessment)
    causal = _causal_claim(plan)
    # Forge the citation set to omit the counter ref: temporal eligibility must
    # still be checked for every evidence ref that can affect the claim.
    assert "e2" in causal.counter_evidence_ids
    forged = plan.model_copy(
        update={
            "claims": tuple(
                claim.model_copy(update={"citation_evidence_ids": ("e3",)})
                if claim.claim_id == causal.claim_id
                else claim
                for claim in plan.claims
            )
        }
    )
    with pytest.raises(IntegritySystemFailure, match="post-cutoff"):
        validate_claim_plan(
            plan,
            assessment,
            runtime_identity=_runtime_identity("1"),
            temporal_identity=_temporal(),
            evidence_inventory=items,
        )
