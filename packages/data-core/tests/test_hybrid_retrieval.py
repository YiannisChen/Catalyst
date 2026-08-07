from __future__ import annotations

import numpy as np
import pytest

from retrieval_model_fixtures import MANIFEST_A, RecordingReranker, make_results


class _FakeCursor:
    def __init__(self, present: bool = True):
        self._present = present

    def fetchone(self):
        return (MANIFEST_A,) if self._present else None


class _FakeManifestDb:
    """Minimal db object satisfying the hybrid upfront manifest check."""

    def __init__(self, *, manifest_present: bool = True):
        self._manifest_present = manifest_present

    def execute(self, sql, params=()):
        return _FakeCursor(self._manifest_present)


def _arms():
    from catalyst_data.retrieval.result import RetrievalResultSet

    lexical_values = tuple(make_results(3, "lex"))
    dense_values = tuple(
        make_results(3, "den", mode_requested="dense", mode_served="dense")
    )
    lexical = RetrievalResultSet(
        candidates=lexical_values, results=lexical_values, candidate_count=3,
        mode_requested="lexical", mode_served="fts5", is_degraded=False,
    )
    dense = RetrievalResultSet(
        candidates=dense_values, results=dense_values, candidate_count=3,
        mode_requested="dense", mode_served="dense", is_degraded=False,
    )
    return lexical, dense


def _run(monkeypatch, *, mode, reranker=None, db=None):
    import catalyst_data.retrieval.hybrid as hybrid_module

    lexical, dense = _arms()
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *args, **kwargs: lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", lambda *args, **kwargs: dense)
    return hybrid_module.retrieve_hybrid(
        db if db is not None else _FakeManifestDb(),
        query="AAPL earnings", ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z", mode=mode,
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
        lancedb_table=object(), reranker=reranker,
    )


def test_hybrid_orchestrates_all_arms(monkeypatch):
    result = _run(monkeypatch, mode="hybrid", reranker=RecordingReranker())
    assert result.mode_requested == "hybrid"
    assert result.mode_served == "hybrid"
    assert result.reranker_results is None
    assert result.final_results



def test_production_hybrid_reranker_busy_is_bounded(monkeypatch):
    import time

    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.hybrid import ProductionHybridRetriever

    lexical, dense = _arms()
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *args, **kwargs: lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", lambda *args, **kwargs: dense)

    class SlowReranker:
        def score(self, query, candidates):
            time.sleep(0.2)
            return [1.0 for _ in candidates]

    retriever = ProductionHybridRetriever(
        db=_FakeManifestDb(),
        lancedb_table=object(),
        embedding_fn=lambda query: np.ones(1024, dtype=np.float32),
        reranker=SlowReranker(),
        index_manifest_id="1" * 64,
        reranker_timeout_seconds=0.01,
    )
    first = retriever.retrieve(
        "q", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id=MANIFEST_A,
    )
    second = retriever.retrieve(
        "q", ticker="AAPL", cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id=MANIFEST_A,
    )
    assert len(first) == 6
    assert len(second) == 6
    assert retriever._reranker_gate.live_worker_count == 1

def test_hybrid_mode_reranked(monkeypatch):
    result = _run(monkeypatch, mode="reranked", reranker=RecordingReranker())
    assert result.mode_served == "reranked"
    assert result.reranker_results is not None


def test_reranker_failure_serves_exact_hybrid_order(monkeypatch):
    result = _run(
        monkeypatch, mode="reranked",
        reranker=RecordingReranker(fail=RuntimeError("failed")),
    )
    assert result.mode_served == "hybrid"
    assert result.final_results == result.fusion_results
    assert result.degradation_reasons == ("reranker_error",)


