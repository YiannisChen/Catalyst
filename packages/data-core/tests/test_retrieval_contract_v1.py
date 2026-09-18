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
        assert hit.content_state in {"FULL_TEXT", "METADATA_ONLY", "TITLE_ONLY"}
        assert hit.material_capability == {
            "FULL_TEXT": "MATERIAL_CAPABLE",
            "TITLE_ONLY": "LEAD_ONLY",
            "METADATA_ONLY": "NOT_CAPABLE",
        }[hit.content_state]
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

    def lookup(db, chunk_id, *, requested_manifest_id):
        meta = real_lookup(db, chunk_id, requested_manifest_id=requested_manifest_id)
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


def test_retrieve_hybrid_return_type_annotation_is_accurate():
    """The public annotation must reflect both runtime return shapes.

    ``retrieve_hybrid`` may return the V1.1 ``RetrievalResultSet`` (default,
    successful reranked) or the legacy ``HybridRetrievalResult``
    (``return_v1=False``, hybrid mode, fail/degraded paths). The declared type
    and the overload set must state that; runtime semantics are unchanged.
    """
    import typing

    from catalyst_data.retrieval.hybrid import (
        HybridRetrievalResult,
        retrieve_hybrid,
    )
    from catalyst_data.retrieval.v1_result import (
        RetrievalResultSet as V1RetrievalResultSet,
    )

    hints = typing.get_type_hints(retrieve_hybrid)
    declared = hints["return"]
    declared_args = set(typing.get_args(declared))
    assert V1RetrievalResultSet in declared_args
    assert HybridRetrievalResult in declared_args

    overloads = typing.get_overloads(retrieve_hybrid)
    assert len(overloads) == 2
    legacy_overload = None
    default_overload = None
    for fn in overloads:
        fn_hints = typing.get_type_hints(fn)
        rv_args = typing.get_args(fn_hints.get("return_v1", ()))
        if rv_args == (False,):
            legacy_overload = fn_hints
        elif rv_args == (True,):
            default_overload = fn_hints
    assert legacy_overload is not None, "Literal[False] overload missing"
    assert default_overload is not None, "Literal[True] overload missing"
    assert legacy_overload["return"] == HybridRetrievalResult
    default_args = set(typing.get_args(default_overload["return"]))
    assert V1RetrievalResultSet in default_args
    assert HybridRetrievalResult in default_args


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


def _stage_with_arms(tmp_path, monkeypatch):
    """Stage a fixture candidate and monkeypatch both retrieval arms."""
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
    return conn, candidate


def _reranked_hits(tmp_path, monkeypatch, **kwargs):
    import catalyst_data.retrieval.hybrid as hybrid_module

    conn, candidate = _stage_with_arms(tmp_path, monkeypatch)
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
        **kwargs,
    )
    assert isinstance(result, V1RetrievalResultSet)
    return conn, candidate, result


def test_v1_hit_carries_full_data_owned_metadata(tmp_path, monkeypatch):
    """Every reranked hit carries the complete data-owned V1.1 metadata and no
    hardcoded fixture provider/ticker values (M4-0 amendment §1.3/§1.5)."""
    conn, _, result = _reranked_hits(tmp_path, monkeypatch)
    assert result.hits
    by_type = {}
    for hit in result.hits:
        by_type.setdefault(hit.asset_type, []).append(hit)
        assert hit.section_key is not None
        assert hit.chunk_ordinal == 1
        assert hit.content_hash and len(hit.content_hash) == 64
        assert hit.canonical_url
        assert hit.ticker_scope == ("AAPL",)
    assert set(by_type) == {"NEWS", "FILING"}
    for hit in by_type["FILING"]:
        assert hit.content_state == "FULL_TEXT"
        assert hit.material_capability == "MATERIAL_CAPABLE"
        assert hit.serving_status == "body_candidate"
        assert hit.provider == "sec"
        assert hit.source_class == "official_government"
        assert hit.evidence_role == "PRIMARY_AUTHORITY"
        assert hit.temporal_precision == "accepted_time"
        assert hit.independence_group_id is None
    for hit in by_type["NEWS"]:
        assert hit.content_state == "METADATA_ONLY"
        assert hit.material_capability == "NOT_CAPABLE"
        assert hit.serving_status == "lead_candidate"
        assert hit.provider == "finnhub"
        assert hit.source_class == "reported_news"
        assert hit.evidence_role == "INDEPENDENT_REPORT"
        assert hit.temporal_precision == "publication_time"
        assert hit.independence_group_id is None




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


