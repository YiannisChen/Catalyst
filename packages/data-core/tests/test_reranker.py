from __future__ import annotations

import threading
import time

from retrieval_model_fixtures import RecordingReranker, make_result, make_results


def test_reranker_candidate_preservation():
    from catalyst_data.retrieval.reranker import rerank

    candidates = make_results(20)
    result = rerank(query="Why did AAPL drop?", candidates=candidates, reranker=RecordingReranker())
    assert {c.chunk_id for c in result.candidates} == {c.chunk_id for c in candidates}
    assert len(result.candidates) == len(candidates)


def test_reranker_top_8_presentation():
    from catalyst_data.retrieval.reranker import rerank

    result = rerank(query="test", candidates=make_results(20), reranker=RecordingReranker())
    assert len(result.results) == 8
    assert [r.reranker_rank for r in result.results] == list(range(1, 9))


def test_reranker_success_relabels_fused_items_as_reranked():
    """Q-011 four-arm gate reads item.mode_served, not only the result-set label.

    Production fusion candidates enter rerank as hybrid/hybrid. A successful
    rerank must relabel every returned item to reranked/reranked with finite
    scores, otherwise the candidate pool rejects the arm as hybrid.
    """
    from catalyst_data.retrieval.reranker import rerank

    candidates = [
        make_result(
            f"fused:{index:02d}",
            mode_requested="hybrid",
            mode_served="hybrid",
            fusion_rank=index,
            fusion_score=1.0 / index,
        )
        for index in range(1, 9)
    ]
    result = rerank(query="test", candidates=candidates, reranker=RecordingReranker())
    assert result.mode_served == "reranked"
    assert result.is_degraded is False
    assert len(result.results) == 8
    for item in result.results:
        assert item.mode_requested == "reranked"
        assert item.mode_served == "reranked"
        assert item.is_degraded is False
        assert item.fallback_reason is None
        assert isinstance(item.reranker_score, float)
        assert item.reranker_rank >= 1


def test_reranker_timeout_falls_back_to_rrf():
    from catalyst_data.retrieval.reranker import rerank

    result = rerank(
        query="test", candidates=make_results(20),
        reranker=RecordingReranker(fail=TimeoutError("fixture timeout")),
    )
    assert result.is_degraded
    assert "reranker_error" in result.degradation_reasons
    assert list(result.results) == list(result.candidates[:8])


def test_reranker_never_retrieves_new_candidates():
    from catalyst_data.retrieval.reranker import rerank

    candidates = [make_result("a"), make_result("b")]
    result = rerank(query="test", candidates=candidates, reranker=RecordingReranker())
    assert {r.chunk_id for r in result.candidates} == {"a", "b"}


def test_missing_reranker_falls_back_to_rrf():
    from catalyst_data.retrieval.reranker import rerank

    result = rerank(query="test", candidates=make_results(20), reranker=None)
    assert result.is_degraded
    assert result.mode_served == "hybrid"
    assert result.fallback_reason == "reranker_failed"
    assert result.degradation_reasons == ("reranker_failed",)


def test_predict_reranker_receives_candidate_content():
    from catalyst_data.retrieval.reranker import rerank

    seen = []

    class PredictReranker:
        def predict(self, pairs):
            seen.extend(pairs)
            return [1.0 for _ in pairs]

    candidates = [make_result("a", content_text="AAPL earnings text")]
    result = rerank(query="earnings", candidates=candidates, reranker=PredictReranker())
    assert not result.is_degraded
    assert seen == [("earnings", "AAPL earnings text")]


