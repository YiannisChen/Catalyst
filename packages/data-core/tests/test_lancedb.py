"""Tests for LanceDB Gold layer — RRF logic, reranker, and hybrid search helpers.

These tests target pure functions only (reciprocal_rank_fusion, _apply_reranker,
load_reranker) so they run without lancedb or FlagEmbedding installed.
"""

from __future__ import annotations

import pytest
from retrieval_model_fixtures import make_result, make_results
from catalyst_data.storage.lancedb_store import (
    reciprocal_rank_fusion,
    _apply_reranker,
    _build_chunk_records,
    _split_l2_sentences,
    load_reranker,
    RRF_K,
    DEFAULT_RERANK_TOP_K,
    hybrid_search,
)


# ---------------------------------------------------------------------------
# reciprocal_rank_fusion
# ---------------------------------------------------------------------------


def test_rrf_single_list():
    """RRF over a single list preserves order and adds rrf_score."""
    results = [{"asset_id": "a1"}, {"asset_id": "a2"}, {"asset_id": "a3"}]
    merged = reciprocal_rank_fusion(results)
    assert len(merged) == 3
    # rank-1 item should stay at position 0
    assert merged[0]["asset_id"] == "a1"
    assert all("rrf_score" in r for r in merged)


def test_rrf_two_lists_boost_overlap():
    """Items appearing in both lists accumulate higher RRF scores."""
    list_a = [{"asset_id": "a1"}, {"asset_id": "a2"}, {"asset_id": "a3"}]
    list_b = [{"asset_id": "a2"}, {"asset_id": "a3"}, {"asset_id": "a4"}]
    merged = reciprocal_rank_fusion(list_a, list_b)
    ids = [r["asset_id"] for r in merged]
    # a2: rank-2 in list_a + rank-1 in list_b → higher combined score than a1 (rank-1 only)
    assert ids.index("a2") < ids.index("a1")


def test_rrf_preserves_metadata():
    """Metadata fields from source dicts survive the merge."""
    results = [{"asset_id": "a1", "ticker": "AAPL", "content_md": "test"}]
    merged = reciprocal_rank_fusion(results)
    assert merged[0]["ticker"] == "AAPL"
    assert merged[0]["content_md"] == "test"


def test_rrf_empty_lists():
    """RRF of empty inputs returns an empty list without error."""
    merged = reciprocal_rank_fusion([], [])
    assert merged == []


def test_rrf_score_computation():
    """Single item at rank-1 earns exactly 1/(k+1)."""
    results = [{"asset_id": "a1"}]
    merged = reciprocal_rank_fusion(results, k=60)
    expected = 1.0 / (RRF_K + 1)
    assert abs(merged[0]["rrf_score"] - expected) < 1e-9


def test_rrf_scores_descending():
    """Output is sorted by rrf_score in descending order."""
    list_a = [{"asset_id": "a1"}, {"asset_id": "a2"}]
    list_b = [{"asset_id": "a2"}, {"asset_id": "a3"}]
    merged = reciprocal_rank_fusion(list_a, list_b)
    scores = [r["rrf_score"] for r in merged]
    assert scores == sorted(scores, reverse=True)


def test_rrf_no_duplicates_in_output():
    """Each asset_id appears exactly once even when it is in multiple lists."""
    list_a = [{"asset_id": "x"}, {"asset_id": "y"}]
    list_b = [{"asset_id": "x"}, {"asset_id": "z"}]
    merged = reciprocal_rank_fusion(list_a, list_b)
    ids = [r["asset_id"] for r in merged]
    assert len(ids) == len(set(ids))


def test_rrf_k_affects_score():
    """A smaller k amplifies the score difference between ranks."""
    results = [{"asset_id": "a"}, {"asset_id": "b"}]
    merged_k1 = reciprocal_rank_fusion(results, k=1)
    merged_k60 = reciprocal_rank_fusion(results, k=60)
    # With k=1, rank-1 score = 1/2 = 0.5; with k=60 it's 1/61 ≈ 0.016
    assert merged_k1[0]["rrf_score"] > merged_k60[0]["rrf_score"]