def test_v1_hit_ticker_and_provider_come_from_canonical_data(tmp_path, monkeypatch):
    """A chunk whose canonical row carries a non-AAPL ticker must surface that
    ticker (proves no hardcoded AAPL/provider in the production conversion)."""
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalFilters, RetrievalResult, RetrievalResultSet

    conn, candidate = _stage(tmp_path)
    other_manifest = "e" * 64
    conn.execute(
        "INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at) VALUES (?, '{}', 0, ?)",
        (other_manifest, NOW),
    )
    cols, values = _other_chunk_row(conn, candidate)
    values["chunk_id"] = "other-gen-chunk:0001"
    values["provider"] = "other_provider"
    values["ticker_associations"] = '["NVDA"]'
    other_build = "9" * 64
    values["build_id"] = other_build
    conn.execute(
        "INSERT INTO corpus_publication_builds (build_id, certified_snapshot_identity, header_json, manifest_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (other_build, SNAPSHOT, "{}", other_manifest, "staging", NOW, NOW),
    )
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO corpus_build_chunks ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(values[c] for c in cols),
    )
    conn.commit()

    other_id = "other-gen-chunk:0001"
    arm_value = RetrievalResult(
        chunk_id=other_id,
        document_id=values["document_id"],
        available_at=values["available_at"],
        cutoff=CUTOFF,
        content_text=values["content_text"],
        filters_applied=RetrievalFilters(
            ticker="NVDA",
            requested_manifest_id=other_manifest,
            cutoff=CUTOFF,
        ),
        source_class=values["source_class"],
        lexical_raw_score=-1.0,
        lexical_rank=1,
        corpus_manifest_id=other_manifest,
        index_manifest_id="1" * 64,
        mode_requested="lexical",
        mode_served="fts5",
        is_degraded=False,
        timing_ms=1.0,
    )
    arm = RetrievalResultSet(
        candidates=(arm_value,), results=(arm_value,), candidate_count=1,
        mode_requested="lexical", mode_served="fts5", is_degraded=False,
    )
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *a, **k: arm)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", lambda *a, **k: arm)
    result = hybrid_module.retrieve_hybrid(
        conn,
        query="NVDA",
        ticker="NVDA",
        cutoff=CUTOFF,
        mode="reranked",
        reranker=RecordingReranker(),
        query_embedding=np.ones(1024, dtype=np.float32),
        requested_manifest_id=other_manifest,
        index_manifest_id="1" * 64,
        lancedb_table=object(),
    )
    assert isinstance(result, V1RetrievalResultSet)
    assert result.hits
    assert result.hits[0].provider == "other_provider"
    assert result.hits[0].ticker_scope == ("NVDA",)


def test_injected_identities_pass_through_unchanged(tmp_path, monkeypatch):
    """Exact injected DataRuntimeIdentity/TemporalIdentity pass through and equal
    the set-level identities; the fabricated cutoff-derived identity is not used."""
    import catalyst_data.retrieval.hybrid as hybrid_module

    conn, candidate = _stage_with_arms(tmp_path, monkeypatch)
    temporal, runtime = _identity_pair(corpus_manifest_id=candidate.manifest_id)
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
        temporal_identity=temporal,
        data_runtime_identity=runtime,
    )
    assert isinstance(result, V1RetrievalResultSet)
    assert result.temporal_identity == temporal
    assert result.data_runtime_identity == runtime
    assert result.temporal_identity.session_open_at != result.temporal_identity.cutoff_at
    assert result.temporal_identity.information_window_start_at != result.temporal_identity.cutoff_at
    assert result.temporal_identity.session_close_at != result.temporal_identity.cutoff_at
    for hit in result.hits:
        assert hit.temporal_identity == temporal
        assert hit.data_runtime_identity == runtime


def test_injected_runtime_identity_must_match_requested_manifest(tmp_path, monkeypatch):
    """A DataRuntimeIdentity whose corpus_manifest_id disagrees with the
    requested manifest is an integrity failure, never degraded success."""
    from catalyst_data.retrieval.result import RetrievalContractError

    _, runtime = _identity_pair(corpus_manifest_id="b" * 64)
    with pytest.raises(RetrievalContractError):
        _reranked_hits(tmp_path, monkeypatch, data_runtime_identity=runtime)


