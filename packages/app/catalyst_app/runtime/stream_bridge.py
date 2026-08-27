"""Writer delta persistence bridge (M6-7).

Final TSD §19. ``StreamBridge`` is the production ``DeltaSink``: it consumes
accepted coalesced Writer chunks produced by the single agents-owned
``Coalescer`` (50 ms / 2,048 UTF-8 chars) and persists them as ``answer.delta``
events through the EventRepository. It never re-implements buffering or
coalescing, never writes per-token rows, and never slices a completed answer
to simulate streaming. The provisional Answer committed with
``answer.completed`` must equal the exact ordered concatenation of accepted
persisted deltas; a mismatch is ``STREAM_PERSISTENCE_FAILURE`` and invalidates
the output. SQLite failure is bounded by the per-run accepted-delta list and
fails closed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from catalyst_app.events import (
    AnswerCompletedPayload,
    AnswerDeltaPayload,
    RunEventType,
)
from catalyst_app.persistence.events import ArtifactPayload, EventRepository
from catalyst_agents.runtime.delta_sink import AnswerArtifact, DeltaEvent


class StreamPersistenceFailure(RuntimeError):
    pass


class StreamBridge:
    """App-owned production DeltaSink over the transactional event repository."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        events: EventRepository | None = None,
        run_id: str,
    ) -> None:
        self.db_path = Path(db_path)
        self.run_id = run_id
        self._events = events or EventRepository(db_path=self.db_path)
        self._deltas: list[DeltaEvent] = []
        self._answer: AnswerArtifact | None = None
        self._envelope: dict[str, Any] | None = None
        self._failed: str | None = None
        self._next_ordinal = 0

    # -- DeltaSink protocol ------------------------------------------------

    def commit_delta(
        self,
        *,
        stream_id: str,
        ordinal: int,
        text: str,
        cumulative_chars: int,
        cumulative_bytes: int,
    ) -> None:
        self._ensure_open()
        if ordinal != self._next_ordinal:
            self.fail("STREAM_PERSISTENCE_FAILURE")
            raise StreamPersistenceFailure(
                f"delta ordinal gap: expected {self._next_ordinal}, got {ordinal}"
            )
        expected_chars = sum(len(d.text) for d in self._deltas) + len(text)
        if cumulative_chars != expected_chars:
            self.fail("STREAM_PERSISTENCE_FAILURE")
            raise StreamPersistenceFailure(
                "cumulative character count does not match accepted deltas"
            )
        try:
            self._events.append(
                run_id=self.run_id,
                event_type=RunEventType.ANSWER_DELTA,
                stage="STREAMING_ANSWER",
                payload=AnswerDeltaPayload(
                    writer_stream_id=stream_id,
                    delta_text=text,
                    delta_ordinal=ordinal + 1,  # public ordinals are 1-based
                    cumulative_character_count=cumulative_chars,
                    provisional=True,
                ),
            )
        except BaseException as exc:
            self.fail("STREAM_PERSISTENCE_FAILURE")
            raise StreamPersistenceFailure(
                f"answer.delta persistence failed: {exc}"
            ) from exc
        self._deltas.append(
            DeltaEvent(
                stream_id=stream_id,
                ordinal=ordinal,
                text=text,
                cumulative_chars=cumulative_chars,
                cumulative_bytes=cumulative_bytes,
            )
        )
        self._next_ordinal = ordinal + 1

    def commit_answer(
        self,
        *,
        answer_id: str,
        stream_id: str,
        text: str,
        text_sha256: str,
        completed_at: str,
    ) -> None:
        self._ensure_open()
        persisted = "".join(d.text for d in self._deltas)
        if persisted != text:
            self.fail("STREAM_PERSISTENCE_FAILURE")
            raise StreamPersistenceFailure(
                "provisional answer text does not equal the exact ordered "
                "concatenation of accepted persisted deltas"
            )
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
            self.fail("STREAM_PERSISTENCE_FAILURE")
            raise StreamPersistenceFailure("answer text hash mismatch")
        try:
            self._events.append(
                run_id=self.run_id,
                event_type=RunEventType.ANSWER_COMPLETED,
                stage="STREAMING_ANSWER",
                payload=AnswerCompletedPayload(
                    writer_stream_id=stream_id,
                    final_text_hash=text_sha256,
                    provisional=True,
                ),
                artifact_payloads=[
                    ArtifactPayload(
                        artifact_id=answer_id,
                        artifact_type="answer",
                        payload={
                            "answer_id": answer_id,
                            "stream_id": stream_id,
                            "text": text,
                            "text_sha256": text_sha256,
                            "completed_at": completed_at,
                        },
                    )
                ],
            )
        except BaseException as exc:
            self.fail("STREAM_PERSISTENCE_FAILURE")
            raise StreamPersistenceFailure(
                f"answer.completed persistence failed: {exc}"
            ) from exc
        self._answer = AnswerArtifact(
            answer_id=answer_id,
            stream_id=stream_id,
            text=text,
            text_sha256=text_sha256,
            completed_at=completed_at,
        )

    def deltas(self) -> list[DeltaEvent]:
        return list(self._deltas)

    def answer(self) -> AnswerArtifact | None:
        return self._answer

    def reset(self) -> None:
        self._deltas = []
        self._answer = None
        self._envelope = None
        self._failed = None
        self._next_ordinal = 0

    def commit_assured_envelope(
        self,
        *,
        answer_id: str,
        final_status: str,
        attribution_type: str,
        checks: tuple[str, ...],
        completed_at: str,
    ) -> None:
        """Record the assured terminal envelope.

        The durable terminal batch (assurance.completed + run.completed in one
        transaction) is published by the app terminalize path (M6-9), which
        owns the atomic terminal commit.
        """
        self._ensure_open()
        self._envelope = {
            "answer_id": answer_id,
            "final_status": final_status,
            "attribution_type": attribution_type,
            "checks": list(checks),
            "completed_at": completed_at,
            "assured": True,
        }

    def assured_envelope(self) -> dict[str, Any] | None:
        return self._envelope

    def fail(self, code: str) -> None:
        """Fail closed: invalidate provisional output and stop accepting."""
        self._failed = code
        self._answer = None
        self._envelope = None

    # -- internals ---------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._failed is not None:
            raise StreamPersistenceFailure(f"sink failed closed: {self._failed}")


__all__ = ["StreamBridge", "StreamPersistenceFailure"]