def test_rrf_metadata_from_first_seen_list():
    """When same asset_id appears in multiple lists, its metadata is retained."""
    list_a = [{"asset_id": "dup", "ticker": "AAPL", "source_type": "news"}]
    list_b = [{"asset_id": "dup", "ticker": "AAPL", "source_type": "news"}]
    merged = reciprocal_rank_fusion(list_a, list_b)
    dup_entry = next(r for r in merged if r["asset_id"] == "dup")
    assert dup_entry["ticker"] == "AAPL"
    assert dup_entry["source_type"] == "news"


def test_legacy_rrf_matches_canonical_fusion_order():
    from catalyst_data.retrieval.fusion import fuse

    left = [{"asset_id": "b", "ticker": "AAPL"}, {"asset_id": "a", "ticker": "AAPL"}]
    right = [{"asset_id": "a", "ticker": "AAPL"}, {"asset_id": "c", "ticker": "AAPL"}]
    legacy = reciprocal_rank_fusion(left, right)
    canonical = fuse(
        [make_result(item["asset_id"]) for item in left],
        [make_result(item["asset_id"]) for item in right],
    )
    assert [item["asset_id"] for item in legacy] == [item.chunk_id for item in canonical]


def test_legacy_rrf_preserves_a_third_arm():
    shared = [{"asset_id": "shared"}]
    merged = reciprocal_rank_fusion(shared, [], shared)
    assert merged[0]["asset_id"] == "shared"
    assert merged[0]["rrf_score"] == pytest.approx(2 / 61)


def test_legacy_rrf_rejects_mixed_index_identity():
    with pytest.raises(ValueError, match="one index"):
        reciprocal_rank_fusion(
            [{"asset_id": "shared", "index_manifest_id": "1" * 64}],
            [{"asset_id": "shared", "index_manifest_id": "2" * 64}],
        )


# ---------------------------------------------------------------------------
# _apply_reranker
# ---------------------------------------------------------------------------

_SAMPLE_CHUNKS = make_results(10, prefix="c", content_text="Content about topic")


class _MockPredictReranker:
    """Simulates CrossEncoder.predict() — returns list of floats."""

    def predict(self, pairs):
        # Reverse order: last chunk gets highest score
        return [float(len(pairs) - i) for i in range(len(pairs))]


class _MockComputeScoreReranker:
    """Simulates FlagReranker.compute_score() — returns list of floats."""

    def compute_score(self, pairs):
        return [float(i) for i in range(len(pairs))]


class _MockScalarReranker:
    """Returns a scalar score (single-pair edge case)."""

    def predict(self, pairs):
        assert len(pairs) == 1
        return 0.75


def test_apply_reranker_with_predict():
    """_apply_reranker works with CrossEncoder-style .predict()."""
    chunks = list(_SAMPLE_CHUNKS[:5])
    result = _apply_reranker(chunks, "test query", _MockPredictReranker(), top_k=3)

    assert len(result) == 3
    assert all(c.reranker_score is not None for c in result)
    # Scores are descending
    scores = [c.reranker_score for c in result]
    assert scores == sorted(scores, reverse=True)


def test_apply_reranker_with_compute_score():
    """_apply_reranker works with FlagReranker-style .compute_score()."""
    chunks = list(_SAMPLE_CHUNKS[:5])
    result = _apply_reranker(chunks, "test query", _MockComputeScoreReranker(), top_k=3)

    assert len(result) == 3
    # compute_score returns [0, 1, 2, 3, 4] — highest is c5
    assert result[0].chunk_id == "c:04"


def test_apply_reranker_scalar_score():
    """Single-pair reranking where reranker returns a scalar."""
    chunks = [_SAMPLE_CHUNKS[0]]
    result = _apply_reranker(chunks, "test query", _MockScalarReranker(), top_k=1)

    assert len(result) == 1
    assert result[0].reranker_score == pytest.approx(0.75)


def test_apply_reranker_empty_chunks():
    """Empty chunk list returns empty without calling reranker."""
    result = _apply_reranker([], "test query", _MockPredictReranker(), top_k=5)
    assert result == []


