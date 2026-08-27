"""Transactional event repository with DB-allocated sequence (M6-5).

Final Migration TSD §17; SSE-01 lock. The repository imports the sealed M2
taxonomy (``catalyst_app.events``) and never defines a second event taxonomy.
Publication follows exactly:

    BEGIN IMMEDIATE
    read max persisted seq for run
    allocate next seq (or contiguous range for a terminal batch)
    insert event row(s)
    insert all required artifact envelope rows referencing allocated seq(s)
    apply lifecycle/result transition when applicable, conditioned on legal prior state
    verify every artifact ref resolves and hashes match
    COMMIT
    invoke the injected non-blocking notifier after commit

The notifier is a wakeup optimization; SQLite remains authoritative. New V1.1
writes go through this repository; legacy TraceWriter rows remain readable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from pydantic import BaseModel

from catalyst_app.events import (
    AssuranceCompletedPayload,
    _EVENT_PAYLOAD_TYPES,
    RunEventType,
)
from catalyst_app.lifecycle import (
    RunLifecycleStatus,
    can_transition,
)
from catalyst_app.persistence.connect import open_rw


class AfterCommitNotifier(Protocol):
    def __call__(self, run_id: str) -> None: ...


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ArtifactPayload:
    """One artifact envelope row to commit with an event (Final TSD §17)."""

    artifact_id: str
    artifact_type: str
    payload: dict[str, Any]
    optional: bool = False
    schema_version: str = "v1"


_TERMINAL_EVENT_TYPES = frozenset(
    {
        RunEventType.RUN_COMPLETED,
        RunEventType.RUN_FAILED,
        RunEventType.RUN_CANCELLED,
    }
)


class RunNotFoundError(RuntimeError):
    pass


class IllegalLifecycleTransitionError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventRepository:
    def __init__(
        self,
        *,
        db_path: str | Path,
        notifier: AfterCommitNotifier | None = None,
        open_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._notifier = notifier
        self._open = open_fn or open_rw

    # -- public API --------------------------------------------------------

    def append(
        self,
        *,
        run_id: str,
        event_type: RunEventType,
        stage: str | None = None,
        payload: BaseModel,
        artifact_payloads: Sequence[ArtifactPayload] = (),
        lifecycle_update: tuple[RunLifecycleStatus, RunLifecycleStatus] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Persist one event (plus artifacts/lifecycle) atomically; returns seq.

        When ``conn`` is supplied the caller owns the transaction and the
        commit/notify; otherwise the repository opens a short transaction,
        commits, and notifies after commit.
        """
        self._validate_append(
            event_type=event_type,
            payload=payload,
            lifecycle_update=lifecycle_update,
        )
        if conn is not None:
            return self._insert_publication(
                conn,
                run_id=run_id,
                events=[(event_type, stage, payload)],
                artifact_payloads=artifact_payloads,
                lifecycle_update=lifecycle_update,
            )[0]

        with self._open(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                seq = self._insert_publication(
                    conn,
                    run_id=run_id,
                    events=[(event_type, stage, payload)],
                    artifact_payloads=artifact_payloads,
                    lifecycle_update=lifecycle_update,
                )[0]
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        self._notify(run_id)
        return seq

    def append_terminal(
        self,
        *,
        run_id: str,
        assurance_payload: AssuranceCompletedPayload,
        terminal_event_type: RunEventType,
        terminal_payload: BaseModel,
        artifact_payloads: Sequence[ArtifactPayload] = (),
        lifecycle_update: tuple[RunLifecycleStatus, RunLifecycleStatus] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[int, int]:
        """Publish assurance.completed + one terminal event in one transaction.

        Allocates two contiguous sequences; the terminal event is the final
        sequence. Returns (assurance_seq, terminal_seq).
        """
        self._validate_append(
            event_type=terminal_event_type,
            payload=terminal_payload,
            lifecycle_update=lifecycle_update,
        )
        if terminal_event_type not in _TERMINAL_EVENT_TYPES:
            raise ValueError(
                f"terminal publication requires a terminal event type, got "
                f"{terminal_event_type.value!r}"
            )
        events = [
            (RunEventType.ASSURANCE_COMPLETED, "ASSURANCE", assurance_payload),
            (terminal_event_type, "TERMINAL", terminal_payload),
        ]

        if conn is not None:
            return self._insert_publication(
                conn,
                run_id=run_id,
                events=events,
                artifact_payloads=artifact_payloads,
                lifecycle_update=lifecycle_update,
            )

        with self._open(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                seqs = self._insert_publication(
                    conn,
                    run_id=run_id,
                    events=events,
                    artifact_payloads=artifact_payloads,
                    lifecycle_update=lifecycle_update,
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        self._notify(run_id)
        return seqs

    def notify(self, run_id: str) -> None:
        """Invoke the after-commit notifier (caller-owned transaction case)."""
        self._notify(run_id)

    # -- internals ---------------------------------------------------------

    def _notify(self, run_id: str) -> None:
        if self._notifier is not None:
            self._notifier(run_id)

    @staticmethod
    def _validate_append(
        *,
        event_type: RunEventType,
        payload: BaseModel,
        lifecycle_update: tuple[RunLifecycleStatus, RunLifecycleStatus] | None,
    ) -> None:
        if not isinstance(event_type, RunEventType):
            raise ValueError(f"unknown event type: {event_type!r}")
        expected = _EVENT_PAYLOAD_TYPES[event_type]
        if not isinstance(payload, expected):
            raise ValueError(
                f"payload type mismatch for {event_type.value!r}: "
                f"expected {expected.__name__}, got {type(payload).__name__}"
            )
        if lifecycle_update is not None:
            from_status, to_status = lifecycle_update
            if not can_transition(from_status, to_status):
                raise IllegalLifecycleTransitionError(
                    f"illegal lifecycle transition {from_status.value} -> {to_status.value}"
                )

    def _insert_publication(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        events: Sequence[tuple[RunEventType, str | None, BaseModel]],
        artifact_payloads: Sequence[ArtifactPayload],
        lifecycle_update: tuple[RunLifecycleStatus, RunLifecycleStatus] | None,
    ) -> tuple[int, ...]:
        run_row = conn.execute(
            "SELECT run_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            raise RunNotFoundError(f"run not found: {run_id}")

        max_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        start_seq = int(max_row[0]) + 1

        occurred_at = _utc_now()
        seqs: list[int] = []
        for offset, (event_type, stage, payload) in enumerate(events):
            seq = start_seq + offset
            seqs.append(seq)
            conn.execute(
                """
                INSERT INTO run_events
                    (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    seq,
                    occurred_at,
                    event_type.value,
                    stage,
                    payload.model_dump_json(),
                    "v1",
                ),
            )

        for artifact in artifact_payloads:
            conn.execute(
                """
                INSERT INTO run_artifacts
                    (artifact_id, run_id, event_seq, artifact_type, payload_hash,
                     payload_json, optional)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    run_id,
                    seqs[0],
                    artifact.artifact_type,
                    payload_sha256(artifact.payload),
                    canonical_json(artifact.payload),
                    1 if artifact.optional else 0,
                ),
            )

        if lifecycle_update is not None:
            from_status, to_status = lifecycle_update
            cursor = conn.execute(
                """
                UPDATE runs
                SET lifecycle_status = ?, updated_at = ?
                WHERE run_id = ? AND lifecycle_status = ?
                """,
                (to_status.value, occurred_at, run_id, from_status.value),
            )
            if cursor.rowcount != 1:
                raise IllegalLifecycleTransitionError(
                    f"run {run_id} is not in {from_status.value}; "
                    f"cannot transition to {to_status.value}"
                )

        # A run.failed terminal event records its typed failure code on the
        # runs row inside the same transaction (Final TSD §17 terminal commit).
        if events and events[-1][0] is RunEventType.RUN_FAILED:
            failed_payload = events[-1][2]
            conn.execute(
                """
                UPDATE runs
                SET failure_code = ?, failure_message = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    getattr(failed_payload, "failure_code", None),
                    getattr(failed_payload, "safe_message", None),
                    occurred_at,
                    run_id,
                ),
            )

        # Verify every artifact ref resolves and its stored hash matches.
        for artifact in artifact_payloads:
            row = conn.execute(
                """
                SELECT payload_hash FROM run_artifacts
                WHERE artifact_id = ? AND run_id = ?
                """,
                (artifact.artifact_id, run_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"artifact ref did not resolve: {artifact.artifact_id}"
                )
            if row["payload_hash"] != payload_sha256(artifact.payload):
                raise RuntimeError(
                    f"artifact hash mismatch: {artifact.artifact_id}"
                )

        return tuple(seqs)


__all__ = [
    "AfterCommitNotifier",
    "ArtifactPayload",
    "EventRepository",
    "IllegalLifecycleTransitionError",
    "RunNotFoundError",
    "canonical_json",
    "payload_sha256",
]