def test_canonical_lookup_is_manifest_qualified(tmp_path, monkeypatch):
    """Identical chunk IDs across generations resolve only through the requested
    manifest/build; a different manifest with the same chunk id resolves to its
    own row, and an unknown manifest fails closed."""
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalContractError

    conn, candidate = _stage(tmp_path)
    other_manifest = "e" * 64
    conn.execute(
        "INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at) VALUES (?, '{}', 0, ?)",
        (other_manifest, NOW),
    )
    cols, values = _other_chunk_row(conn, candidate)
    values["provider"] = "other_provider"
    other_build = "9" * 64
    values["build_id"] = other_build
    conn.execute(
        "INSERT INTO corpus_publication_builds (build_id, certified_snapshot_identity, header_json, manifest_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (other_build, SNAPSHOT, "{}", other_manifest, "staging", NOW, NOW),
    )
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO corpus_build_chunks ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(values[c] for c in cols),
    )
    conn.commit()

    # Same chunk id resolves to the manifest-bound provider.
    meta = hybrid_module._lookup_canonical_chunk(
        conn, values["chunk_id"], requested_manifest_id=other_manifest
    )
    assert meta["provider"] == "other_provider"
    meta = hybrid_module._lookup_canonical_chunk(
        conn, values["chunk_id"], requested_manifest_id=candidate.manifest_id
    )
    assert meta["provider"] in {"polygon", "sec"}

    # Unknown manifest/chunk combination fails closed (never silent fallback).
    with pytest.raises(RetrievalContractError):
        hybrid_module._lookup_canonical_chunk(
            conn, "missing-chunk", requested_manifest_id=candidate.manifest_id
        )


def test_canonical_lookup_duplicate_rows_fail_closed(tmp_path):
    """Two builds under the same requested manifest containing the same chunk id
    are an integrity failure, never a nondeterministic resolve."""
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalContractError

    conn, candidate = _stage(tmp_path)
    cols, values = _other_chunk_row(conn, candidate)
    duplicate_build = "8" * 64
    values["build_id"] = duplicate_build
    conn.execute(
        "INSERT INTO corpus_publication_builds (build_id, certified_snapshot_identity, header_json, manifest_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (duplicate_build, SNAPSHOT, "{}", candidate.manifest_id, "staging", NOW, NOW),
    )
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO corpus_build_chunks ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(values[c] for c in cols),
    )
    conn.commit()
    with pytest.raises(RetrievalContractError):
        hybrid_module._lookup_canonical_chunk(
            conn, values["chunk_id"], requested_manifest_id=candidate.manifest_id
        )


def test_canonical_lookup_conflicting_chain_fails_closed(tmp_path):
    """A canonical asset/version join that disagrees with the build row
    (document identity or eligible timestamp) is an integrity failure."""
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalContractError

    conn, candidate = _stage(tmp_path)
    row = conn.execute(
        """SELECT chunk_id, canonical_asset_id, corpus_document_id, available_at
           FROM corpus_build_chunks WHERE build_id=? LIMIT 1""",
        (candidate.build_id,),
    ).fetchone()
    chunk_id = row[0]
    # Break the canonical eligible_at agreement.
    conn.execute(
        "UPDATE canonical_assets SET eligible_at=? WHERE asset_id=?",
        ("2020-01-01T00:00:00Z", row[1]),
    )
    conn.commit()
    with pytest.raises(RetrievalContractError):
        hybrid_module._lookup_canonical_chunk(
            conn, chunk_id, requested_manifest_id=candidate.manifest_id
        )


def test_empty_result_set_retains_exact_identities(tmp_path, monkeypatch):
    """An empty reranked result set still carries the exact injected set-level
    identities (supervisor clarification 1)."""
    import catalyst_data.retrieval.hybrid as hybrid_module
    from catalyst_data.retrieval.result import RetrievalResultSet as LegacyResultSet

    conn, candidate = _stage(tmp_path)
    empty = LegacyResultSet(
        candidates=(), results=(), candidate_count=0,
        mode_requested="lexical", mode_served="fts5", is_degraded=False,
    )
    monkeypatch.setattr(hybrid_module, "retrieve_lexical", lambda *a, **k: empty)
    monkeypatch.setattr(hybrid_module, "retrieve_dense", lambda *a, **k: empty)
    temporal, runtime = _identity_pair(corpus_manifest_id=candidate.manifest_id)
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
        temporal_identity=temporal,
        data_runtime_identity=runtime,
    )
    assert isinstance(result, V1RetrievalResultSet)
    assert result.hits == ()
    assert result.temporal_identity == temporal
    assert result.data_runtime_identity == runtime


def test_v1_hit_rejects_material_capability_mismatch():
    """material_capability must derive exactly from content_state (M4-0 §1.3)."""
    from catalyst_data.canonical.identity import DataRuntimeIdentity
    from catalyst_data.canonical.temporal import TemporalIdentity
    from catalyst_data.retrieval.v1_result import RetrievalHit, StageRank, StageScore
    from datetime import datetime, timezone

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
