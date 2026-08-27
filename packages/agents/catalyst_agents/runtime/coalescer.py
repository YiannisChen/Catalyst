"""Bounded streaming coalescer (M5-7).

Final TSD §19: provider streaming deltas are buffered and flushed at 50 ms or
2,048 UTF-8 characters (whichever first) and at provider completion. It never
writes per-token rows and never slices a completed answer to simulate
streaming. Each persisted event records stream ID, delta ordinal, exact
accepted text, and cumulative accepted character/byte count. The provisional
Answer equals the exact ordered concatenation of persisted deltas.
"""
from __future__ import annotations

import time
from typing import Callable

from catalyst_agents.runtime.delta_sink import DeltaSink

DEFAULT_FLUSH_INTERVAL_MS = 50
DEFAULT_FLUSH_CHARS = 2048


class Coalescer:
    """Buffers accepted provider deltas and flushes bounded events to a sink."""

    def __init__(
        self,
        *,
        flush_interval_ms: int = DEFAULT_FLUSH_INTERVAL_MS,
        flush_chars: int = DEFAULT_FLUSH_CHARS,
        sink: DeltaSink,
        stream_id: str,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if flush_interval_ms < 1:
            raise ValueError("flush_interval_ms must be positive")
        if flush_chars < 1:
            raise ValueError("flush_chars must be positive")
        self.flush_interval_ms = flush_interval_ms
        self.flush_chars = flush_chars
        self.sink = sink
        self.stream_id = stream_id
        self._monotonic_fn = monotonic_fn

        self._buffer: list[str] = []
        self._buffer_chars = 0
        self._ordinal = 0
        self._total_chars = 0
        self._total_bytes = 0
        self._last_flush = self._monotonic_fn()
        self._completed = False

    def accept(self, text: str) -> None:
        """Accept one provider delta fragment into the buffer."""
        if not text:
            return
        if self._completed:
            raise RuntimeError("coalescer already completed")
        self._buffer.append(text)
        self._buffer_chars += len(text)
        elapsed_ms = (self._monotonic_fn() - self._last_flush) * 1000
        if self._buffer_chars >= self.flush_chars or elapsed_ms >= self.flush_interval_ms:
            self.flush()

    def flush(self) -> None:
        """Flush the buffered text as exactly one persisted delta event."""
        if not self._buffer:
            self._last_flush = self._monotonic_fn()
            return
        text = "".join(self._buffer)
        self._total_chars += len(text)
        self._total_bytes += len(text.encode("utf-8"))
        self.sink.commit_delta(
            stream_id=self.stream_id,
            ordinal=self._ordinal,
            text=text,
            cumulative_chars=self._total_chars,
            cumulative_bytes=self._total_bytes,
        )
        self._ordinal += 1
        self._buffer = []
        self._buffer_chars = 0
        self._last_flush = self._monotonic_fn()

    def complete(self) -> None:
        """Flush the remainder and commit the provisional Answer artifact.

        The Answer is the exact ordered concatenation of persisted deltas; it
        is never sliced or fabricated to simulate streaming.
        """
        if self._completed:
            return
        self.flush()
        answer_text = "".join(delta.text for delta in self.sink.deltas())
        import hashlib

        self.sink.commit_answer(
            answer_id=f"answer:{self.stream_id}",
            stream_id=self.stream_id,
            text=answer_text,
            text_sha256=hashlib.sha256(answer_text.encode("utf-8")).hexdigest(),
            completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._completed = True


__all__ = ["Coalescer", "DEFAULT_FLUSH_CHARS", "DEFAULT_FLUSH_INTERVAL_MS"]
