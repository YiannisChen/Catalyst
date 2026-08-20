"""V1.1 retrieval result set/hit contract tests (M2-2).

The V1.1 RetrievalResultSet/RetrievalHit are a separate typed contract in
retrieval/v1_result.py; the live legacy retrieval/result.py contract is
BASELINE_ONLY for this milestone and must remain byte/shape-unchanged until
its named migration gate (M3-11).
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval.result import RetrievalResult
from catalyst_data.trading_calendar import session_close_utc


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _runtime_identity() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="snapshot:7a004",
        corpus_manifest_id="c" * 64,
        fts_index_version="fts:v3",
        query_policy_version="qp:v1",
    )


def _temporal_identity() -> TemporalIdentity:
    monday_close = _utc(session_close_utc("2026-01-05"))
    tuesday_close = _utc(session_close_utc("2026-01-06"))
    return TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-06T14:30:00Z"),
        session_close_at=tuesday_close,
        information_window_start_at=monday_close,
        cutoff_at=tuesday_close,
    )


def _hit(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "evidence_id": "corpus:chunk:0001",
        "canonical_asset_id": "issuer:AAPL:news:0001",
        "content_version_id": "content:v1:0001",
        "corpus_document_id": "corpus:doc:0001",
        "chunk_id": "corpus:chunk:0001",
        "fact_id": None,
        "excerpt": "AAPL reported record quarterly revenue.",
        "scores": {"lexical": 12.5, "dense": None, "fusion": 7.25, "reranked": None},
        "ranks": {"lexical": 1, "dense": None, "fusion": 2, "reranked": None},
        "source_class": "reported_news",
        "content_state": "FULL_TEXT",
        "eligible_at": _utc("2026-01-05T21:05:00Z"),
        "ticker_scope": ("AAPL",),
        "provider": "polygon",
        "publisher": "example-news",
        "dedup_cluster_id": None,
        "parse_quality": "full",
        "retrieval_policy_version": "qp:v1",
        "temporal_identity": _temporal_identity(),
        "data_runtime_identity": _runtime_identity(),
    }
    base.update(overrides)
    return base


def test_v1_hit_is_frozen_and_forbids_extra() -> None:
    from catalyst_data.retrieval.v1_result import RetrievalHit

    hit = RetrievalHit(**_hit())
    with pytest.raises(ValidationError):
        hit.evidence_id = "other"  # frozen
    with pytest.raises(ValidationError):
        RetrievalHit(**_hit(), unknown_field=True)  # extra forbidden


def test_v1_hit_rejects_nan_and_infinite_scores() -> None:
    from catalyst_data.retrieval.v1_result import RetrievalHit

    with pytest.raises(ValidationError):
        RetrievalHit(**_hit(scores={"lexical": float("nan")}))
    with pytest.raises(ValidationError):
        RetrievalHit(**_hit(scores={"lexical": float("inf")}))
    with pytest.raises(ValidationError):
        RetrievalHit(**_hit(scores={"lexical": float("-inf")}))


def test_v1_hit_rejects_zero_or_negative_rank() -> None:
    from catalyst_data.retrieval.v1_result import RetrievalHit

    with pytest.raises(ValidationError):
        RetrievalHit(**_hit(ranks={"lexical": 0}))
    with pytest.raises(ValidationError):
        RetrievalHit(**_hit(ranks={"lexical": -1}))


def test_v1_hit_missing_modality_stays_none_never_fabricated() -> None:
    from catalyst_data.retrieval.v1_result import RetrievalHit

    hit = RetrievalHit(**_hit())
    dumped = hit.model_dump()
    assert dumped["scores"]["dense"] is None
    assert dumped["ranks"]["dense"] is None
    assert dumped["scores"]["reranked"] is None
    assert dumped["ranks"]["reranked"] is None


def test_v1_hit_evidence_id_must_equal_chunk_id_for_text_hits() -> None:
    from catalyst_data.retrieval.v1_result import RetrievalHit

    with pytest.raises(ValidationError):
        RetrievalHit(**_hit(evidence_id="corpus:chunk:9999"))
    with pytest.raises(ValidationError):
        RetrievalHit(
            **_hit(
                evidence_id="fact:1",
                chunk_id=None,
                fact_id="fact:2",
                scores={"structured": 1.0},
                ranks={"structured": 1},
            )
        )


def test_v1_hit_rejects_both_text_and_structured_evidence_identity() -> None:
    from catalyst_data.retrieval.v1_result import RetrievalHit

    with pytest.raises(ValidationError):
        RetrievalHit(
            **_hit(evidence_id="corpus:chunk:0001", fact_id="fact:1")
        )


def test_v1_set_requires_contiguous_ranks_within_participating_stage() -> None:
    from catalyst_data.retrieval.v1_result import RetrievalHit, RetrievalResultSet

    first = RetrievalHit(
        **_hit(
            evidence_id="corpus:chunk:0001",
            chunk_id="corpus:chunk:0001",
            ranks={"lexical": 1, "dense": None, "fusion": None, "reranked": None},
        )
    )
    third = RetrievalHit(
        **_hit(
            evidence_id="corpus:chunk:0003",
            chunk_id="corpus:chunk:0003",
            ranks={"lexical": 3, "dense": None, "fusion": None, "reranked": None},
        )
    )
    with pytest.raises(ValidationError):
        RetrievalResultSet(hits=(first, third))


def test_v1_set_accepts_contiguous_ranks_and_is_frozen() -> None:
    from catalyst_data.retrieval.v1_result import RetrievalHit, RetrievalResultSet

    first = RetrievalHit(
        **_hit(
            evidence_id="corpus:chunk:0001",
            chunk_id="corpus:chunk:0001",
            ranks={"lexical": 1, "dense": None, "fusion": None, "reranked": None},
        )
    )
    second = RetrievalHit(
        **_hit(
            evidence_id="corpus:chunk:0002",
            chunk_id="corpus:chunk:0002",
            ranks={"lexical": 2, "dense": None, "fusion": None, "reranked": None},
        )
    )
    result_set = RetrievalResultSet(hits=(first, second))
    assert len(result_set.hits) == 2
    with pytest.raises(ValidationError):
        result_set.hits = (first,)  # frozen


def test_v1_hit_carries_structured_fact_identity_without_chunk() -> None:
    from catalyst_data.retrieval.v1_result import RetrievalHit

    hit = RetrievalHit(
        **_hit(
            evidence_id="fact:42",
            chunk_id=None,
            fact_id="fact:42",
            excerpt="AAPL P/E 32.1",
            scores={"structured": 1.0},
            ranks={"structured": 1},
            source_class="structured_market_data",
            content_state="FULL_TEXT",
            provider="fmp",
            publisher=None,
        )
    )
    assert hit.evidence_id == "fact:42"
    assert hit.fact_id == "fact:42"


def test_live_retrieval_result_module_audited_shape_unchanged() -> None:
    """No-mutation guard: live legacy result.py keeps its audited legacy shape."""
    from catalyst_data.retrieval import result as result_module_alias

    source = result_module_alias.__file__
    assert source is not None
    assert "retrieval/result.py" in source
    # Import works and the audited legacy field set is exactly preserved.
    assert set(RetrievalResult.model_fields) == {
        "chunk_id",
        "document_id",
        "available_at",
        "cutoff",
        "content_text",
        "filters_applied",
        "source_class",
        "lexical_raw_score",
        "lexical_rank",
        "dense_score",
        "dense_rank",
        "fusion_score",
        "fusion_rank",
        "arm_ranks",
        "arm_scores",
        "reranker_score",
        "reranker_rank",
        "corpus_manifest_id",
        "index_manifest_id",
        "mode_requested",
        "mode_served",
        "is_degraded",
        "fallback_reason",
        "timing_ms",
        "ticker_associations",
        "dedup_cluster_id",
        "cluster_first_available_at",
        "representative_document_id",
        "is_novel",
        "temporal_center_date",
        "query_date",
        "query_date_conflict",
        "query_date_decision",
    }
    # Sanity: the live module's class is NOT the V1.1 class.
    from catalyst_data.retrieval.v1_result import RetrievalHit

    assert RetrievalHit.__module__ == "catalyst_data.retrieval.v1_result"