def test_slow_reranker_timeout_is_wall_clock_bounded():
    from catalyst_data.retrieval.reranker import rerank

    class SlowReranker:
        def score(self, query, candidates):
            time.sleep(0.2)
            return [1.0 for _ in candidates]

    started = time.perf_counter()
    result = rerank(
        query="test", candidates=make_results(2), reranker=SlowReranker(),
        timeout_seconds=0.01,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 0.15
    assert result.mode_served == "hybrid"
    assert result.fallback_reason == "reranker_timeout"


# ---------------------------------------------------------------------------
# Finding 5: bounded single-flight / circuit for reranker timeouts
# ---------------------------------------------------------------------------


def test_reranker_timeout_is_typed_reason():
    from catalyst_data.retrieval.reranker import RerankerGate, rerank

    class SlowReranker:
        def score(self, query, candidates):
            time.sleep(0.2)
            return [1.0 for _ in candidates]

    gate = RerankerGate()
    result = rerank(
        query="test", candidates=make_results(2), reranker=SlowReranker(),
        timeout_seconds=0.01, gate=gate,
    )
    assert result.is_degraded
    assert result.degradation_reasons == ("reranker_timeout",)
    assert gate.live_worker_count == 1


def test_reranker_busy_fallback_with_single_outstanding_inference():
    from catalyst_data.retrieval.reranker import RerankerGate, rerank

    class SlowReranker:
        def score(self, query, candidates):
            time.sleep(0.2)
            return [1.0 for _ in candidates]

    gate = RerankerGate()
    first = rerank(
        query="test", candidates=make_results(2), reranker=SlowReranker(),
        timeout_seconds=0.01, gate=gate,
    )
    assert first.degradation_reasons == ("reranker_timeout",)

    second = rerank(
        query="test", candidates=make_results(2), reranker=SlowReranker(),
        timeout_seconds=0.01, gate=gate,
    )
    assert second.is_degraded
    assert second.degradation_reasons == ("reranker_busy",)
    assert second.mode_served == "hybrid"
    assert gate.live_worker_count == 1


def test_reranker_recovers_after_background_task_finishes():
    from catalyst_data.retrieval.reranker import RerankerGate, rerank

    class SlowReranker:
        def score(self, query, candidates):
            time.sleep(0.2)
            return [1.0 for _ in candidates]

    gate = RerankerGate()
    rerank(query="test", candidates=make_results(2), reranker=SlowReranker(), timeout_seconds=0.01, gate=gate)
    assert gate.live_worker_count == 1
    time.sleep(0.35)
    assert gate.live_worker_count == 0
    recovered = rerank(
        query="test", candidates=make_results(2), reranker=SlowReranker(),
        timeout_seconds=1.0, gate=gate,
    )
    assert not recovered.is_degraded
    assert recovered.mode_served == "reranked"


def test_reranker_worker_error_is_typed_reason():
    from catalyst_data.retrieval.reranker import RerankerGate, rerank

    class FailingReranker:
        def score(self, query, candidates):
            raise RuntimeError("gpu inference failed")

    gate = RerankerGate()
    result = rerank(
        query="test", candidates=make_results(2), reranker=FailingReranker(),
        timeout_seconds=1.0, gate=gate,
    )
    assert result.is_degraded
    assert result.degradation_reasons == ("reranker_error",)


class _CountingGate:
    def __init__(self):
        from catalyst_data.retrieval.reranker import RerankerGate

        self._inner = RerankerGate()
        self.worker_starts = 0

    def acquire(self, *, target, name, deadline=None):
        worker = self._inner.acquire(target=target, name=name, deadline=deadline)
        if worker is not None:
            self.worker_starts += 1
        return worker

    @property
    def live_worker_count(self):
        return self._inner.live_worker_count


def test_reranker_thread_count_does_not_grow_with_timeout_count():
    from catalyst_data.retrieval.reranker import rerank

    class SlowReranker:
        def score(self, query, candidates):
            time.sleep(0.2)
            return [1.0 for _ in candidates]

    gate = _CountingGate()
    for _ in range(10):
        rerank(
            query="test", candidates=make_results(2), reranker=SlowReranker(),
            timeout_seconds=0.01, gate=gate,
        )
    assert gate.worker_starts == 1
    assert gate.live_worker_count == 1


def test_reranker_gate_acquire_race_is_closed():
    """A second acquire while the first worker runs must be rejected.

    The thread is started under the gate lock, so there is no window where
    an unstarted worker looks free to a concurrent request.
    """
    from catalyst_data.retrieval.reranker import RerankerGate

    gate = RerankerGate()
    first = gate.acquire(target=lambda: time.sleep(0.2), name="w1")
    second = gate.acquire(target=lambda: None, name="w2")
    assert first is not None
    assert second is None
    first.join()
    assert gate.live_worker_count == 0


# ---------------------------------------------------------------------------
# Bounded wait: the gate waits for the live worker inside the caller's own
# budget instead of answering busy on first contact.
# ---------------------------------------------------------------------------


class _ConcurrencyRecordingReranker:
    """Records the maximum number of simultaneously running inferences."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.live = 0
        self.max_live = 0
        self.lock = threading.Lock()

    def score(self, query, candidates):
        with self.lock:
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        try:
            time.sleep(self.seconds)
            return [1.0 for _ in candidates]
        finally:
            with self.lock:
                self.live -= 1


def test_reranker_gate_waits_for_live_worker_within_its_own_budget():
    """A request that arrives while one inference is live waits for it and is
    then served normally inside its own (unchanged) timeout budget."""
    from catalyst_data.retrieval.reranker import RerankerGate, rerank

    class SlowReranker:
        def score(self, query, candidates):
            time.sleep(0.15)
            return [1.0 for _ in candidates]

    gate = RerankerGate()
    first = rerank(
        query="first", candidates=make_results(2), reranker=SlowReranker(),
        timeout_seconds=0.02, gate=gate,
    )
    assert first.degradation_reasons == ("reranker_timeout",)

    second = rerank(
        query="second", candidates=make_results(2), reranker=SlowReranker(),
        timeout_seconds=1.0, gate=gate,
    )
    assert second.is_degraded is False
    assert "reranker_busy" not in second.degradation_reasons
    assert second.mode_served == "reranked"
    assert len(second.results) == 2


def test_reranker_gate_reports_busy_only_after_wait_budget_exhausted():
    """Busy is the last resort: the caller first waits out its whole budget
    and never starts a second live inference."""
    from catalyst_data.retrieval.reranker import rerank

    class SlowReranker:
        def score(self, query, candidates):
            time.sleep(0.2)
            return [1.0 for _ in candidates]

    gate = _CountingGate()
    timed_out = rerank(
        query="first", candidates=make_results(2), reranker=SlowReranker(),
        timeout_seconds=0.01, gate=gate,
    )
    assert timed_out.degradation_reasons == ("reranker_timeout",)

    started = time.monotonic()
    busy = rerank(
        query="second", candidates=make_results(2), reranker=SlowReranker(),
        timeout_seconds=0.08, gate=gate,
    )
    waited = time.monotonic() - started
    assert busy.degradation_reasons == ("reranker_busy",)
    assert busy.mode_served == "hybrid"
    assert waited >= 0.05, f"busy returned without waiting its budget: {waited:.3f}s"
    assert gate.worker_starts == 1
    assert gate.live_worker_count == 1


def test_concurrent_rerank_keeps_single_live_inference_and_serves_both():
    """Two concurrent requests must be served reranked: one live inference at
    a time, the second waiting for it inside the same 2.0s-style budget."""
    from catalyst_data.retrieval.reranker import RerankerGate, rerank

    gate = RerankerGate()
    reranker = _ConcurrencyRecordingReranker(0.12)
    results: list = [None, None]
    barrier = threading.Barrier(2)

    def worker(index: int) -> None:
        barrier.wait(timeout=5)
        results[index] = rerank(
            query=f"q{index}", candidates=make_results(4), reranker=reranker,
            timeout_seconds=2.0, gate=gate,
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert reranker.max_live == 1
    for result in results:
        assert result is not None
        assert result.is_degraded is False
        assert result.degradation_reasons == ()
        assert result.mode_served == "reranked"
        assert len(result.results) == 4
    assert gate.live_worker_count == 0