def test_apply_reranker_top_k_limits_output():
    """Output length is capped at top_k."""
    chunks = list(_SAMPLE_CHUNKS)
    result = _apply_reranker(chunks, "query", _MockPredictReranker(), top_k=3)
    assert len(result) == 3


def test_apply_reranker_preserves_metadata():
    """Reranking preserves all original chunk fields."""
    chunks = [make_result("x", ticker="NVDA", content_text="hello")]

    class SimpleReranker:
        def predict(self, pairs):
            return [0.9]

    result = _apply_reranker(chunks, "q", SimpleReranker(), top_k=1)
    assert result[0].filters_applied.ticker == "NVDA"
    assert result[0].chunk_id == "x"
    assert result[0].reranker_score == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# load_reranker
# ---------------------------------------------------------------------------


def test_load_reranker_returns_none_when_import_fails(monkeypatch):
    """load_reranker returns None gracefully when sentence_transformers is missing."""
    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if "sentence_transformers" in name:
            raise ImportError("mocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    result = load_reranker("some-model")
    assert result is None


# ---------------------------------------------------------------------------
# L2 chunking helpers
# ---------------------------------------------------------------------------


class _MockSentenceTokenizer:
    def __init__(self, sentences):
        self._sentences = sentences

    def tokenize(self, _text):
        return list(self._sentences)


def test_split_l2_sentences_filters_and_caps():
    tokenizer = _MockSentenceTokenizer(
        [
            "short",
            "This sentence should stay because it is long enough.",
            "Another qualifying sentence that should be kept.",
            "Third qualifying sentence should be removed by cap.",
        ]
    )
    chunks = _split_l2_sentences(
        "ignored input",
        tokenizer=tokenizer,
        max_sentences_per_asset=2,
        min_sentence_length=12,
    )
    assert chunks == [
        "This sentence should stay because it is long enough.",
        "Another qualifying sentence that should be kept.",
    ]


def test_build_chunk_records_adds_l2_only_for_polygon_news():
    rows = [
        ("asset-pn", "NVDA", "polygon_news", "2026-01-01", "First. Second qualifying sentence."),
        ("asset-fred", "SPY", "fred_macro", "2026-01-01", "Macro content."),
    ]
    tokenizer = _MockSentenceTokenizer(["First sentence is long enough.", "Second sentence is long enough."])
    records = _build_chunk_records(
        rows,
        tokenizer=tokenizer,
        max_sentences_per_asset=30,
        min_sentence_length=12,
    )
    l1_records = [r for r in records if r["chunk_level"] == "l1"]
    l2_records = [r for r in records if r["chunk_level"] == "l2"]

    assert len(l1_records) == 2
    assert all(r["parent_asset_id"] in {"asset-pn", "asset-fred"} for r in l1_records)
    assert len(l2_records) == 2
    assert all(r["parent_asset_id"] == "asset-pn" for r in l2_records)
    assert [r["asset_id"] for r in l2_records] == [
        "asset-pn::l2s0001",
        "asset-pn::l2s0002",
    ]


# ---------------------------------------------------------------------------
# hybrid_search guardrail
# ---------------------------------------------------------------------------


class _FakeDataFrame:
    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        for idx, row in enumerate(self._rows):
            yield idx, row


class _FakeQueryBuilder:
    def __init__(self, rows):
        self._rows = rows

    def limit(self, _n):
        return self

    def where(self, _clause, prefilter=True):  # noqa: ARG002
        return self

    def to_pandas(self):
        return _FakeDataFrame(self._rows)


class _FakeTable:
    def __init__(self, vector_rows, fts_rows):
        self._vector_rows = vector_rows
        self._fts_rows = fts_rows

    def search(self, _query, query_type="vector"):
        if query_type == "vector":
            return _FakeQueryBuilder(self._vector_rows)
        return _FakeQueryBuilder(self._fts_rows)


def test_hybrid_search_requires_identity_bound_contract():
    with pytest.raises(TypeError):
        hybrid_search(table=object(), query="why move")
