"""Blocker C: retrieval observations must come from the real retriever.

The V1.1 hit contract deliberately carries nullable per-stage scores/ranks, and
the production data-core converter writes the pre-rerank stages (lexical,
dense, fusion) as ``None`` because the served set is the reranked arm only. An
adapter that reads candidate order / rank displacement / served arms out of the
converted hits therefore reports an empty candidate inventory and an empty
"served arms" set while still claiming a served reranked ranking.

These tests drive the REAL ``ProductionHybridRetriever`` with the REAL
data-core V1 converter (only the two external arms and the canonical-metadata
lookup are stubbed, as the sealed data-core tests do) and require the observed
facts to survive the lossy conversion.
"""
from __future__ import annotations

import numpy as np
import pytest

from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval.hybrid import HybridRetrievalResult, observe_hybrid_result
from catalyst_data.retrieval.result import RetrievalFilters, RetrievalResult, RetrievalResultSet
from catalyst_data.retrieval.v1_result import RetrievalResultSet as V1RetrievalResultSet

CUTOFF = "2026-01-15T21:00:00Z"
INDEX_MANIFEST_ID = "1" * 64
MANIFEST_A = "a" * 64


class RecordingReranker:
    """Deterministic reranker: keeps the declared order (or the input order)."""

    def __init__(self, order: list[str] | None = None):
        self.order = order
        self.calls = 0

    def score(self, query: str, candidates: list) -> list[float]:
        self.calls += 1
        order = self.order or [candidate.chunk_id for candidate in candidates]
        ranks = {chunk_id: len(order) - index for index, chunk_id in enumerate(order)}
        return [float(ranks.get(candidate.chunk_id, 0)) for candidate in candidates]


def _make_result(chunk_id: str, **kwargs) -> RetrievalResult:
    filters = RetrievalFilters(
        ticker=kwargs.pop("ticker", "AAPL"),
        requested_manifest_id=kwargs.pop("requested_manifest_id", MANIFEST_A),
        cutoff=kwargs.pop("cutoff", CUTOFF),
    )
    return RetrievalResult(
        chunk_id=chunk_id,
        document_id=kwargs.pop("document_id", "doc"),
        available_at=kwargs.pop("available_at", "2026-01-01T00:00:00Z"),
        cutoff=filters.cutoff,
        content_text=kwargs.pop("content_text", "fixture body"),
        filters_applied=filters,
        source_class=kwargs.pop("source_class", "reported_news"),
        corpus_manifest_id=filters.requested_manifest_id,
        index_manifest_id=kwargs.pop("index_manifest_id", INDEX_MANIFEST_ID),
        mode_requested=kwargs.pop("mode_requested", "lexical"),
        mode_served=kwargs.pop("mode_served", "fts5"),
        is_degraded=False,
        timing_ms=1.0,
        lexical_raw_score=kwargs.pop("lexical_raw_score", None),
        lexical_rank=kwargs.pop("lexical_rank", None),
        dense_score=kwargs.pop("dense_score", None),
        dense_rank=kwargs.pop("dense_rank", None),
        fusion_score=kwargs.pop("fusion_score", None),
        fusion_rank=kwargs.pop("fusion_rank", None),
        reranker_score=kwargs.pop("reranker_score", None),
        reranker_rank=kwargs.pop("reranker_rank", None),
        **kwargs,
    )


class _FakeManifestDb:
    """Minimal db object satisfying the hybrid upfront manifest check."""

    def execute(self, sql, params=()):
        class _Cursor:
            def fetchone(self):
                return (MANIFEST_A,)

        return _Cursor()


def _temporal_identity() -> TemporalIdentity:
    from datetime import datetime, timezone

    def utc(iso):
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(
            tzinfo=timezone.utc
        )

    return TemporalIdentity(
        session_date="2026-01-15",
        market_timezone="America/New_York",
        session_open_at=utc("2026-01-15T14:30:00Z"),
        session_close_at=utc(CUTOFF),
        information_window_start_at=utc("2026-01-14T21:00:00Z"),
        cutoff_at=utc(CUTOFF),
    )


