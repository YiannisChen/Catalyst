from __future__ import annotations

import pytest
from pydantic import ValidationError
from types import SimpleNamespace

from catalyst_data.retrieval.result import (
    RetrievalFilters,
    RetrievalResult,
    RetrievalResultSet,
)
from catalyst_data.retrieval.trace import RetrievalTrace
from retrieval_fixtures import MANIFEST_A


def _result(rank: int = 1) -> RetrievalResult:
    filters = RetrievalFilters(
        ticker="AAPL", requested_manifest_id=MANIFEST_A,
        cutoff="2026-01-15T21:00:00Z",
    )
    return RetrievalResult(
        chunk_id=f"polygon:article-{rank}:news_v2:body:0001",
        document_id=f"polygon:article-{rank}",
        available_at="2026-01-01T09:00:00Z",
        cutoff=filters.cutoff,
        filters_applied=filters,
        source_class="reported_news",
        lexical_raw_score=-1.0,
        lexical_rank=rank,
        corpus_manifest_id=MANIFEST_A,
        index_manifest_id=None,
        mode_requested="lexical",
        mode_served="fts5",
        is_degraded=False,
        fallback_reason=None,
        timing_ms=1.0,
    )


def test_result_models_are_frozen_and_forbid_extra():
    result = _result()
    with pytest.raises(ValidationError):
        result.lexical_rank = 2
    with pytest.raises(ValidationError):
        RetrievalResult(**result.model_dump(), unknown=True)


def test_trace_rejects_impossible_count_order():
    with pytest.raises(ValidationError):
        RetrievalTrace(
            manifest_row_count=1, eligible_row_count=2, matched_row_count=0,
            candidate_count=0, final_count=0, filter_ms=0, score_ms=0,
            total_ms=0, mode_requested="lexical", mode_served="fts5",
        )


def test_result_set_requires_results_prefix_and_exact_count():
    first, second = _result(1), _result(2)
    with pytest.raises(ValidationError):
        RetrievalResultSet(
            candidates=(first, second), results=(second,), candidate_count=2,
            mode_requested="lexical", mode_served="fts5",
            is_degraded=False, fallback_reason=None,
        )


def test_result_set_requires_retrieval_result_instances():
    with pytest.raises(ValidationError):
        RetrievalResultSet(
            candidates=(SimpleNamespace(chunk_id="fixture"),),
            results=(SimpleNamespace(chunk_id="fixture"),),
            candidate_count=1,
            mode_requested="lexical",
            mode_served="fts5",
            is_degraded=False,
        )
