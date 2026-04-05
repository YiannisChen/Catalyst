"""Tests for LanceDB Gold layer — RRF logic and hybrid search helpers.

These tests target pure functions only (reciprocal_rank_fusion) so they run
without lancedb or FlagEmbedding installed.
"""

from __future__ import annotations

import pytest
from catalyst_data.storage.lancedb_store import reciprocal_rank_fusion, RRF_K


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
