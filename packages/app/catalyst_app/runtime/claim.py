"""Atomic executor claim and terminal commit (M6-4).

Final Migration TSD §11/§17. ``claim_run`` conditionally transitions
ACCEPTED -> RUNNING with an owner/task token under one transaction; the row
count must equal one before agents executes. Terminal state, required
terminal artifacts, and the terminal event commit together; a late completion
loses the conditional update and is discarded. A stranded RUNNING row is
failed at startup recovery, never resumed mid-graph.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel

from catalyst_app.events import (
    AssuranceCompletedPayload,
    RunEventType,
)
from catalyst_app.lifecycle import RunLifecycleStatus
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import ArtifactPayload, EventRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunClaimer:
    def __init__(self, *, db_path: str | Path, events: EventRepository) -> None:
        self.db_path = Path(db_path)
        self.events = events

    def claim_run(self, run_id: str, *, owner: str, task_token: str) -> bool:
        """Conditionally claim ACCEPTED -> RUNNING; row count must equal one."""
        with open_rw(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE runs
                SET lifecycle_status = ?, owner = ?, task_token = ?, updated_at = ?
                WHERE run_id = ? AND lifecycle_status = ?
                """,
                (
                    RunLifecycleStatus.RUNNING.value,
                    owner,
                    task_token,
                    _utc_now(),
                    run_id,
                    RunLifecycleStatus.ACCEPTED.value,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1

    def current_lifecycle(self, run_id: str) -> RunLifecycleStatus | None:
        with open_rw(self.db_path) as conn:
            row = conn.execute(
                "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return RunLifecycleStatus(row["lifecycle_status"])

    def owner_of(self, run_id: str) -> tuple[str | None, str | None]:
        with open_rw(self.db_path) as conn:
            row = conn.execute(
                "SELECT owner, task_token FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return (None, None)
        return (row["owner"], row["task_token"])

    def terminalize(
        self,
        *,
        run_id: str,
        assurance_payload: AssuranceCompletedPayload,
        terminal_event_type: RunEventType,
        terminal_payload: BaseModel,
        required_artifact_payloads: Sequence[ArtifactPayload] = (),
        lifecycle_update: tuple[RunLifecycleStatus, RunLifecycleStatus],
    ) -> tuple[int, int]:
        """Commit terminal state + required artifacts + terminal event together.

        The lifecycle update is conditioned on the exact prior state, so a
        late completion loses the conditional update and is discarded.
        """
        return self.events.append_terminal(
            run_id=run_id,
            assurance_payload=assurance_payload,
            terminal_event_type=terminal_event_type,
            terminal_payload=terminal_payload,
            artifact_payloads=required_artifact_payloads,
            lifecycle_update=lifecycle_update,
        )

    def fail_stranded(self, run_id: str, *, failure_code: str) -> None:
        """Fail a stranded (RUNNING/CANCEL_REQUESTED) row at recovery."""
        from catalyst_app.events import RunFailedPayload

        lifecycle = self.current_lifecycle(run_id)
        if lifecycle is None:
            raise ValueError(f"run not found: {run_id}")
        self.events.append_terminal(
            run_id=run_id,
            assurance_payload=AssuranceCompletedPayload(
                valid=False, violations=("process_recovery",)
            ),
            terminal_event_type=RunEventType.RUN_FAILED,
            terminal_payload=RunFailedPayload(
                failure_code=failure_code, stage="STARTUP_RECOVERY", retryable=False
            ),
            lifecycle_update=(lifecycle, RunLifecycleStatus.FAILED),
        )


__all__ = ["RunClaimer"]
