"""Race-safe SSE replay + live tail (M6-6, SSE-01).

Final Migration TSD §18. SQLite is authoritative; a run-keyed asyncio
Condition is only a wakeup optimization. The reader re-queries before every
wait, holds the condition lock across the bounded read snapshot and the wait,
and worker-thread publication notifies through ``loop.call_soon_threadsafe``
so no worker thread touches an asyncio primitive directly.

Framing uses the Frozen public names (API-01): ``id: {run_id}:{sequence}``,
``event: {event_type}``, and data is the versioned public envelope with
``sequence``/``emitted_at``. Delivery is at least once; the client deduplicates
by sequence and reconnects from its last applied sequence.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, AsyncIterator, Callable

from catalyst_app.events import (
    ArtifactRef,
    HEARTBEAT_COMMENT,
    PublicRunEvent,
    RunEventType,
    _EVENT_PAYLOAD_TYPES,
    sse_frame,
)
from catalyst_app.lifecycle import RunLifecycleStatus

TERMINAL_EVENT_TYPES = frozenset(
    {
        RunEventType.RUN_COMPLETED,
        RunEventType.RUN_FAILED,
        RunEventType.RUN_CANCELLED,
    }
)

DEFAULT_WAIT_TIMEOUT_SECONDS = 10.0


class InvalidEventCursorError(ValueError):
    pass


class RunNotFoundError(LookupError):
    pass


def parse_last_event_id(run_id: str, last_event_id: str | None) -> int:
    """Parse ``{run_id}:{sequence}``; absence is cursor 0.

    Malformed, negative, and cross-run cursors are rejected (Final TSD §18.1).
    """
    if not last_event_id:
        return 0
    if ":" not in last_event_id:
        raise ValueError("malformed Last-Event-ID (expected {run_id}:{sequence})")
    prefix, _, seq_text = last_event_id.rpartition(":")
    if prefix != run_id:
        raise ValueError("cross-run Last-Event-ID")
    if not seq_text.isdigit():
        raise ValueError("malformed Last-Event-ID sequence")
    seq = int(seq_text)
    if seq < 0:
        raise ValueError("negative Last-Event-ID")
    return seq


@dataclass(frozen=True)
class StreamSnapshot:
    """One consistent read-only SQLite snapshot for the SSE reader."""

    events: tuple[PublicRunEvent, ...]
    lifecycle_status: str | None
    run_exists: bool
    max_seq: int

    @property
    def is_terminal(self) -> bool:
        return self.lifecycle_status in {
            RunLifecycleStatus.COMPLETED.value,
            RunLifecycleStatus.FAILED.value,
            RunLifecycleStatus.CANCELLED.value,
        }

    @staticmethod
    def read(
        db_factory: Callable[..., Any], db_path: str | Path, run_id: str, cursor: int
    ) -> "StreamSnapshot":
        """Read committed events after ``cursor`` plus terminal state in one
        SQLite snapshot; closes the connection before framing/waiting."""
        with db_factory(db_path) as conn:
            conn.execute("BEGIN")
            run = conn.execute(
                "SELECT lifecycle_status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                return StreamSnapshot((), None, False, 0)
            rows = conn.execute(
                """
                SELECT seq, occurred_at, event_type, stage, payload_json, schema_version
                FROM run_events
                WHERE run_id = ? AND seq > ?
                ORDER BY seq ASC
                """,
                (run_id, cursor),
            ).fetchall()
            max_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            artifact_rows: list[Any] = []
            if rows:
                artifact_rows = conn.execute(
                    """
                    SELECT event_seq, artifact_id, artifact_type, payload_hash
                    FROM run_artifacts
                    WHERE run_id = ?
                    ORDER BY event_seq ASC
                    """,
                    (run_id,),
                ).fetchall()
            conn.execute("ROLLBACK")

        artifacts_by_seq: dict[int, list[ArtifactRef]] = defaultdict(list)
        for artifact in artifact_rows:
            artifacts_by_seq[int(artifact["event_seq"])].append(
                ArtifactRef(
                    artifact_id=artifact["artifact_id"],
                    artifact_type=artifact["artifact_type"],
                    schema_version="v1",
                    content_sha256=artifact["payload_hash"],
                )
            )

        events: list[PublicRunEvent] = []
        for row in rows:
            event_type = RunEventType(row["event_type"])
            payload_model = _EVENT_PAYLOAD_TYPES[event_type]
            payload = payload_model.model_validate_json(row["payload_json"])
            events.append(
                PublicRunEvent(
                    schema_version=row["schema_version"],
                    event_id=None,
                    run_id=run_id,
                    sequence=int(row["seq"]),
                    event_type=event_type,
                    emitted_at=row["occurred_at"],
                    stage=row["stage"],
                    payload=payload,
                    artifact_refs=tuple(artifacts_by_seq.get(int(row["seq"]), ())),
                )
            )
        return StreamSnapshot(
            events=tuple(events),
            lifecycle_status=run["lifecycle_status"],
            run_exists=True,
            max_seq=int(max_row[0]),
        )


class ConditionRegistry:
    """Process-local per-run asyncio.Condition registry (Final TSD §18.2).

    One condition per attached/nonterminal run on the FastAPI event loop.
    Worker-thread publication schedules ``notify_all`` via
    ``loop.call_soon_threadsafe``; a worker thread never touches an asyncio
    primitive directly. Removed after terminal publication + last detach;
    lazily recreated after restart.
    """

    def __init__(self, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop
        self._conditions: dict[str, asyncio.Condition] = {}
        self._attached: dict[str, int] = {}
        self._guard = threading.Lock()

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def condition(self, run_id: str) -> asyncio.Condition:
        condition = self._conditions.get(run_id)
        if condition is None:
            condition = asyncio.Condition()
            self._conditions[run_id] = condition
        return condition

    def attach(self, run_id: str) -> None:
        with self._guard:
            self._attached[run_id] = self._attached.get(run_id, 0) + 1
        self.condition(run_id)

    def detach(self, run_id: str, *, terminal: bool) -> None:
        with self._guard:
            count = self._attached.get(run_id, 0)
            if count <= 1:
                self._attached.pop(run_id, None)
                remove = terminal
            else:
                self._attached[run_id] = count - 1
                remove = False
        if remove:
            self._conditions.pop(run_id, None)

    def notify_from_thread(self, run_id: str) -> None:
        """Schedule notify_all on the event loop from a worker thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._schedule_notify, run_id)

    def _schedule_notify(self, run_id: str) -> None:
        asyncio.create_task(self._notify(run_id))

    async def _notify(self, run_id: str) -> None:
        condition = self._conditions.get(run_id)
        if condition is None:
            return
        async with condition:
            condition.notify_all()


