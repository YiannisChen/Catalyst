from __future__ import annotations

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

    def acquire(self, *, target, name):
        worker = self._inner.acquire(target=target, name=name)
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
