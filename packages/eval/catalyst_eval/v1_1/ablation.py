"""A1-A5 experiment seams + Stage-1 development signals (M7-7).

Eval-owned adapter: builds one-seam ``ExperimentPolicyOverride`` per arm and
labels Stage-1 A1/A2/A3/A5 outputs as development signals only. A4 is schema
+ eligibility-manifest readiness only (multi-gap subset, predicate version,
minimum denominator, refusal reasons) and never introduces or executes a
two-action batch arm in M7; executable A4 is Stage-2+ only.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from catalyst_agents.runtime.experiment import (
    A1_PACKING_VERSIONS,
    A2_HYPOTHESIS_VERSIONS,
    A3_ROUNDS,
    A5_OBSERVATION_VERSIONS,
    EVAL_CAPABILITY_TOKEN,
    ExperimentPolicyOverride,
)

A4_ELIGIBILITY_PREDICATE_VERSION = "multi-gap-recoverable-v1"


def _override(**seams) -> ExperimentPolicyOverride:
    return ExperimentPolicyOverride(
        eval_capability_token=EVAL_CAPABILITY_TOKEN, **seams
    )


def a1_arm(packing_version: str) -> ExperimentPolicyOverride:
    """A1: packing policy seam (critic_prefix_600chars_v1 vs evidence_context_pack_v1)."""
    if packing_version not in A1_PACKING_VERSIONS:
        raise ValueError(f"A1 version must be one of {A1_PACKING_VERSIONS}")
    return _override(a1_packing_policy_version=packing_version)


def a2_arm(hypothesis_version: str) -> ExperimentPolicyOverride:
    """A2: hypothesis policy seam (legacy_single_candidate_v1 vs bounded_competition_v1)."""
    if hypothesis_version not in A2_HYPOTHESIS_VERSIONS:
        raise ValueError(f"A2 version must be one of {A2_HYPOTHESIS_VERSIONS}")
    return _override(a2_hypothesis_policy_version=hypothesis_version)


def a3_arm(max_corrective_rounds: int) -> ExperimentPolicyOverride:
    """A3: corrective rounds seam (0 vs 1); executable A4 is Stage-2+ only."""
    if max_corrective_rounds not in A3_ROUNDS:
        raise ValueError(f"A3 rounds must be one of {A3_ROUNDS}")
    return _override(a3_max_corrective_rounds=max_corrective_rounds)


def a5_arm(observation_version: str) -> ExperimentPolicyOverride:
    """A5: observation policy seam (context_builder_v1_limited vs move_profile_v1)."""
    if observation_version not in A5_OBSERVATION_VERSIONS:
        raise ValueError(f"A5 version must be one of {A5_OBSERVATION_VERSIONS}")
    return _override(a5_observation_policy_version=observation_version)


def validate_a4_readiness(
    *,
    eligible_case_ids: Sequence[str],
    eligibility_predicate_version: str,
    minimum_eligible_denominator: int,
    refusal_reason_codes: Sequence[str],
    dataset_case_ids: Sequence[str],
) -> list[str]:
    """A4 readiness diagnostics only: never a promotion conclusion.

    Validates the predeclared multi-gap subset, predicate version, minimum
    denominator, and refusal reasons against the dataset. Below-minimum or
    unknown subsets are reported as diagnostics; they cannot satisfy
    retention/promotion.
    """
    diagnostics: list[str] = []
    dataset = set(dataset_case_ids)
    if eligibility_predicate_version != A4_ELIGIBILITY_PREDICATE_VERSION:
        diagnostics.append(
            f"A4 eligibility predicate version must be "
            f"{A4_ELIGIBILITY_PREDICATE_VERSION!r}, got "
            f"{eligibility_predicate_version!r}"
        )
    eligible = set(eligible_case_ids)
    unknown = eligible - dataset
    if unknown:
        diagnostics.append(f"A4 eligible ids reference unknown cases: {sorted(unknown)}")
    if not eligible:
        diagnostics.append("A4 eligible subset is empty")
    if minimum_eligible_denominator <= 0:
        diagnostics.append("A4 minimum_eligible_denominator must be positive")
    if len(eligible & dataset) < minimum_eligible_denominator:
        diagnostics.append(
            f"A4 eligible denominator {len(eligible & dataset)} is below the "
            f"predeclared minimum {minimum_eligible_denominator}"
        )
    if not refusal_reason_codes:
        diagnostics.append("A4 readiness requires documented refusal reason codes")
    return diagnostics


def stage1_development_signals(
    arm_results: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Stage-1 A1/A2/A3/A5 outputs are development signals only.

    A strong A3 efficacy claim requires Stage-2-or-later denominators.
    """
    known_arms = ("A1", "A2", "A3", "A5")
    missing = [arm for arm in known_arms if arm not in arm_results]
    return {
        "arms": list(known_arms),
        "missing_arms": missing,
        "development_signal_only": True,
        "strong_efficacy_claim": False,
        "requires_stage2_denominators": True,
    }


__all__ = [
    "A4_ELIGIBILITY_PREDICATE_VERSION",
    "a1_arm",
    "a2_arm",
    "a3_arm",
    "a5_arm",
    "stage1_development_signals",
    "validate_a4_readiness",
]