async def stream_events(
    run_id: str,
    last_event_id: str | None,
    db_factory: Callable[..., Any],
    condition_registry: ConditionRegistry,
    *,
    db_path: str | Path,
    wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> AsyncIterator[str]:
    """Replay persisted events after the cursor, then tail live events.

    Implements Final TSD §18 exactly: outer loop -> condition lock -> bounded
    read-only snapshot under the lock -> close connection -> frame in order ->
    re-query before any wait; terminal closes; heartbeat on timeout.
    """
    cursor = parse_last_event_id(run_id, last_event_id)
    initial = await asyncio.to_thread(
        StreamSnapshot.read, db_factory, db_path, run_id, cursor
    )
    if not initial.run_exists:
        raise RunNotFoundError(f"run not found: {run_id}")
    if cursor > initial.max_seq:
        raise InvalidEventCursorError(
            f"cursor {cursor} exceeds persisted max {initial.max_seq}"
        )

    condition_registry.attach(run_id)
    snapshot = initial
    try:
        while True:
            condition = condition_registry.condition(run_id)
            timed_out = False
            async with condition:
                snapshot = await asyncio.to_thread(
                    StreamSnapshot.read, db_factory, db_path, run_id, cursor
                )
                if not snapshot.events and not snapshot.is_terminal:
                    try:
                        await asyncio.wait_for(
                            condition.wait(), timeout=wait_timeout_seconds
                        )
                    except asyncio.TimeoutError:
                        timed_out = True

            if snapshot.events:
                terminal = False
                for event in snapshot.events:
                    yield sse_frame(event)
                    cursor = max(cursor, event.sequence)
                    if event.event_type in TERMINAL_EVENT_TYPES:
                        terminal = True
                if terminal:
                    break
                # Immediately re-query before any wait; a notification that
                # happened while framing cannot strand later events.
                continue

            if snapshot.is_terminal:
                break

            if timed_out:
                yield HEARTBEAT_COMMENT
    finally:
        condition_registry.detach(run_id, terminal=snapshot.is_terminal)


__all__ = [
    "ConditionRegistry",
    "DEFAULT_WAIT_TIMEOUT_SECONDS",
    "InvalidEventCursorError",
    "RunNotFoundError",
    "StreamSnapshot",
    "parse_last_event_id",
    "stream_events",
]
