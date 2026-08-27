"""ClaimValidator graph node — thin artifact-loading/persistence wrapper (M5-6).

The deterministic validation logic lives in
``attribution/claim_validation.validate_claim_plan``; this node only loads the
plan/assessment artifacts, runs the validator, and emits the validated plan.
"""
from __future__ import annotations

from typing import Any


def claim_validator_node(
    state: dict,
    *,
    plan: Any,
    assessment: Any,
    runtime_identity: Any,
    temporal_identity: Any,
    evidence_inventory: tuple[Any, ...] = (),
) -> dict:
    """Validate the ClaimPlan and emit the ValidatedClaimPlan artifact ref."""
    from catalyst_agents.attribution.claim_validation import validate_claim_plan

    validated = validate_claim_plan(
        plan,
        assessment,
        runtime_identity=runtime_identity,
        temporal_identity=temporal_identity,
        evidence_inventory=evidence_inventory,
    )
    return {
        "validated_claim_plan": validated,
        "run_id": state.get("run_id"),
    }


__all__ = ["claim_validator_node"]
