"""Agents-owned cooperative run control protocol (Final TSD §9.1).

The app owns cancellation requests, persisted lifecycle transitions, and the
process-local token registry; agents own the narrow observation protocol.
``RunControl`` exposes only the cooperative boundary surface
(``should_cancel``, ``deadline_epoch_ms``, ``cancellation_reason``) and never
imports packages/app. The graph observes it at semantic boundaries (before
and after observation/retrieval, before and after Analyst calls, before
corrective dispatch, between Writer stream chunks, before result-artifact
persistence, and immediately before terminal commit).

A provider call that cannot be cancelled is not interrupted; when it returns
after cancellation/deadline won, the graph raises the typed winner and later
stages are never dispatched.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol


class RunControl(Protocol):
    """Narrow agents-side control boundary observed by the graph."""

    def should_cancel(self) -> bool: ...

    def deadline_epoch_ms(self) -> int | None: ...

    def cancellation_reason(self) -> str | None: ...


@dataclass(frozen=True)
class NoopRunControl:
    """Fixture/direct-call control: never cancels, optional fixed deadline."""

    _deadline_epoch_ms: int | None = None

    def should_cancel(self) -> bool:
        return False

    def deadline_epoch_ms(self) -> int | None:
        return self._deadline_epoch_ms

    def cancellation_reason(self) -> str | None:
        return None


class RunCancelledError(RuntimeError):
    """Cooperative cancellation won at a semantic boundary."""


class RunDeadlineExceededError(RuntimeError):
    """The absolute run deadline expired at a semantic boundary."""


def control_expired(control: Any | None, now_ms: int | None = None) -> bool:
    """True when the control signals cancellation or the deadline passed.

    Deadline expiry uses the persisted absolute epoch-millisecond deadline:
    ``remaining = max(0, deadline - now)``; boundary equality counts as
    expired.
    """
    if control is None:
        return False
    if control.should_cancel():
        return True
    deadline = control.deadline_epoch_ms()
    if deadline is None:
        return False
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return now_ms >= deadline


def raise_if_control_expired(control: Any | None, now_ms: int | None = None) -> None:
    """Observe the control at a semantic boundary; raise the typed winner."""
    if control is None:
        return
    if control.should_cancel():
        raise RunCancelledError(control.cancellation_reason() or "cancelled")
    deadline = control.deadline_epoch_ms()
    if deadline is None:
        return
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if now_ms >= deadline:
        raise RunDeadlineExceededError(
            f"run deadline exceeded: now={now_ms} >= deadline={deadline}"
        )


__all__ = [
    "NoopRunControl",
    "RunCancelledError",
    "RunControl",
    "RunDeadlineExceededError",
    "control_expired",
    "raise_if_control_expired",
]
