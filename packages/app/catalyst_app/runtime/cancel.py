"""Cooperative cancellation protocol (M6-8).

Final TSD §11. ``request_cancel``: ACCEPTED cancellation is terminal; RUNNING
persists CANCEL_REQUESTED and signals the process-local control token;
CANCELLED is reached only when work stops or late output is safely discarded
(``acknowledge_cancellation``). A late completion loses the conditional
lifecycle update and is discarded; CANCEL_REQUESTED -> FAILED covers
cancellation-handling integrity failure. No events or artifacts are appended
after a terminal CANCELLED (enforced by EventRepository's terminal guard).
Repeated cancels return an acknowledgement.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from catalyst_app.events import (
    AssuranceCompletedPayload,
    RunCancelledPayload,
    RunEventType,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository, RunNotFoundError
from catalyst_app.runtime.claim import RunClaimer


class CancellationToken:
    """Thread-safe cooperative cancellation signal (Event-based)."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class CancellationTokenRegistry:
    def __init__(self) -> None:
        self._tokens: dict[str, CancellationToken] = {}
        self._lock = threading.Lock()

    def token(self, run_id: str) -> CancellationToken:
        with self._lock:
            token = self._tokens.get(run_id)
            if token is None:
                token = CancellationToken()
                self._tokens[run_id] = token
            return token

    def request(self, run_id: str) -> None:
        self.token(run_id).request()

    def discard(self, run_id: str) -> None:
        with self._lock:
            self._tokens.pop(run_id, None)


class CancelOutcome:
    def __init__(
        self,
        *,
        run_id: str,
        status: RunLifecycleStatus,
        acknowledged: bool,
        failure_code: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.status = status
        self.acknowledged = acknowledged
        self.failure_code = failure_code


class CancellationController:
    def __init__(
        self,
        *,
        db_path: str | Path,
        events: EventRepository,
        claimer: RunClaimer | None = None,
        tokens: CancellationTokenRegistry | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.events = events
        self.claimer = claimer or RunClaimer(db_path=self.db_path, events=events)
        self.tokens = tokens or CancellationTokenRegistry()

    def request_cancel(self, run_id: str) -> CancelOutcome:
        lifecycle = self.claimer.current_lifecycle(run_id)
        if lifecycle is None:
            raise RunNotFoundError(f"run not found: {run_id}")

        if lifecycle is RunLifecycleStatus.ACCEPTED:
            self._terminalize_cancelled(run_id, from_status=lifecycle)
            return CancelOutcome(run_id=run_id, status=RunLifecycleStatus.CANCELLED, acknowledged=True)

        if lifecycle is RunLifecycleStatus.RUNNING:
            with open_rw(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    UPDATE runs
                    SET lifecycle_status = ?, updated_at = ?
                    WHERE run_id = ? AND lifecycle_status = ?
                    """,
                    (
                        RunLifecycleStatus.CANCEL_REQUESTED.value,
                        _utc_now(),
                        run_id,
                        RunLifecycleStatus.RUNNING.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        f"run {run_id} is not RUNNING; cannot request cancellation"
                    )
                conn.commit()
            self.tokens.request(run_id)
            return CancelOutcome(
                run_id=run_id, status=RunLifecycleStatus.CANCEL_REQUESTED, acknowledged=True
            )

        if lifecycle is RunLifecycleStatus.CANCEL_REQUESTED:
            self.tokens.request(run_id)
            return CancelOutcome(
                run_id=run_id, status=RunLifecycleStatus.CANCEL_REQUESTED, acknowledged=True
            )

        # Terminal: idempotent acknowledgement.
        return CancelOutcome(run_id=run_id, status=lifecycle, acknowledged=True)

    def acknowledge_cancellation(self, run_id: str) -> CancelOutcome:
        """Terminalize CANCELLED once work stopped or late output is discarded."""
        lifecycle = self.claimer.current_lifecycle(run_id)
        if lifecycle is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        if lifecycle in (RunLifecycleStatus.RUNNING, RunLifecycleStatus.CANCEL_REQUESTED):
            self._terminalize_cancelled(run_id, from_status=lifecycle)
            return CancelOutcome(
                run_id=run_id, status=RunLifecycleStatus.CANCELLED, acknowledged=True
            )
        if lifecycle is RunLifecycleStatus.CANCELLED:
            return CancelOutcome(
                run_id=run_id, status=RunLifecycleStatus.CANCELLED, acknowledged=True
            )
        raise ValueError(f"run {run_id} is not cancellable from {lifecycle.value}")

    def _terminalize_cancelled(self, run_id: str, *, from_status: RunLifecycleStatus) -> None:
        self.events.append_terminal(
            run_id=run_id,
            assurance_payload=AssuranceCompletedPayload(
                valid=False, violations=("cancelled",)
            ),
            terminal_event_type=RunEventType.RUN_CANCELLED,
            terminal_payload=RunCancelledPayload(
                cancellation_reason="user_cancelled",
                stage=None,
                acknowledgement_mode="cooperative",
                provisional_output_invalidated=True,
            ),
            lifecycle_update=(from_status, RunLifecycleStatus.CANCELLED),
        )


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CancelOutcome",
    "CancellationToken",
    "CancellationTokenRegistry",
    "CancellationController",
]