def _runtime_identity() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id=MANIFEST_A,
        fts_index_version="build:fts",
        dense_index_version=INDEX_MANIFEST_ID,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _canonical_meta(chunk_id: str) -> dict[str, object]:
    return {
        "canonical_asset_id": f"asset:{chunk_id}",
        "content_version_id": f"version:{chunk_id}",
        "corpus_document_id": f"doc:{chunk_id}",
        "document_id": f"doc:{chunk_id}",
        "section_key": "body",
        "chunk_ordinal": 1,
        "content_hash": "c" * 64,
        "source_class": "reported_news",
        "content_state": "FULL_TEXT",
        "eligible_at": "2026-01-15T00:00:00Z",
        "dedup_cluster_id": None,
        "independence_group_id": "g:1",
        "parse_quality": "not_applicable",
        "provider": "polygon",
        "publisher": None,
        "canonical_url": "https://example.test/a",
        "asset_type": "NEWS",
        "temporal_precision": "publication_time",
        "serving_status": "body_candidate",
        "ticker_scope": ("AAPL",),
    }


def _lexical_arm() -> RetrievalResultSet:
    values = tuple(
        _make_result(
            f"lex:{index:02d}",
            lexical_raw_score=float(-(index + 1)),
            lexical_rank=index + 1,
        )
        for index in (0, 1, 2)
    )
    return RetrievalResultSet(
        candidates=values, results=values, candidate_count=3,
        mode_requested="lexical", mode_served="fts5", is_degraded=False,
    )


def _dense_arm() -> RetrievalResultSet:
    values = tuple(
        _make_result(
            f"den:{index + 2:02d}",
            dense_score=float(1 - index),
            dense_rank=index + 1,
            mode_requested="dense",
            mode_served="dense",
        )
        for index in (0, 1, 2)
    )
    return RetrievalResultSet(
        candidates=values, results=values, candidate_count=3,
        mode_requested="dense", mode_served="dense", is_degraded=False,
    )


def _large_lexical_arm() -> RetrievalResultSet:
    values = tuple(
        _make_result(
            f"lex20:{index:02d}",
            lexical_raw_score=float(-(index + 1)),
            lexical_rank=index + 1,
        )
        for index in range(20)
    )
    return RetrievalResultSet(
        candidates=values, results=values, candidate_count=20,
        mode_requested="lexical", mode_served="fts5", is_degraded=False,
    )


def _large_dense_arm() -> RetrievalResultSet:
    values = tuple(
        _make_result(
            f"den20:{index:02d}",
            dense_score=float(1 - index / 20),
            dense_rank=index + 1,
            mode_requested="dense",
            mode_served="dense",
        )
        for index in range(20)
    )
    return RetrievalResultSet(
        candidates=values, results=values, candidate_count=20,
        mode_requested="dense", mode_served="dense", is_degraded=False,
    )
def _empty_arm(*, mode_requested: str, mode_served: str) -> RetrievalResultSet:
    return RetrievalResultSet(
        candidates=(),
        results=(),
        candidate_count=0,
        mode_requested=mode_requested,
        mode_served=mode_served,
        is_degraded=False,
    )


def _real_retriever(monkeypatch, *, reranker, empty_arms=False):
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.hybrid import ProductionHybridRetriever

    lexical = (
        _empty_arm(mode_requested="lexical", mode_served="fts5")
        if empty_arms else _lexical_arm()
    )
    dense = (
        _empty_arm(mode_requested="dense", mode_served="dense")
        if empty_arms else _dense_arm()
    )
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *a, **k: lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", lambda *a, **k: dense)
    monkeypatch.setattr(
        hybrid_module, "_lookup_canonical_chunk",
        lambda db, chunk_id, *, requested_manifest_id: _canonical_meta(chunk_id),
    )
    return ProductionHybridRetriever(
        db=_FakeManifestDb(),
        lancedb_table=object(),
        embedding_fn=lambda q: np.ones(1024, dtype=np.float32),
        reranker=reranker,
        index_manifest_id=INDEX_MANIFEST_ID,
        data_runtime_identity=_runtime_identity(),
    )


def test_real_production_retriever_serves_the_v1_contract(monkeypatch):
    """Sanity: the real retriever serves the lossy V1.1 reranked set."""
    retriever = _real_retriever(monkeypatch, reranker=RecordingReranker())
    result = retriever.retrieve(
        "Why did AAPL move?", ticker="AAPL", cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_A,
        temporal_identity=_temporal_identity(),
    )
    assert isinstance(result, V1RetrievalResultSet)
    assert result.hits
    # The V1 conversion is lossy for the pre-rerank stages: that is exactly why
    # the observation must be captured before the conversion.
    for hit in result.hits:
        ranks = {entry.stage: entry.value for entry in hit.ranks}
        assert ranks["lexical"] is None
        assert ranks["dense"] is None
        assert ranks["fusion"] is None
        assert ranks["reranked"] is not None


