"""V1.1 run lifecycle contract (M2-8).

App-owned technical lifecycle (Final Migration TSD §10/§11). AttributionStatus
and AttributionType stay separate agents-owned ontologies; FAILED/CANCELLED
never synthesize an AttributionStatus.
"""
from __future__ import annotations

from enum import Enum

from catalyst_agents.attribution.analyst import AttributionStatus


class RunLifecycleStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ALLOWED_TRANSITIONS: dict[RunLifecycleStatus, frozenset[RunLifecycleStatus]] = {
    RunLifecycleStatus.ACCEPTED: frozenset(
        {
            RunLifecycleStatus.RUNNING,
            RunLifecycleStatus.CANCELLED,
            RunLifecycleStatus.FAILED,
        }
    ),
    RunLifecycleStatus.RUNNING: frozenset(
        {
            RunLifecycleStatus.CANCEL_REQUESTED,
            RunLifecycleStatus.COMPLETED,
            RunLifecycleStatus.FAILED,
            RunLifecycleStatus.CANCELLED,
        }
    ),
    RunLifecycleStatus.CANCEL_REQUESTED: frozenset(
        {
            RunLifecycleStatus.CANCELLED,
            RunLifecycleStatus.FAILED,
        }
    ),
    RunLifecycleStatus.COMPLETED: frozenset(),
    RunLifecycleStatus.FAILED: frozenset(),
    RunLifecycleStatus.CANCELLED: frozenset(),
}


def can_transition(
    current: RunLifecycleStatus, target: RunLifecycleStatus
) -> bool:
    """True when target is an allowed transition from current (Final TSD §11)."""
    return target in ALLOWED_TRANSITIONS[current]


def is_terminal(status: RunLifecycleStatus) -> bool:
    return status in {
        RunLifecycleStatus.COMPLETED,
        RunLifecycleStatus.FAILED,
        RunLifecycleStatus.CANCELLED,
    }


def is_capacity_bearing(status: RunLifecycleStatus) -> bool:
    """Capacity-bearing lifecycle states (Final TSD §11 admission slots)."""
    return status in {
        RunLifecycleStatus.ACCEPTED,
        RunLifecycleStatus.RUNNING,
        RunLifecycleStatus.CANCEL_REQUESTED,
    }


def attribution_status_for(
    lifecycle: RunLifecycleStatus, attribution_status: AttributionStatus | None
) -> AttributionStatus | None:
    """Project the nullable attribution status for a lifecycle.

    Only COMPLETED may carry an AttributionStatus. FAILED, CANCELLED, and all
    non-COMPLETED lifecycle states never synthesize an AttributionStatus
    (Final Migration TSD §10; Phase 5 TSD §8.1).
    """
    if lifecycle is not RunLifecycleStatus.COMPLETED:
        return None
    return attribution_status


__all__ = [
    "ALLOWED_TRANSITIONS",
    "RunLifecycleStatus",
    "attribution_status_for",
    "can_transition",
    "is_capacity_bearing",
    "is_terminal",
]
