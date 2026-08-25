"""M3-11: live retrieval emits the M2 V1.1 result contract over a fixture rebuild."""
from __future__ import annotations

import numpy as np
import pytest

from catalyst_data.retrieval.v1_result import RetrievalResultSet as V1RetrievalResultSet
from retrieval_model_fixtures import RecordingReranker, make_result
from test_corpus_rebuild_v1 import (
    NOW,
    POSTBUILD,
    PROBE,
    SNAPSHOT,
    _fixture_conn,
)


CUTOFF = "2026-08-02T00:00:00Z"


def _stage(tmp_path):
    from catalyst_data.corpus.streaming_publication import stage_corpus_candidate

    conn = _fixture_conn(tmp_path / "m3-11.db")
    conn.execute(
        """INSERT INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 1, ?)""",
        ("a" * 64, NOW),
    )
    conn.commit()
    candidate = stage_corpus_candidate(
        conn,
        certified_snapshot_identity=SNAPSHOT,
        profile_versions={"news": "news_v2", "filing": "filing_v3"},
        source_bundle_output_root=tmp_path / "bundles",
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
    )
    return conn, candidate


def _arm_for_chunks(conn, candidate, *, prefix: str, mode_requested: str, mode_served: str):
    from catalyst_data.retrieval.result import RetrievalResultSet

    rows = conn.execute(
        """SELECT chunk_id, document_id, content_text, available_at
           FROM corpus_build_chunks WHERE build_id=? ORDER BY chunk_id LIMIT 3""",
        (candidate.build_id,),
    ).fetchall()
    values = tuple(
        make_result(
            row[0],
            document_id=row[1],
            content_text=row[2],
            available_at=row[3],
            requested_manifest_id=candidate.manifest_id,
            cutoff=CUTOFF,
            lexical_raw_score=-1.0,
            lexical_rank=index,
            dense_score=0.5,
            dense_rank=index,
            mode_requested=mode_requested,
            mode_served=mode_served,
        )
        for index, row in enumerate(rows, start=1)
    )
    return RetrievalResultSet(
        candidates=values,
        results=values,
        candidate_count=len(values),
        mode_requested=mode_requested,
        mode_served=mode_served,
        is_degraded=False,
    )


def test_retrieve_hybrid_reranked_emits_v1_contract(tmp_path, monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module

    conn, candidate = _stage(tmp_path)
    lexical = _arm_for_chunks(
        conn, candidate, prefix="lex", mode_requested="lexical", mode_served="fts5"
    )
    dense = _arm_for_chunks(
        conn, candidate, prefix="den", mode_requested="dense", mode_served="dense"
    )
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *a, **k: lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", lambda *a, **k: dense)
    result = hybrid_module.retrieve_hybrid(
        conn,
        query="Apple AI features",
        ticker="AAPL",
        cutoff=CUTOFF,
        mode="reranked",
        reranker=RecordingReranker(),
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=candidate.manifest_id,
        index_manifest_id="1" * 64,
        lancedb_table=object(),
    )
    assert isinstance(result, V1RetrievalResultSet)
    assert result.hits
    for rank, hit in enumerate(result.hits, start=1):
        assert hit.canonical_asset_id
        assert hit.content_version_id
        assert hit.corpus_document_id
        assert hit.chunk_id == hit.evidence_id
        assert hit.content_state == "FULL_TEXT"
        assert hit.source_class
        assert hit.eligible_at
        assert hit.parse_quality
        assert hit.data_runtime_identity == result.data_runtime_identity
        assert hit.temporal_identity == result.temporal_identity
        assert hit.ranks[-1].value == rank or any(
            entry.stage == "reranked" and entry.value == rank for entry in hit.ranks
        )


def test_identity_mismatch_fails_closed(tmp_path, monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalContractError

    conn, _candidate = _stage(tmp_path)
    with pytest.raises(RetrievalContractError):
        hybrid_module.retrieve_hybrid(
            conn,
            query="Apple",
            ticker="AAPL",
            cutoff=CUTOFF,
            mode="reranked",
            query_embedding=np.ones(1024, dtype=np.float32),
            requested_manifest_id="f" * 64,
            index_manifest_id="1" * 64,
            lancedb_table=object(),
        )


def test_empty_failed_and_post_cutoff_excluded(tmp_path, monkeypatch):
    import catalyst_data.retrieval.hybrid as hybrid_module

    conn, candidate = _stage(tmp_path)
    lexical = _arm_for_chunks(
        conn, candidate, prefix="lex", mode_requested="lexical", mode_served="fts5"
    )
    dense = _arm_for_chunks(
        conn, candidate, prefix="den", mode_requested="dense", mode_served="dense"
    )
    empty_id = lexical.results[0].chunk_id
    real_lookup = hybrid_module._lookup_canonical_chunk

    def lookup(db, chunk_id):
        meta = real_lookup(db, chunk_id)
        if meta is None:
            return None
        if chunk_id == empty_id:
            meta = dict(meta)
            meta["content_state"] = "EMPTY"
        return meta

    monkeypatch.setattr(hybrid_module, "_lookup_canonical_chunk", lookup)
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *a, **k: lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", lambda *a, **k: dense)
    result = hybrid_module.retrieve_hybrid(
        conn,
        query="Apple",
        ticker="AAPL",
        cutoff=CUTOFF,
        mode="reranked",
        reranker=RecordingReranker(),
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=candidate.manifest_id,
        index_manifest_id="1" * 64,
        lancedb_table=object(),
    )
    assert isinstance(result, V1RetrievalResultSet)
    assert all(hit.chunk_id != empty_id for hit in result.hits)
    assert all(hit.content_state not in {"EMPTY", "FAILED"} for hit in result.hits)


def test_retrieve_hybrid_return_v1_false_keeps_legacy_shape(tmp_path, monkeypatch):
    """M3 exit four-arm consumes the legacy HybridRetrievalResult shape.

    ``retrieve_hybrid(..., return_v1=False)`` must return the pre-M3-11
    HybridRetrievalResult (outer arm result sets + flat temporal identity),
    while the default (return_v1=True) keeps the V1.1 RetrievalResultSet.
    """
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.hybrid import HybridRetrievalResult

    conn, candidate = _stage(tmp_path)
    lexical = _arm_for_chunks(
        conn, candidate, prefix="lex", mode_requested="lexical", mode_served="fts5"
    )
    dense = _arm_for_chunks(
        conn, candidate, prefix="den", mode_requested="dense", mode_served="dense"
    )
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *a, **k: lexical)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", lambda *a, **k: dense)
    result = hybrid_module.retrieve_hybrid(
        conn,
        query="Apple AI features",
        ticker="AAPL",
        cutoff=CUTOFF,
        mode="reranked",
        reranker=RecordingReranker(),
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=candidate.manifest_id,
        index_manifest_id="1" * 64,
        lancedb_table=object(),
        return_v1=False,
    )
    assert isinstance(result, HybridRetrievalResult)
    assert result.mode_served == "reranked"
    assert result.lexical_results is not None
    assert result.dense_results is not None
    assert result.fusion_results
    assert result.final_results
    for key in ("temporal_center_date", "query_date", "query_date_conflict", "query_date_decision"):
        assert hasattr(result, key), key
    assert isinstance(result.temporal_center_date, str)
    assert type(result.query_date_conflict) is bool
    assert result.query_date_decision in {"structured", "structured_ignore_query", "none"}
    # Inner evidence results carry the same flat temporal identity.
    for item in result.final_results:
        assert item.temporal_center_date == result.temporal_center_date
        assert item.query_date_decision == result.query_date_decision