def test_real_retriever_observation_survives_the_lossy_v1_conversion(monkeypatch):
    """Observed candidates, final ranking, arms and latency are authoritative."""
    retriever = _real_retriever(monkeypatch, reranker=RecordingReranker())
    adapter = AgentRetrieverAdapter(retriever)
    evidence, observation = adapter.retrieve_with_observations(
        "Why did AAPL move?", ticker="AAPL", cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_A,
        temporal_identity=_temporal_identity(),
        top_k=3,
        candidate_depth=20,
    )
    assert evidence, "the real retriever must serve evidence"
    # The pre-rerank candidate inventory is non-empty and ordered.
    assert observation.ordered_candidate_evidence_ids
    # The served final ranking is non-empty, ordered and distinct.
    assert observation.ordered_final_ranked_evidence_ids
    assert len(set(observation.ordered_final_ranked_evidence_ids)) == len(
        observation.ordered_final_ranked_evidence_ids
    )
    # Only the stages the run actually executed/served are reported.
    assert set(observation.arm_names) == {"lexical", "dense", "fusion", "reranked"}
    assert observation.served_mode == "reranked"
    assert observation.requested_mode == "reranked"
    assert observation.measured_latency_ms is not None


def test_real_retriever_reports_rank_changes_from_real_pre_and_post_ranks(monkeypatch):
    """Rank displacement is computed only where both real ranks exist."""
    # The reranker reverses the fused order of 'lex:00'/'den:00', so at least
    # one hit really moves.
    order = ["lex:02", "den:02", "lex:01", "den:01", "lex:00", "den:00"]
    retriever = _real_retriever(monkeypatch, reranker=RecordingReranker(order=order))
    adapter = AgentRetrieverAdapter(retriever)
    _, observation = adapter.retrieve_with_observations(
        "Why did AAPL move?", ticker="AAPL", cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_A,
        temporal_identity=_temporal_identity(),
        top_k=6,
        candidate_depth=20,
    )
    assert observation.rank_changes, "a real rerank must record real displacement"
    assert all(delta != 0 for delta in observation.rank_changes.values())


def test_real_retriever_preserves_empty_stage_truth_and_degradation(monkeypatch):
    """Empty successful and degraded runs preserve control-flow stage truth."""
    successful = _real_retriever(
        monkeypatch, reranker=RecordingReranker(), empty_arms=True
    )
    observed_success = successful.retrieve_with_observation(
        "Why did AAPL move?", ticker="AAPL", cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_A,
        temporal_identity=_temporal_identity(),
        top_k=8,
        candidate_depth=20,
    )
    success_observation = observed_success.observation
    assert observed_success.result_set is not None
    assert observed_success.result_set.hits == ()
    assert success_observation.ordered_candidate_evidence_ids == ()
    assert success_observation.ordered_final_ranked_evidence_ids == ()
    assert set(success_observation.arm_names) == {
        "lexical", "dense", "fusion", "reranked"
    }
    assert success_observation.degradation_reasons == ()

    degraded = _real_retriever(monkeypatch, reranker=None, empty_arms=True)
    observed_degraded = degraded.retrieve_with_observation(
        "Why did AAPL move?", ticker="AAPL", cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_A,
        temporal_identity=_temporal_identity(),
        top_k=8,
        candidate_depth=20,
    )
    degraded_observation = observed_degraded.observation
    assert observed_degraded.result_set is None
    assert degraded_observation.ordered_candidate_evidence_ids == ()
    assert degraded_observation.ordered_final_ranked_evidence_ids == ()
    assert set(degraded_observation.arm_names) == {"lexical", "dense", "fusion"}
    assert degraded_observation.degradation_reasons == ("reranker_failed",)


