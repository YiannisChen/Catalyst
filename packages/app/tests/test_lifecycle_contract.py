"""V1.1 run lifecycle/status ontology contract tests (M2-8).

Final Migration TSD §10/§11: RunLifecycleStatus is exactly the six V1.1
values; AttributionStatus/AttributionType stay separate; QUEUED/SUCCEEDED are
not valid new-write lifecycle values; FAILED/CANCELLED never synthesize an
AttributionStatus.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.analyst import (
    AttributionStatus,
    AttributionType,
)
from catalyst_app.lifecycle import (
    ALLOWED_TRANSITIONS,
    RunLifecycleStatus,
    attribution_status_for,
    can_transition,
    is_capacity_bearing,
    is_terminal,
)


def test_run_lifecycle_status_is_exactly_the_six_v1_1_values() -> None:
    assert [member.value for member in RunLifecycleStatus] == [
        "ACCEPTED",
        "RUNNING",
        "CANCEL_REQUESTED",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    ]
    assert "QUEUED" not in RunLifecycleStatus.__members__
    assert "SUCCEEDED" not in RunLifecycleStatus.__members__
    with pytest.raises(ValueError):
        RunLifecycleStatus("QUEUED")
    with pytest.raises(ValueError):
        RunLifecycleStatus("SUCCEEDED")


def test_status_and_type_ontologies_are_separate() -> None:
    assert [member.value for member in AttributionStatus] == [
        "SUFFICIENT",
        "PARTIAL",
        "ABSTAIN",
    ]
    assert [member.value for member in AttributionType] == [
        "EVIDENCE_BACKED_CAUSAL",
        "NO_MATERIAL_PUBLIC_CATALYST",
    ]
    assert "SUCCEEDED" not in AttributionStatus.__members__
    assert "SUCCEEDED" not in AttributionType.__members__


def test_allowed_transitions_match_final_tsd_11() -> None:
    assert ALLOWED_TRANSITIONS[RunLifecycleStatus.ACCEPTED] == frozenset(
        {RunLifecycleStatus.RUNNING, RunLifecycleStatus.CANCELLED, RunLifecycleStatus.FAILED}
    )
    assert ALLOWED_TRANSITIONS[RunLifecycleStatus.RUNNING] == frozenset(
        {
            RunLifecycleStatus.CANCEL_REQUESTED,
            RunLifecycleStatus.COMPLETED,
            RunLifecycleStatus.FAILED,
            RunLifecycleStatus.CANCELLED,
        }
    )
    assert ALLOWED_TRANSITIONS[RunLifecycleStatus.CANCEL_REQUESTED] == frozenset(
        {RunLifecycleStatus.CANCELLED, RunLifecycleStatus.FAILED}
    )
    assert can_transition(
        RunLifecycleStatus.RUNNING, RunLifecycleStatus.CANCEL_REQUESTED
    )
    assert not can_transition(
        RunLifecycleStatus.COMPLETED, RunLifecycleStatus.RUNNING
    )


def test_terminal_and_capacity_bearing_classification() -> None:
    assert is_terminal(RunLifecycleStatus.COMPLETED)
    assert is_terminal(RunLifecycleStatus.FAILED)
    assert is_terminal(RunLifecycleStatus.CANCELLED)
    assert not is_terminal(RunLifecycleStatus.ACCEPTED)
    assert is_capacity_bearing(RunLifecycleStatus.ACCEPTED)
    assert is_capacity_bearing(RunLifecycleStatus.RUNNING)
    assert is_capacity_bearing(RunLifecycleStatus.CANCEL_REQUESTED)
    assert not is_capacity_bearing(RunLifecycleStatus.COMPLETED)


def test_completed_partial_and_completed_abstain_are_valid_results() -> None:
    assert (
        attribution_status_for(RunLifecycleStatus.COMPLETED, AttributionStatus.PARTIAL)
        is AttributionStatus.PARTIAL
    )
    assert (
        attribution_status_for(RunLifecycleStatus.COMPLETED, AttributionStatus.ABSTAIN)
        is AttributionStatus.ABSTAIN
    )
    assert (
        attribution_status_for(RunLifecycleStatus.COMPLETED, AttributionStatus.SUFFICIENT)
        is AttributionStatus.SUFFICIENT
    )


def test_failed_and_cancelled_never_synthesize_attribution_status() -> None:
    assert (
        attribution_status_for(RunLifecycleStatus.FAILED, AttributionStatus.PARTIAL)
        is None
    )
    assert (
        attribution_status_for(RunLifecycleStatus.CANCELLED, AttributionStatus.ABSTAIN)
        is None
    )
    assert (
        attribution_status_for(RunLifecycleStatus.FAILED, None)
        is None
    )


def test_non_completed_states_never_carry_attribution_status() -> None:
    """Phase 6 corrective: only COMPLETED may carry an attribution status."""
    for lifecycle in (
        RunLifecycleStatus.ACCEPTED,
        RunLifecycleStatus.RUNNING,
        RunLifecycleStatus.CANCEL_REQUESTED,
    ):
        assert (
            attribution_status_for(lifecycle, AttributionStatus.PARTIAL)
            is None
        )
        assert (
            attribution_status_for(lifecycle, AttributionStatus.SUFFICIENT)
            is None
        )
        assert (
            attribution_status_for(lifecycle, AttributionStatus.ABSTAIN)
            is None
        )
