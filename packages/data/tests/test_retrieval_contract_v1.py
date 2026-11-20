"""M3-11: live retrieval emits the M2 V1.1 result contract over a fixture rebuild."""
from __future__ import annotations

import pytest
from retrieval_model_fixtures import make_result
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












# ---------------------------------------------------------------------------
# M4-0: production V1.1 retrieval binding (amendment §1)
# ---------------------------------------------------------------------------


def _utc(iso: str):
    from datetime import datetime, timezone

    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _identity_pair(*, corpus_manifest_id: str):
    from catalyst_data.canonical.identity import DataRuntimeIdentity
    from catalyst_data.canonical.temporal import TemporalIdentity

    temporal = TemporalIdentity(
        session_date="2026-08-01",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-08-01T13:30:00Z"),
        session_close_at=_utc("2026-08-01T20:00:00Z"),
        information_window_start_at=_utc("2026-07-31T20:00:00Z"),
        cutoff_at=_utc(CUTOFF),
    )
    runtime = DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id=corpus_manifest_id,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )
    return temporal, runtime










def _other_chunk_row(conn, candidate):
    """Extract one corpus_build_chunks row (with build_id) for manual insertion
    into a second build/manifest in M4-0 binding tests."""
    cols = [
        "build_id", "chunk_id", "document_id", "chunk_profile_version",
        "section_key", "ordinal", "content_text", "content_hash",
        "metadata_hash", "source_class", "dedup_cluster_id",
        "cluster_first_available_at", "representative_document_id",
        "available_at", "ticker_associations", "eligibility", "status",
        "boundary_kind", "body_token_start", "body_token_end",
        "body_overlap_tokens", "prefix_token_count", "prefix_truncated",
        "section_parse_degraded", "source_kind", "provider", "source_type",
        "canonical_asset_id", "content_version_id", "corpus_document_id",
        "content_state", "independence_group_id", "parse_quality",
        "created_at", "updated_at",
    ]
    row = conn.execute(
        "SELECT " + ", ".join(cols) + " FROM corpus_build_chunks WHERE build_id=? LIMIT 1",
        (candidate.build_id,),
    ).fetchone()
    return cols, dict(zip(cols, row))
















def test_v1_hit_rejects_material_capability_mismatch():
    """material_capability must derive exactly from content_state (M4-0 §1.3)."""

    from catalyst_data.canonical.identity import DataRuntimeIdentity
    from catalyst_data.canonical.temporal import TemporalIdentity
    from catalyst_data.retrieval.v1_result import RetrievalHit, StageRank, StageScore

    temporal = TemporalIdentity(
        session_date="2026-08-01",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-08-01T13:30:00Z"),
        session_close_at=_utc("2026-08-01T20:00:00Z"),
        information_window_start_at=_utc("2026-07-31T20:00:00Z"),
        cutoff_at=_utc(CUTOFF),
    )
    runtime = DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id="m" * 64,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )

    def build(**overrides):
        base = dict(
            evidence_id="chunk:1",
            canonical_asset_id="a",
            content_version_id="v",
            corpus_document_id="d",
            chunk_id="chunk:1",
            excerpt="text",
            scores=(StageScore(stage="reranked", value=1.0),),
            ranks=(StageRank(stage="reranked", value=1),),
            source_class="reported_news",
            content_state="FULL_TEXT",
            eligible_at=_utc("2026-08-01T00:00:00Z"),
            ticker_scope=("AAPL",),
            provider="polygon",
            publisher=None,
            dedup_cluster_id=None,
            parse_quality="not_applicable",
            retrieval_policy_version="qp:v1",
            temporal_identity=temporal,
            data_runtime_identity=runtime,
            section_key="body",
            chunk_ordinal=1,
            asset_type="NEWS",
            content_hash="c" * 64,
            material_capability="MATERIAL_CAPABLE",
            serving_status="body_candidate",
            temporal_precision="publication_time",
            independence_group_id=None,
            canonical_url=None,
            evidence_role="INDEPENDENT_REPORT",
        )
        base.update(overrides)
        return base

    RetrievalHit(**build())
    with pytest.raises(Exception):
        RetrievalHit(**build(material_capability="LEAD_ONLY"))  # mismatch for FULL_TEXT
    with pytest.raises(Exception):
        RetrievalHit(**build(content_state="METADATA_ONLY", material_capability="MATERIAL_CAPABLE"))
    with pytest.raises(Exception):
        RetrievalHit(**build(content_state="EMPTY", material_capability="NOT_CAPABLE"))
    metadata_only = RetrievalHit(
        **build(
            content_state="METADATA_ONLY",
            material_capability="NOT_CAPABLE",
            excerpt="title only",
        )
    )
    assert metadata_only.material_capability == "NOT_CAPABLE"