def test_observation_keeps_all_20_fusion_candidates_when_only_8_are_final():
    """The candidate inventory is the full fusion input, not the display top-8."""
    fusion = tuple(
        _make_result(f"e{index:02d}", fusion_rank=index, fusion_score=1.0 / index)
        for index in range(1, 21)
    )
    final = tuple(
        item.model_copy(update={"reranker_rank": rank})
        for rank, item in enumerate(reversed(fusion[:8]), start=1)
    )
    reranked = RetrievalResultSet(
        candidates=final + fusion[8:],
        results=final,
        candidate_count=20,
        mode_requested="reranked",
        mode_served="reranked",
        is_degraded=False,
    )
    observed = observe_hybrid_result(
        HybridRetrievalResult(
            mode_requested="reranked",
            mode_served="reranked",
            fusion_results=fusion,
            reranker_results=reranked,
            final_results=final,
        )
    )

    assert observed.ordered_candidate_evidence_ids == tuple(
        f"e{index:02d}" for index in range(1, 21)
    )
    assert len(observed.ordered_final_ranked_evidence_ids) == 8


def test_real_production_retriever_and_adapter_execute_20_to_8_end_to_end(monkeypatch):
    """The 20-candidate -> 8-display contract uses both real production calls."""
    import catalyst_data.retrieval.hybrid as hybrid_module

    lexical_calls: list[dict] = []
    dense_calls: list[dict] = []
    lexical = _large_lexical_arm()
    dense = _large_dense_arm()

    def lexical_stub(_db, _query, **kwargs):
        lexical_calls.append(kwargs)
        return lexical

    def dense_stub(_db, _query_embedding, **kwargs):
        dense_calls.append(kwargs)
        return dense

    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lexical_stub)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", dense_stub)
    monkeypatch.setattr(
        hybrid_module, "_lookup_canonical_chunk",
        lambda db, chunk_id, *, requested_manifest_id: _canonical_meta(chunk_id),
    )
    from catalyst_data.retrieval.hybrid import ProductionHybridRetriever

    retriever = ProductionHybridRetriever(
        db=_FakeManifestDb(),
        lancedb_table=object(),
        embedding_fn=lambda q: np.ones(1024, dtype=np.float32),
        reranker=RecordingReranker(),
        index_manifest_id=INDEX_MANIFEST_ID,
        data_runtime_identity=_runtime_identity(),
    )
    adapter = AgentRetrieverAdapter(retriever)
    evidence, observation = adapter.retrieve_with_observations(
        "Why did AAPL move?", ticker="AAPL", cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_A,
        temporal_identity=_temporal_identity(), top_k=8, candidate_depth=20,
    )

    assert len(evidence) == 8
    assert len(observation.ordered_candidate_evidence_ids) == 20
    assert len(observation.ordered_final_ranked_evidence_ids) == 8
    assert set(observation.arm_names) == {"lexical", "dense", "fusion", "reranked"}
    assert lexical_calls and dense_calls
    assert lexical_calls[0]["candidate_depth"] == 20
    assert lexical_calls[0]["top_k"] == 20
    assert dense_calls[0]["top_k"] == 20


def test_real_retriever_degrades_with_the_reason_preserved(monkeypatch):
    """A reranker fallback still reports the served arms and the real reason."""
    retriever = _real_retriever(monkeypatch, reranker=None)
    with pytest.raises(Exception):
        # The frozen V1 contract only represents a served reranked set, so a
        # degraded retrieval cannot be represented as one.
        retriever.retrieve(
            "Why did AAPL move?", ticker="AAPL", cutoff=CUTOFF,
            requested_manifest_id=MANIFEST_A,
            temporal_identity=_temporal_identity(),
        )
    observed = retriever.retrieve_with_observation(
        "Why did AAPL move?", ticker="AAPL", cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_A,
        temporal_identity=_temporal_identity(),
        top_k=3,
        candidate_depth=20,
    )
    # A degraded call has no representable V1.1 reranked set, but the real
    # served mode, arm truth and reason are all still observed.
    assert observed.result_set is None
    observation = observed.observation
    assert observation.served_mode != "reranked"
    assert "reranker_failed" in observation.degradation_reasons
    assert set(observation.arm_names) == {"lexical", "dense", "fusion"}
    assert observation.ordered_candidate_evidence_ids


def test_degraded_retriever_does_not_fabricate_final_ids(monkeypatch):
    retriever = _real_retriever(monkeypatch, reranker=None)
    observed = retriever.retrieve_with_observation(
        "Why did AAPL move?", ticker="AAPL", cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_A,
        temporal_identity=_temporal_identity(), top_k=8, candidate_depth=20,
    )
    assert observed.result_set is None
    assert observed.observation.ordered_final_ranked_evidence_ids == ()
