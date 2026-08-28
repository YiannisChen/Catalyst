"""M7-7: A1-A5 experiment policy override contract.

Immutable ExperimentPolicyOverride with explicit A1/A2/A3/A5 seams and an
eval capability token owned by the caller. ``None`` preserves the M6
production behavior exactly; each arm changes exactly one policy seam;
artifacts record the effective policy versions.
"""
from __future__ import annotations

import pytest

from catalyst_agents.runtime.experiment import (
    A1_PACKING_VERSIONS,
    A2_HYPOTHESIS_VERSIONS,
    A3_ROUNDS,
    A5_OBSERVATION_VERSIONS,
    EVAL_CAPABILITY_TOKEN,
    ExperimentPolicyOverride,
    resolve_experiment_seams,
)


def _override(**kwargs) -> ExperimentPolicyOverride:
    base = dict(eval_capability_token=EVAL_CAPABILITY_TOKEN)
    base.update(kwargs)
    return ExperimentPolicyOverride(**base)


def test_override_requires_capability_token():
    with pytest.raises(ValueError, match="token"):
        ExperimentPolicyOverride(eval_capability_token="")


def test_override_rejects_unknown_arm_versions():
    with pytest.raises(ValueError, match="A1"):
        _override(a1_packing_policy_version="not-a-real-version")
    with pytest.raises(ValueError, match="A2"):
        _override(a2_hypothesis_policy_version="not-a-real-version")
    with pytest.raises(ValueError, match="A5"):
        _override(a5_observation_policy_version="not-a-real-version")


def test_override_rejects_a3_outside_zero_or_one():
    with pytest.raises(ValueError, match="A3"):
        _override(a3_max_corrective_rounds=2)


def test_allowed_version_sets_match_the_documented_arms():
    assert set(A1_PACKING_VERSIONS) == {
        "critic_prefix_600chars_v1", "evidence_context_pack_v1",
    }
    assert set(A2_HYPOTHESIS_VERSIONS) == {
        "legacy_single_candidate_v1", "bounded_competition_v1",
    }
    assert set(A3_ROUNDS) == {0, 1}
    assert set(A5_OBSERVATION_VERSIONS) == {
        "context_builder_v1_limited", "move_profile_v1",
    }


def test_resolve_none_preserves_production_defaults():
    seams = resolve_experiment_seams(
        packing_policy_version="evidence_context_pack_v1",
        experiment_override=None,
    )
    assert seams["packing_policy_version"] == "evidence_context_pack_v1"
    assert seams["hypothesis_policy_version"] == "bounded_competition_v1"
    assert seams["max_corrective_rounds"] == 1
    assert seams["observation_policy_version"] == "move_profile_v1"


def test_a1_changes_only_packing_seam():
    seams = resolve_experiment_seams(
        packing_policy_version="evidence_context_pack_v1",
        experiment_override=_override(a1_packing_policy_version="critic_prefix_600chars_v1"),
    )
    assert seams["packing_policy_version"] == "critic_prefix_600chars_v1"
    assert seams["hypothesis_policy_version"] == "bounded_competition_v1"
    assert seams["max_corrective_rounds"] == 1
    assert seams["observation_policy_version"] == "move_profile_v1"


def test_a2_changes_only_hypothesis_seam():
    seams = resolve_experiment_seams(
        packing_policy_version="evidence_context_pack_v1",
        experiment_override=_override(a2_hypothesis_policy_version="legacy_single_candidate_v1"),
    )
    assert seams["hypothesis_policy_version"] == "legacy_single_candidate_v1"
    assert seams["packing_policy_version"] == "evidence_context_pack_v1"
    assert seams["max_corrective_rounds"] == 1


def test_a3_zero_disables_corrective_rounds():
    seams = resolve_experiment_seams(
        packing_policy_version="evidence_context_pack_v1",
        experiment_override=_override(a3_max_corrective_rounds=0),
    )
    assert seams["max_corrective_rounds"] == 0


def test_a5_changes_only_observation_seam():
    seams = resolve_experiment_seams(
        packing_policy_version="evidence_context_pack_v1",
        experiment_override=_override(a5_observation_policy_version="context_builder_v1_limited"),
    )
    assert seams["observation_policy_version"] == "context_builder_v1_limited"
    assert seams["max_corrective_rounds"] == 1


def test_override_is_immutable():
    override = _override(a1_packing_policy_version="critic_prefix_600chars_v1")
    with pytest.raises(Exception):
        override.a1_packing_policy_version = "evidence_context_pack_v1"  # type: ignore[misc]
