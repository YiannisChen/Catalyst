"""M3-10: inactive candidate FTS build over corpus_build_chunks_fts."""
from __future__ import annotations

from unittest.mock import patch

from test_corpus_rebuild_v1 import (
    POSTBUILD,
    PROBE,
    SNAPSHOT,
    _fixture_conn,
    _pointer_snapshot,
    _seed_live_pointers,
)


def test_build_candidate_fts_missing_api():
    from catalyst_data.corpus.streaming_publication import build_candidate_fts

    assert callable(build_candidate_fts)


def test_build_candidate_fts_is_inactive_and_reproducible(tmp_path):
    from catalyst_data.corpus.streaming_publication import (
        StreamingLexicalResult,
        build_candidate_fts,
        stage_corpus_candidate,
    )

    conn = _fixture_conn(tmp_path / "m3-10-fts.db")
    active_path = tmp_path / "active_generation.json"
    _seed_live_pointers(conn, active_path)
    before = _pointer_snapshot(conn, active_path)
    live_fts = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='corpus_chunks_fts'"
    ).fetchone()[0]
    candidate = stage_corpus_candidate(
        conn,
        certified_snapshot_identity=SNAPSHOT,
        profile_versions={"news": "news_v2", "filing": "filing_v3"},
        source_bundle_output_root=tmp_path / "bundles",
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
    )
    fts5_calls: list[object] = []
    with patch(
        "catalyst_data.retrieval.fts5_builder.build_fts5_index",
        side_effect=lambda *a, **k: fts5_calls.append((a, k)),
    ):
        first = build_candidate_fts(conn, build_id=candidate.build_id)
        second = build_candidate_fts(conn, build_id=candidate.build_id)

    assert isinstance(first, StreamingLexicalResult)
    assert first.manifest_id == candidate.manifest_id
    assert first.mode_served == "fts5"
    assert first.row_count == candidate.chunk_count
    assert first.digest == second.digest
    assert first.row_count == second.row_count
    indexed = conn.execute(
        "SELECT COUNT(*) FROM corpus_build_chunks_fts WHERE build_id=?",
        (candidate.build_id,),
    ).fetchone()[0]
    assert indexed == candidate.chunk_count
    lex = conn.execute(
        "SELECT corpus_manifest_id, mode_served, row_count FROM lexical_index_state "
        "WHERE singleton_id=1"
    ).fetchone()
    assert tuple(lex) == ("a" * 64, "fts5", 1)
    after_fts = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='corpus_chunks_fts'"
    ).fetchone()[0]
    assert after_fts == live_fts
    assert fts5_calls == []
    assert _pointer_snapshot(conn, active_path) == before
    current = conn.execute(
        "SELECT is_current FROM corpus_manifest WHERE manifest_id=?",
        (candidate.manifest_id,),
    ).fetchone()[0]
    assert int(current) == 0