def test_production_hybrid_calls_canonical_arms_with_identical_scope(monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module

    lexical, dense = _arms()
    calls = []

    def fake_lexical(*args, **kwargs):
        calls.append(("lexical", kwargs))
        return lexical

    def fake_dense(*args, **kwargs):
        calls.append(("dense", kwargs))
        return dense

    monkeypatch.setattr(hybrid_module, "retrieve_lexical", fake_lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", fake_dense)

    result = hybrid_module.retrieve_hybrid(
        _FakeManifestDb(), query="AAPL earnings", ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z", mode="hybrid",
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
        lancedb_table=object(), source_classes=("reported_news",),
        evidence_types=("news_v2",),
    )

    assert result.mode_served == "hybrid"
    assert [name for name, _ in calls] == ["lexical", "dense"]
    lexical_kwargs = calls[0][1]
    dense_kwargs = calls[1][1]
    for key in ("ticker", "cutoff", "requested_manifest_id", "source_classes", "evidence_types"):
        assert lexical_kwargs[key] == dense_kwargs[key]
    assert dense_kwargs["index_manifest_id"] == "1" * 64
    assert dense_kwargs["lancedb_table"] is not None


# ---------------------------------------------------------------------------
# Task 4: typed arm-unavailable failure semantics
# ---------------------------------------------------------------------------


def test_invalid_filter_fails_typed_without_degrading(monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalContractError

    def forbidden_lexical(*args, **kwargs):
        raise AssertionError("lexical must not run on invalid filter")

    def forbidden_dense(*args, **kwargs):
        raise AssertionError("dense must not run on invalid filter")

    monkeypatch.setattr(hybrid_module, "retrieve_lexical", forbidden_lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", forbidden_dense)
    with pytest.raises(RetrievalContractError):
        hybrid_module.retrieve_hybrid(
            _FakeManifestDb(), query="AAPL earnings", ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z", mode="hybrid",
            query_embedding=np.ones(1024, dtype=np.float32),
            requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
            lancedb_table=object(), source_classes=("bogus_class",),
        )


def test_manifest_mismatch_fails_typed_without_degrading(monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalContractError

    def forbidden_lexical(*args, **kwargs):
        raise AssertionError("lexical must not run on manifest mismatch")

    def forbidden_dense(*args, **kwargs):
        raise AssertionError("dense must not run on manifest mismatch")

    monkeypatch.setattr(hybrid_module, "retrieve_lexical", forbidden_lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", forbidden_dense)
    with pytest.raises(RetrievalContractError, match="manifest"):
        hybrid_module.retrieve_hybrid(
            _FakeManifestDb(manifest_present=False),
            query="AAPL earnings", ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z", mode="hybrid",
            query_embedding=np.ones(1024, dtype=np.float32),
            requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
            lancedb_table=object(),
        )


def test_invalid_query_embedding_fails_before_arms(monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module

    def forbidden_lexical(*args, **kwargs):
        raise AssertionError("lexical must not run on invalid embedding")

    def forbidden_dense(*args, **kwargs):
        raise AssertionError("dense must not run on invalid embedding")

    monkeypatch.setattr(hybrid_module, "retrieve_lexical", forbidden_lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", forbidden_dense)
    with pytest.raises(ValueError, match="1024"):
        hybrid_module.retrieve_hybrid(
            _FakeManifestDb(), query="AAPL earnings", ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z", mode="hybrid",
            query_embedding=np.ones(512, dtype=np.float32),
            requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
            lancedb_table=object(),
        )


def test_dense_row_identity_corruption_does_not_degrade(monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module

    lexical, _ = _arms()
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *args, **kwargs: lexical)

    class CorruptTable:
        def search(self, query_embedding, *, query_type="vector"):
            return self

        def where(self, predicate, prefilter=True):
            return self

        def limit(self, value):
            return self

        def to_list(self):
            return [{
                "chunk_id": "c:1", "document_id": "doc", "content_text": "x",
                "available_at": "2026-01-01T00:00:00Z",
                "ticker_associations": ["AAPL"], "source_class": "reported_news",
                "chunk_profile_version": "news_v2", "status": "active",
                "eligibility": "eligible",
                "corpus_manifest_id": "9" * 64,  # identity drift
                "index_manifest_id": "1" * 64, "_distance": 0.1,
            }]

    with pytest.raises(ValueError, match="corpus_manifest_id"):
        hybrid_module.retrieve_hybrid(
            _FakeManifestDb(), query="AAPL earnings", ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z", mode="hybrid",
            query_embedding=np.ones(1024, dtype=np.float32),
            requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
            lancedb_table=CorruptTable(),
        )


def test_expected_dense_backend_unavailable_falls_back_to_lexical(monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module

    lexical, _ = _arms()
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *args, **kwargs: lexical)

    class UnavailableTable:
        def search(self, query_embedding, *, query_type="vector"):
            return self

        def where(self, predicate, prefilter=True):
            return self

        def limit(self, value):
            return self

        def to_list(self):
            raise RuntimeError(
                "lance error: LanceError(IO): Object at location /private/tmp/lance/aabb not found"
            )

    result = hybrid_module.retrieve_hybrid(
        _FakeManifestDb(), query="AAPL earnings", ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z", mode="hybrid",
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
        lancedb_table=UnavailableTable(),
    )
    assert result.mode_served == "fts5"
    assert result.final_results
    assert result.degradation_reasons == ("dense_unavailable",)


def test_expected_lexical_unavailable_falls_back_to_dense(monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalArmUnavailableError

    _, dense = _arms()
    monkeypatch.setattr(
        hybrid_module, "retrieve_lexical",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RetrievalArmUnavailableError("lexical", "fts5_unavailable", "SQLite backend unavailable")
        ),
    )
    monkeypatch.setattr(hybrid_module, "retrieve_dense", lambda *args, **kwargs: dense)
    result = hybrid_module.retrieve_hybrid(
        _FakeManifestDb(), query="AAPL earnings", ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z", mode="hybrid",
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
        lancedb_table=object(),
    )
    assert result.mode_served == "dense"
    assert result.final_results
    assert result.degradation_reasons == ("fts5_unavailable",)


def test_both_arms_unavailable_fails(monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalArmUnavailableError

    def unavailable_lexical(*args, **kwargs):
        raise RetrievalArmUnavailableError("lexical", "fts5_unavailable")

    def unavailable_dense(*args, **kwargs):
        raise RetrievalArmUnavailableError("dense", "dense_unavailable")

    monkeypatch.setattr(hybrid_module, "retrieve_lexical", unavailable_lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", unavailable_dense)
    result = hybrid_module.retrieve_hybrid(
        _FakeManifestDb(), query="AAPL earnings", ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z", mode="hybrid",
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
        lancedb_table=object(),
    )
    assert result.mode_served == "failed"
    assert result.degradation_reasons == ("fts5_unavailable", "dense_unavailable")


def test_unexpected_runtime_error_is_not_swallowed(monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module

    monkeypatch.setattr(
        hybrid_module, "retrieve_lexical",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("programmer bug")),
    )
    with pytest.raises(RuntimeError, match="programmer bug"):
        hybrid_module.retrieve_hybrid(
            _FakeManifestDb(), query="AAPL earnings", ticker="AAPL",
            cutoff="2026-01-15T21:00:00Z", mode="hybrid",
            query_embedding=np.ones(1024, dtype=np.float32),
            requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
            lancedb_table=object(),
        )


def test_degradation_reason_contains_no_exception_details(monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module

    lexical, _ = _arms()
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *args, **kwargs: lexical)

    class SecretTable:
        def search(self, query_embedding, *, query_type="vector"):
            return self

        def where(self, predicate, prefilter=True):
            return self

        def limit(self, value):
            return self

        def to_list(self):
            raise RuntimeError(
                "lance error: LanceError(IO): /secret/path/ak_sk_live_1234567890abcdef not found"
            )

    result = hybrid_module.retrieve_hybrid(
        _FakeManifestDb(), query="AAPL earnings", ticker="AAPL",
        cutoff="2026-01-15T21:00:00Z", mode="hybrid",
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=MANIFEST_A, index_manifest_id="1" * 64,
        lancedb_table=SecretTable(),
    )
    assert result.degradation_reasons == ("dense_unavailable",)
    for reason in result.degradation_reasons:
        assert "secret" not in reason.lower()
        assert "/" not in reason
        assert "lance" not in reason.lower()
