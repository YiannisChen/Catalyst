"""V1.1 delta/answer persistence protocol seam (M5-7).

Final TSD §19; M5/M6 boundary §3: agents owns the ``DeltaSink`` protocol and
the in-memory FAST sink; the production SQLite implementation is M6 app-owned.
"Committed" means committed through the injected sink. The provisional Answer
committed at answer.completed must equal the exact ordered concatenation of
accepted persisted deltas.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DeltaEvent:
    """One persisted stream delta (never per-token rows)."""

    stream_id: str
    ordinal: int
    text: str
    cumulative_chars: int
    cumulative_bytes: int
    #: Frozen-A5 (WP-C C3c): ``provider`` for provider-stream text,
    #: ``writer_assembly`` for code-owned structural segments.
    origin: str = "provider"


#: The two delta origins (Frozen-A5).
PROVIDER_ORIGIN = "provider"
WRITER_ASSEMBLY_ORIGIN = "writer_assembly"
DELTA_ORIGINS = (PROVIDER_ORIGIN, WRITER_ASSEMBLY_ORIGIN)


@dataclass(frozen=True)
class AnswerArtifact:
    """The provisional Answer artifact committed at answer.completed."""

    answer_id: str
    stream_id: str
    text: str
    text_sha256: str
    completed_at: str


class DeltaSink(Protocol):
    """Agents-owned persistence seam; M6 app supplies the production envelope."""

    def commit_delta(
        self,
        *,
        stream_id: str,
        ordinal: int,
        text: str,
        cumulative_chars: int,
        cumulative_bytes: int,
        origin: str = "provider",
    ) -> None:
        ...

    def commit_answer(
        self,
        *,
        answer_id: str,
        stream_id: str,
        text: str,
        text_sha256: str,
        completed_at: str,
    ) -> None:
        ...

    def deltas(self) -> list[DeltaEvent]:
        ...

    def answer(self) -> AnswerArtifact | None:
        ...

    def reset(self) -> None:
        ...

    def commit_assured_envelope(
        self,
        *,
        answer_id: str,
        final_status: str,
        attribution_type: str,
        checks: tuple[str, ...],
        completed_at: str,
    ) -> None:
        """Persist the assured terminal result envelope (M6 supplies the durable envelope)."""

    def assured_envelope(self) -> dict | None:
        ...

    def fail(self, code: str) -> None:
        """Bound the sink failure: mark failed so the run fails closed."""


class InMemoryDeltaSink:
    """FAST test sink; never a production artifact store."""

    def __init__(self) -> None:
        self._deltas: list[DeltaEvent] = []
        self._answer: AnswerArtifact | None = None
        self._envelope: dict | None = None
        self._failed: str | None = None

    def commit_delta(
        self,
        *,
        stream_id: str,
        ordinal: int,
        text: str,
        cumulative_chars: int,
        cumulative_bytes: int,
        origin: str = "provider",
    ) -> None:
        if self._failed:
            raise RuntimeError(f"sink failed closed: {self._failed}")
        if origin not in DELTA_ORIGINS:
            raise ValueError(f"unknown delta origin {origin!r}")
        self._deltas.append(
            DeltaEvent(
                stream_id=stream_id,
                ordinal=ordinal,
                text=text,
                cumulative_chars=cumulative_chars,
                cumulative_bytes=cumulative_bytes,
                origin=origin,
            )
        )

    def commit_answer(
        self,
        *,
        answer_id: str,
        stream_id: str,
        text: str,
        text_sha256: str,
        completed_at: str,
    ) -> None:
        if self._failed:
            raise RuntimeError(f"sink failed closed: {self._failed}")
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
        self._envelope: dict | None = None
        self._failed = None

    def commit_assured_envelope(
        self,
        *,
        answer_id: str,
        final_status: str,
        attribution_type: str,
        checks: tuple[str, ...],
        completed_at: str,
    ) -> None:
        if self._failed:
            raise RuntimeError(f"sink failed closed: {self._failed}")
        self._envelope = {
            "answer_id": answer_id,
            "final_status": final_status,
            "attribution_type": attribution_type,
            "checks": list(checks),
            "completed_at": completed_at,
            "assured": True,
        }

    def assured_envelope(self) -> dict | None:
        return self._envelope

    def fail(self, code: str) -> None:
        self._failed = code
        self._answer = None
        self._envelope = None


__all__ = [
    "AnswerArtifact",
    "DELTA_ORIGINS",
    "DeltaEvent",
    "DeltaSink",
    "InMemoryDeltaSink",
    "PROVIDER_ORIGIN",
    "WRITER_ASSEMBLY_ORIGIN",
]
