"""M3-9: inactive candidate source-bundle export."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalyst_data.retrieval.gpu_contract import verify_source_bundle
from catalyst_data.storage.sqlite import init_db

from test_corpus_rebuild_v1 import (
    NOW,
    POSTBUILD,
    PROBE,
    SNAPSHOT,
    _fixture_conn,
    _pointer_snapshot,
    _seed_live_pointers,
)


def test_export_candidate_source_bundle_missing_api():
    from catalyst_data.retrieval.source_bundle import export_candidate_source_bundle

    assert callable(export_candidate_source_bundle)


def test_candidate_bundle_export_shape_and_pointer_isolation(tmp_path):
    from catalyst_data.corpus.streaming_publication import stage_corpus_candidate
    from catalyst_data.retrieval.source_bundle import export_candidate_source_bundle

    conn = _fixture_conn(tmp_path / "m3-9-bundle.db")
    active_path = tmp_path / "active_generation.json"
    _seed_live_pointers(conn, active_path)
    before = _pointer_snapshot(conn, active_path)
    out = tmp_path / "bundles"
    profiles = {"news": "news_v2", "filing": "filing_v3"}

    candidate = stage_corpus_candidate(
        conn,
        certified_snapshot_identity=SNAPSHOT,
        profile_versions=profiles,
        source_bundle_output_root=out,
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
    )
    bundle_id, bundle_path = export_candidate_source_bundle(
        conn,
        build_id=candidate.build_id,
        manifest_id=candidate.manifest_id,
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
        output_root=out,
    )
    assert bundle_id == candidate.source_bundle_id
    assert Path(bundle_path) == Path(candidate.source_bundle_path)
    names = {path.name for path in Path(bundle_path).iterdir() if path.is_file()}
    assert names == {"chunks.jsonl", "source_bundle_manifest.json", "checksums.sha256"}
    manifest = json.loads((Path(bundle_path) / "source_bundle_manifest.json").read_text())
    assert manifest["build_id"] == candidate.build_id
    assert manifest["corpus_manifest_id"] == candidate.manifest_id
    assert manifest["snapshot_id"] == SNAPSHOT
    assert manifest["probe_report_id"] == PROBE
    assert manifest["postbuild_readiness_id"] == POSTBUILD
    assert manifest["chunk_count"] == candidate.chunk_count
    assert manifest["source_bundle_id"] == bundle_id
    verify_source_bundle(
        Path(bundle_path),
        expected_source_bundle_id=bundle_id,
        expected_snapshot_id=SNAPSHOT,
        expected_corpus_manifest_id=candidate.manifest_id,
        expected_probe_report_id=PROBE,
        expected_postbuild_readiness_id=POSTBUILD,
    )

    again_id, again_path = export_candidate_source_bundle(
        conn,
        build_id=candidate.build_id,
        manifest_id=candidate.manifest_id,
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
        output_root=out,
    )
    assert again_id == bundle_id
    assert Path(again_path) == Path(bundle_path)
    first_bytes = {
        name: (Path(bundle_path) / name).read_bytes()
        for name in ("chunks.jsonl", "source_bundle_manifest.json", "checksums.sha256")
    }
    second_bytes = {
        name: (Path(again_path) / name).read_bytes()
        for name in ("chunks.jsonl", "source_bundle_manifest.json", "checksums.sha256")
    }
    assert first_bytes == second_bytes

    (Path(bundle_path) / "chunks.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError):
        export_candidate_source_bundle(
            conn,
            build_id=candidate.build_id,
            manifest_id=candidate.manifest_id,
            snapshot_id=SNAPSHOT,
            probe_report_id=PROBE,
            postbuild_readiness_id=POSTBUILD,
            output_root=out,
        )

    live = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current=1"
    ).fetchone()[0]
    with pytest.raises(ValueError):
        export_candidate_source_bundle(
            conn,
            build_id=candidate.build_id,
            manifest_id=live,
            snapshot_id=SNAPSHOT,
            probe_report_id=PROBE,
            postbuild_readiness_id=POSTBUILD,
            output_root=tmp_path / "other",
        )

    assert _pointer_snapshot(conn, active_path) == before
    lex_after = conn.execute(
        "SELECT corpus_manifest_id, mode_served FROM lexical_index_state WHERE singleton_id=1"
    ).fetchone()
    assert tuple(lex_after) == (live, "fts5")


def test_candidate_export_does_not_read_served_chunks(tmp_path, monkeypatch):
    from catalyst_data.corpus import streaming_publication as sp
    from catalyst_data.retrieval import source_bundle as bundle_mod

    conn = _fixture_conn(tmp_path / "m3-9-spy.db")
    _seed_live_pointers(conn, tmp_path / "active_generation.json")
    out = tmp_path / "bundles"
    candidate = __import__(
        "catalyst_data.corpus.streaming_publication",
        fromlist=["stage_corpus_candidate"],
    ).stage_corpus_candidate(
        conn,
        certified_snapshot_identity=SNAPSHOT,
        profile_versions={"news": "news_v2", "filing": "filing_v3"},
        source_bundle_output_root=out,
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
    )

    def _boom(*_args, **_kwargs):
        raise AssertionError("served_chunks_relation must not be used for candidate export")

    monkeypatch.setattr(sp, "served_chunks_relation", _boom)
    monkeypatch.setattr(bundle_mod, "served_chunks_relation", _boom)
    bundle_mod.export_candidate_source_bundle(
        conn,
        build_id=candidate.build_id,
        manifest_id=candidate.manifest_id,
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
        output_root=out,
    )
