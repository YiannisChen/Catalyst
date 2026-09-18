"""Candidate-preserving reranker adapter with explicit fallback and a bounded
single-flight gate (B6-L Finding 5).

A timed-out daemon inference is still running on the GPU, so a new request must
never start another inference while one is outstanding. ``RerankerGate``
enforces at most one live worker. A request that finds a live worker waits for
it inside its own (unchanged) budget before answering: the gate returns ``None``
— the typed ``reranker_busy`` fallback — only once that wait budget is
exhausted, and recovery stays automatic once the background task finishes.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Iterable

from .result import RetrievalResult, RetrievalResultSet

# Poll granularity while waiting for the single live inference to finish.
DEFAULT_GATE_POLL_SECONDS = 0.002


class RerankerGate:
    """Single-flight gate: at most one outstanding reranker inference."""

    def __init__(self, *, poll_interval_seconds: float = DEFAULT_GATE_POLL_SECONDS) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._poll_interval_seconds = poll_interval_seconds

    def _start_if_idle_locked(
        self, target: Callable[[], None], name: str
    ) -> threading.Thread | None:
        """Start one worker under the gate lock, or return ``None`` if busy.

        The thread is started while the lock is held so there is no
        acquire/start race: a concurrent request can never observe an
        unstarted worker as free.
        """
        if self._worker is not None and self._worker.is_alive():
            return None
        worker = threading.Thread(target=target, name=name, daemon=True)
        self._worker = worker
        worker.start()
        return worker

    def acquire(
        self,
        *,
        target: Callable[[], None],
        name: str,
        deadline: float | None = None,
    ) -> threading.Thread | None:
        """Return a running worker, waiting for the live one when allowed.

        ``deadline`` is a ``time.monotonic()`` instant supplied by the caller
        from its existing budget. While a worker is live the gate polls until
        that worker finishes and then starts this request's worker, so a short
        contention window no longer degrades a healthy call. ``None`` is
        returned only when the wait budget is exhausted (or when no deadline
        was supplied, preserving the immediate-busy contract for direct
        callers). At most one inference is ever live.
        """
        with self._lock:
            worker = self._start_if_idle_locked(target, name)
        if worker is not None:
            return worker
        if deadline is None:
            return None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(self._poll_interval_seconds, remaining))
            with self._lock:
                worker = self._start_if_idle_locked(target, name)
            if worker is not None:
                return worker

    @property
    def live_worker_count(self) -> int:
        with self._lock:
            return 1 if self._worker is not None and self._worker.is_alive() else 0


def _scores(
    reranker: object,
    query: str,
    candidates: list[RetrievalResult],
    content_lookup: Callable[[RetrievalResult], str] | None = None,
) -> list[float]:
    if reranker is None:
        raise TypeError("reranker is unavailable")
    if hasattr(reranker, "score"):
        values = reranker.score(query, candidates)
    elif hasattr(reranker, "predict"):
        pairs = []
        for candidate in candidates:
            content = candidate.content_text
            if content is None and content_lookup is not None:
                content = content_lookup(candidate)
            if not isinstance(content, str):
                raise ValueError("reranker candidate content is unavailable")
            pairs.append((query, content))
        values = reranker.predict(pairs)
    elif hasattr(reranker, "compute_score"):
        pairs = []
        for candidate in candidates:
            content = candidate.content_text
            if content is None and content_lookup is not None:
                content = content_lookup(candidate)
            if not isinstance(content, str):
                raise ValueError("reranker candidate content is unavailable")
            pairs.append((query, content))
        values = reranker.compute_score(pairs)
    else:
        raise TypeError("reranker must provide score, predict, or compute_score")
    if isinstance(values, (int, float)):
        values = [values]
    values = list(values)
    if len(values) != len(candidates):
        raise ValueError("reranker returned a different candidate count")
    return [float(value) for value in values]


def _scores_with_timeout_and_gate(
    reranker: object,
    query: str,
    candidates: list[RetrievalResult],
    content_lookup: Callable[[RetrievalResult], str] | None,
    timeout_seconds: float,
    gate: RerankerGate,
) -> tuple[list[float] | None, str | None]:
    """Score under the single-flight gate with typed degradation reasons.

    Returns ``(scores, None)`` on success and ``(None, reason)`` on
    ``reranker_busy`` / ``reranker_timeout`` / ``reranker_error``. ``busy``
    means the single-flight wait budget was exhausted, not that another
    inference merely happened to be live.
    """
    if timeout_seconds is None:
        return _scores(reranker, query, candidates, content_lookup), None
    if timeout_seconds <= 0:
        raise TimeoutError("reranker timeout must be positive")
    completed: queue.Queue[tuple[list[float] | None, BaseException | None]] = queue.Queue(maxsize=1)

    def worker_fn() -> None:
        try:
            completed.put((_scores(reranker, query, candidates, content_lookup), None))
        except BaseException as exc:
            completed.put((None, exc))

    # The caller's existing timeout budget covers the wait for a live worker
    # *and* this request's own inference: the gate may only answer busy once
    # that whole budget is spent, never on first contact.
    deadline = time.monotonic() + timeout_seconds
    worker = gate.acquire(target=worker_fn, name="catalyst-reranker", deadline=deadline)
    if worker is None:
        return None, "reranker_busy"
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        return None, "reranker_timeout"
    scores, error = completed.get_nowait()
    if error is not None:
        return None, "reranker_error"
    if scores is None:
        return None, "reranker_error"
    return scores, None


def _fallback(
    values: list[RetrievalResult],
    display_top_k: int,
    reasons: tuple[str, ...],
) -> RetrievalResultSet:
    ordered = [
        item.model_copy(update={"reranker_rank": index})
        for index, item in enumerate(values, start=1)
    ]
    return RetrievalResultSet(
        candidates=tuple(ordered),
        results=tuple(ordered[:display_top_k]),
        candidate_count=len(ordered),
        mode_requested="reranked",
        mode_served="hybrid",
        is_degraded=True,
        fallback_reason=reasons[0] if reasons else "reranker_failed",
        degradation_reasons=reasons,
    )


def rerank(
    *,
    query: str,
    candidates: Iterable[RetrievalResult],
    reranker: object | None = None,
    timeout_seconds: float = 2.0,
    fallback_ordering: str = "rrf",
    display_top_k: int = 8,
    content_lookup: Callable[[RetrievalResult], str] | None = None,
    gate: RerankerGate | None = None,
) -> RetrievalResultSet:
    values = list(candidates)
    if fallback_ordering != "rrf":
        raise ValueError("only explicit rrf fallback is supported")
    if reranker is None or timeout_seconds <= 0:
        return _fallback(values, display_top_k, ("reranker_failed",))
    active_gate = gate or RerankerGate()
    try:
        scores, degradation = _scores_with_timeout_and_gate(
            reranker, query, values, content_lookup, timeout_seconds, active_gate,
        )
    except Exception:
        degradation = "reranker_error"
        scores = None
    if degradation is not None or scores is None:
        return _fallback(values, display_top_k, (degradation or "reranker_error",))
    ranked = sorted(
        zip(values, scores),
        key=lambda pair: (-pair[1], pair[0].chunk_id),
    )
    ordered = [
        item.model_copy(
            update={
                "reranker_score": score,
                "reranker_rank": rank,
                "mode_requested": "reranked",
                "mode_served": "reranked",
                "is_degraded": False,
                "fallback_reason": None,
            }
        )
        for rank, (item, score) in enumerate(ranked, start=1)
    ]
    return RetrievalResultSet(
        candidates=tuple(ordered),
        results=tuple(ordered[:display_top_k]),
        candidate_count=len(ordered),
        mode_requested="reranked",
        mode_served="reranked",
        is_degraded=False,
        fallback_reason=None,
        degradation_reasons=(),
    )


__all__ = ["RerankerGate", "rerank"]
