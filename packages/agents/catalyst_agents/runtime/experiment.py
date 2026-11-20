"""A1-A5 experiment policy override (M7-7).

One immutable ``ExperimentPolicyOverride`` exposes exactly the A1 (packing),
A2 (hypothesis), A3 (corrective rounds), and A5 (observation) seams plus an
eval capability token owned by the caller. ``None`` preserves the M6
production behavior; each arm changes exactly one policy seam and the
effective policy versions are recorded in run artifacts. No case ID or gold
label ever enters an override. A4 is schema/eligibility readiness only and
has no executable arm in M7.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

EVAL_CAPABILITY_TOKEN = "catalyst-eval-stage1"

A1_PACKING_VERSIONS = ("critic_prefix_600chars_v1", "evidence_context_pack_v1")
A2_HYPOTHESIS_VERSIONS = ("legacy_single_candidate_v1", "bounded_competition_v1")
A3_ROUNDS = (0, 1)
A5_OBSERVATION_VERSIONS = ("context_builder_v1_limited", "move_profile_v1")

# Production defaults recorded when no override is supplied (M6 identity).
PRODUCTION_HYPOTHESIS_POLICY_VERSION = "bounded_competition_v1"
PRODUCTION_OBSERVATION_POLICY_VERSION = "move_profile_v1"
PRODUCTION_MAX_CORRECTIVE_ROUNDS = 1


@dataclass(frozen=True)
class ExperimentPolicyOverride:
    """Immutable eval-only policy seams; never carries case ids or gold."""

    eval_capability_token: str
    a1_packing_policy_version: str | None = None
    a2_hypothesis_policy_version: str | None = None
    a3_max_corrective_rounds: int | None = None
    a5_observation_policy_version: str | None = None

    def __post_init__(self) -> None:
        if not self.eval_capability_token:
            raise ValueError("ExperimentPolicyOverride requires an eval capability token")
        if self.eval_capability_token != EVAL_CAPABILITY_TOKEN:
            raise ValueError(
                "eval capability token is not owned by the Stage-1 eval caller"
            )
        if (
            self.a1_packing_policy_version is not None
            and self.a1_packing_policy_version not in A1_PACKING_VERSIONS
        ):
            raise ValueError(
                f"A1 packing version must be one of {A1_PACKING_VERSIONS}, got "
                f"{self.a1_packing_policy_version!r}"
            )
        if (
            self.a2_hypothesis_policy_version is not None
            and self.a2_hypothesis_policy_version not in A2_HYPOTHESIS_VERSIONS
        ):
            raise ValueError(
                f"A2 hypothesis version must be one of {A2_HYPOTHESIS_VERSIONS}, got "
                f"{self.a2_hypothesis_policy_version!r}"
            )
        if (
            self.a3_max_corrective_rounds is not None
            and self.a3_max_corrective_rounds not in A3_ROUNDS
        ):
            raise ValueError(
                f"A3 max_corrective_rounds must be one of {A3_ROUNDS}, got "
                f"{self.a3_max_corrective_rounds!r}"
            )
        if (
            self.a5_observation_policy_version is not None
            and self.a5_observation_policy_version not in A5_OBSERVATION_VERSIONS
        ):
            raise ValueError(
                f"A5 observation version must be one of {A5_OBSERVATION_VERSIONS}, "
                f"got {self.a5_observation_policy_version!r}"
            )


def resolve_experiment_seams(
    *,
    packing_policy_version: str,
    experiment_override: ExperimentPolicyOverride | None,
) -> Mapping[str, object]:
    """Resolve the effective policy seams for one run.

    ``None`` reproduces the M6 production identity and behavior exactly.
    """
    if experiment_override is None:
        return {
            "packing_policy_version": packing_policy_version,
            "hypothesis_policy_version": PRODUCTION_HYPOTHESIS_POLICY_VERSION,
            "max_corrective_rounds": PRODUCTION_MAX_CORRECTIVE_ROUNDS,
            "observation_policy_version": PRODUCTION_OBSERVATION_POLICY_VERSION,
        }
    return {
        "packing_policy_version": (
            experiment_override.a1_packing_policy_version or packing_policy_version
        ),
        "hypothesis_policy_version": (
            experiment_override.a2_hypothesis_policy_version
            or PRODUCTION_HYPOTHESIS_POLICY_VERSION
        ),
        "max_corrective_rounds": (
            experiment_override.a3_max_corrective_rounds
            if experiment_override.a3_max_corrective_rounds is not None
            else PRODUCTION_MAX_CORRECTIVE_ROUNDS
        ),
        "observation_policy_version": (
            experiment_override.a5_observation_policy_version
            or PRODUCTION_OBSERVATION_POLICY_VERSION
        ),
    }


__all__ = [
    "A1_PACKING_VERSIONS",
    "A2_HYPOTHESIS_VERSIONS",
    "A3_ROUNDS",
    "A5_OBSERVATION_VERSIONS",
    "EVAL_CAPABILITY_TOKEN",
    "ExperimentPolicyOverride",
    "PRODUCTION_HYPOTHESIS_POLICY_VERSION",
    "PRODUCTION_MAX_CORRECTIVE_ROUNDS",
    "PRODUCTION_OBSERVATION_POLICY_VERSION",
    "resolve_experiment_seams",
]
