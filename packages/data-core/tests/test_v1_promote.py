"""M3-12A: reversible V1.1 generation promotion (library).

Covers execution-lock §I promotion/rollback protocol: journal state machine,
one-transaction SQLite pointer flips, atomic dense pointer replacement,
mandatory rollback verification, idempotent COMMITTED reruns, and fail-closed
journal/identity handling. Fixture vectors only; no GPU.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from test_corpus_rebuild_v1 import (
    POSTBUILD,
    PROBE,
    SNAPSHOT,
    _fixture_conn,
    _pointer_snapshot,
    _seed_live_pointers,
)

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.corpus.streaming_publication import (
    build_candidate_fts,
    stage_corpus_candidate,
)
from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
from catalyst_data.retrieval.index_manifest import IndexManifest

NOW = "2026-08-01T00:00:00Z"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_dir(root: Path, *, chunk_ids: list[str]) -> Path:
    count = len(chunk_ids)
    vectors = np.zeros((count, BGE_M3_DIMENSION), dtype=np.float32)
    for index in range(count):
        vectors[index, index] = 1.0
    artifact = root / "embedding-artifact"
    artifact.mkdir(parents=True)
    np.save(artifact / "vectors.npy", vectors)
    (artifact / "chunk_ids.json").write_text(json.dumps(chunk_ids), encoding="utf-8")
    # Real GPU artifacts never contain chunks.jsonl; per-row metadata is read
    # from the derivative corpus_build_chunks via source_conn/source_build_id.
    checksums = {
        "vectors.npy": _sha(artifact / "vectors.npy"),
        "chunk_ids.json": _sha(artifact / "chunk_ids.json"),
    }
    (artifact / "checksums.sha256").write_text(
        "\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())) + "\n",
        encoding="utf-8",
    )
    return artifact


def _dense_manifest(
    artifact: Path,
    *,
    vector_count: int,
    source_bundle_id: str,
    corpus_manifest_id: str,
) -> IndexManifest:
    hashes = {
        "vectors.npy": _sha(artifact / "vectors.npy"),
        "chunk_ids.json": _sha(artifact / "chunk_ids.json"),
        "lancedb_table": "c" * 64,
    }
    return IndexManifest(
        model_name=BGE_M3_MODEL,
        model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION,
        normalization_mode="l2",
        dtype="float32",
        dimension=BGE_M3_DIMENSION,
        corpus_manifest_id=corpus_manifest_id,
        source_bundle_id=source_bundle_id,
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
        artifact_hashes=hashes,
        code_revision="a" * 40,
        vector_count=vector_count,
        artifact_state="vectors_staged",
    )


def _promotable(tmp_path: Path, name: str = "promote"):
    """Return (root, conn, active_path, candidate, lexical, dense)."""
    import catalyst_data.index.v1_staging as staging

    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    conn = _fixture_conn(root / "promote.db")
    active_path = root / "active_generation.json"
    _seed_live_pointers(conn, active_path)
    candidate = stage_corpus_candidate(
        conn,
        certified_snapshot_identity=SNAPSHOT,
        profile_versions={"news": "news_v2", "filing": "filing_v3"},
        source_bundle_output_root=root / "bundles",
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
    )
    lexical = build_candidate_fts(conn, build_id=candidate.build_id)
    db_chunk_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT chunk_id FROM corpus_build_chunks WHERE build_id=? "
            "ORDER BY chunk_id COLLATE BINARY",
            (candidate.build_id,),
        ).fetchall()
    ]
    assert len(db_chunk_ids) == candidate.chunk_count
    artifact = _artifact_dir(root, chunk_ids=db_chunk_ids)
    manifest = _dense_manifest(
        artifact,
        vector_count=candidate.chunk_count,
        source_bundle_id=candidate.source_bundle_id,
        corpus_manifest_id=candidate.manifest_id,
    )
    dense = staging.stage_dense(
        root / "lancedb_gold" / candidate.source_bundle_id / "candidates"
        / manifest.index_manifest_id,
        embedding_artifact_dir=artifact,
        new_index_manifest=manifest,
        expected_chunk_count=candidate.chunk_count,
        source_conn=conn,
        source_build_id=candidate.build_id,
    )
    return root, conn, active_path, candidate, lexical, dense


def _promote_kwargs(root: Path, candidate, lexical, dense):
    return dict(
        journal_path=root / "journal.json",
        build_id=candidate.build_id,
        corpus_manifest_id=candidate.manifest_id,
        lexical_digest=lexical.digest,
        dense_candidate=dense,
        active_generation_path=root / "active_generation.json",
    )


def _pointer_state(conn, active_path: Path) -> dict:
    """Semantic pointer snapshot (dense payload parsed, not raw bytes)."""
    snapshot = _pointer_snapshot(conn, active_path)
    snapshot["active"] = (
        json.loads(active_path.read_text(encoding="utf-8"))
        if active_path.exists()
        else None
    )
    return snapshot


def test_promote_api_missing_api():
    import catalyst_data.index as index
    from catalyst_data.index.v1_promote import PromotionResult

    assert callable(index.promote_v1_generation)
    assert callable(index.rollback_v1_generation)
    assert PromotionResult is not None
    assert callable(index.stage_dense)
    assert callable(index.validate_dense_candidate)


def test_happy_path_prepared_sqlite_committed_committed(tmp_path, monkeypatch):
    import catalyst_data.index.v1_promote as vp

    root, conn, active_path, candidate, lexical, dense = _promotable(tmp_path)
    before = _pointer_state(conn, active_path)
    kwargs = _promote_kwargs(root, candidate, lexical, dense)

    states: list[str] = []
    real_write = vp._write_journal

    def capture(path, payload):
        states.append(payload["state"])
        return real_write(path, payload)

    monkeypatch.setattr(vp, "_write_journal", capture)

    result = vp.promote_v1_generation(conn, **kwargs)

    assert result.state == "COMMITTED"
    assert result.admitted is True
    assert result.build_id == candidate.build_id
    assert result.corpus_manifest_id == candidate.manifest_id
    assert result.lexical_generation_id == candidate.build_id
    assert result.dense_index_manifest_id == dense.index_manifest_id
    assert states == ["PREPARED", "SQLITE_COMMITTED", "COMMITTED"]

    journal = json.loads(kwargs["journal_path"].read_text(encoding="utf-8"))
    assert journal["schema_version"] == "m3_promotion_journal_v2"
    assert journal["state"] == "COMMITTED"
    assert journal["promotion_id"]
    assert journal["lexical_generation_id"] == candidate.build_id

    current = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current=1"
    ).fetchone()[0]
    assert current == candidate.manifest_id
    lex = conn.execute(
        "SELECT corpus_manifest_id, lexical_generation_id, lexical_digest "
        "FROM lexical_index_state WHERE singleton_id=1"
    ).fetchone()
    assert tuple(lex) == (candidate.manifest_id, candidate.build_id, lexical.digest)
    status = conn.execute(
        "SELECT status FROM corpus_publication_builds WHERE build_id=?",
        (candidate.build_id,),
    ).fetchone()[0]
    assert status == "published"
    dense_payload = json.loads(active_path.read_text(encoding="utf-8"))
    assert dense_payload["index_manifest_id"] == dense.index_manifest_id
    assert dense_payload["table_name"] == dense.table_name
    assert dense_payload["chunk_count"] == dense.chunk_count
    assert before["manifest"][0][0] != current
    conn.close()


def test_failure_before_pointer_mutation_is_failed(tmp_path, monkeypatch):
    import catalyst_data.index.v1_promote as vp

    root, conn, active_path, candidate, lexical, dense = _promotable(tmp_path)
    before = _pointer_state(conn, active_path)
    kwargs = _promote_kwargs(root, candidate, lexical, dense)

    def explode(conn_, *, build_id, manifest_id, now):
        raise RuntimeError("injected:before-mutation")

    monkeypatch.setattr(vp, "_flip_sqlite_pointers", explode)
    result = vp.promote_v1_generation(conn, **kwargs)

    assert result.state == "FAILED"
    assert result.admitted is False
    journal = json.loads(kwargs["journal_path"].read_text(encoding="utf-8"))
    assert journal["state"] == "FAILED"
    assert _pointer_state(conn, active_path) == before
    conn.close()


def test_failure_after_pointer_mutation_rolls_back(tmp_path, monkeypatch):
    import catalyst_data.index.v1_promote as vp

    root, conn, active_path, candidate, lexical, dense = _promotable(tmp_path)
    before = _pointer_state(conn, active_path)
    kwargs = _promote_kwargs(root, candidate, lexical, dense)

    states: list[str] = []
    real_write = vp._write_journal

    def capture(path, payload):
        states.append(payload["state"])
        return real_write(path, payload)

    monkeypatch.setattr(vp, "_write_journal", capture)

    def explode(path, payload):
        raise RuntimeError("injected:after-mutation")

    monkeypatch.setattr(vp, "_replace_dense_pointer", explode)
    result = vp.promote_v1_generation(conn, **kwargs)

    assert result.state == "ROLLED_BACK"
    assert result.admitted is False
    assert "ROLLING_BACK" in states and "ROLLED_BACK" in states
    journal = json.loads(kwargs["journal_path"].read_text(encoding="utf-8"))
    assert journal["state"] == "ROLLED_BACK"
    assert _pointer_state(conn, active_path) == before
    # live dense file is valid JSON (never truncated) and equals the prior pointer
    assert json.loads(active_path.read_text(encoding="utf-8")) == before["active"]
    conn.close()


def test_sqlite_new_dense_old_is_never_admissible(tmp_path):
    import catalyst_data.index.v1_promote as vp

    root, conn, active_path, candidate, lexical, dense = _promotable(tmp_path)
    before = _pointer_state(conn, active_path)
    kwargs = _promote_kwargs(root, candidate, lexical, dense)
    journal_path = kwargs["journal_path"]

    # Capture the prior lexical row BEFORE the flip.
    prior_lex = conn.execute(
        "SELECT schema_version, corpus_manifest_id, mode_served, fallback_reason, "
        "row_count, built_at, lexical_generation_id, lexical_digest "
        "FROM lexical_index_state WHERE singleton_id=1"
    ).fetchone()
    # Simulate a crash after the SQLite flip but before the dense pointer write:
    # journal says SQLITE_COMMITTED, SQLite shows the new manifest, dense is old.
    vp._flip_sqlite_pointers(
        conn, build_id=candidate.build_id, manifest_id=candidate.manifest_id, now=NOW
    )
    prior_dense = before["active"]
    vp._write_journal(journal_path, {
        "schema_version": "m3_promotion_journal_v2",
        "state": "SQLITE_COMMITTED",
        "promotion_id": "e" * 32,
        "build_id": candidate.build_id,
        "corpus_manifest_id": candidate.manifest_id,
        "lexical_generation_id": candidate.build_id,
        "lexical_digest": lexical.digest,
        "dense_index_manifest_id": dense.index_manifest_id,
        "active_generation_path": str(active_path),
        "created_at": NOW,
        "updated_at": NOW,
        "prior": {
            "corpus_manifest_id": before["manifest"][0][0],
            "corpus_manifest_json": before["manifest"][0][2],
            "build_status": "lexical_ready",
            "lexical_index_state": {
                "schema_version": prior_lex[0],
                "corpus_manifest_id": prior_lex[1],
                "mode_served": prior_lex[2],
                "fallback_reason": prior_lex[3],
                "row_count": prior_lex[4],
                "built_at": prior_lex[5],
                "lexical_generation_id": prior_lex[6],
                "lexical_digest": prior_lex[7],
            },
            "dense_pointer": prior_dense,
            "served_chunk_count": len(before["served"]),
            "fts_row_count": 0,
            "dense_chunk_count": None,
        },
    })

    result = vp.promote_v1_generation(conn, **kwargs)

    assert result.state == "ROLLED_BACK"
    assert result.admitted is False
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "ROLLED_BACK"
    assert _pointer_state(conn, active_path) == before
    conn.close()


def test_rollback_restores_sqlite_in_one_transaction_and_dense_first(tmp_path, monkeypatch):
    import catalyst_data.index.v1_promote as vp

    root, conn, active_path, candidate, lexical, dense = _promotable(tmp_path)
    before = _pointer_state(conn, active_path)
    kwargs = _promote_kwargs(root, candidate, lexical, dense)
    vp.promote_v1_generation(conn, **kwargs)

    sql: list[str] = []
    conn.set_trace_callback(lambda statement: sql.append(statement.strip()))

    order: list[str] = []
    real_dense = vp._restore_dense_pointer
    real_sqlite = vp._restore_sqlite_pointers

    def track_dense(path, payload):
        order.append("dense")
        return real_dense(path, payload)

    def track_sqlite(inner_conn, prior):
        order.append("sqlite")
        return real_sqlite(inner_conn, prior)

    monkeypatch.setattr(vp, "_restore_dense_pointer", track_dense)
    monkeypatch.setattr(vp, "_restore_sqlite_pointers", track_sqlite)

    result = vp.rollback_v1_generation(
        conn, journal_path=kwargs["journal_path"], active_generation_path=active_path
    )

    assert result.state == "ROLLED_BACK"
    assert result.admitted is False
    assert order == ["dense", "sqlite"]
    begins = [s for s in sql if s.upper().startswith("BEGIN IMMEDIATE")]
    commits = [s for s in sql if s.upper() == "COMMIT"]
    rollbacks = [s for s in sql if s.upper() == "ROLLBACK"]
    assert len(begins) == 1
    assert len(commits) == 1
    assert rollbacks == []
    assert _pointer_state(conn, active_path) == before
    conn.close()


def test_prior_verification_mandatory_before_rolled_back(tmp_path, monkeypatch):
    import catalyst_data.index.v1_promote as vp

    root, conn, active_path, candidate, lexical, dense = _promotable(tmp_path)
    kwargs = _promote_kwargs(root, candidate, lexical, dense)
    vp.promote_v1_generation(conn, **kwargs)

    def explode(inner_conn, prior, active_generation_path):
        raise RuntimeError("injected:verify-prior")

    monkeypatch.setattr(vp, "_verify_prior_generation", explode)

    with pytest.raises(RuntimeError, match="injected:verify-prior"):
        vp.rollback_v1_generation(
            conn, journal_path=kwargs["journal_path"], active_generation_path=active_path
        )
    journal = json.loads(kwargs["journal_path"].read_text(encoding="utf-8"))
    assert journal["state"] == "ROLLING_BACK"
    conn.close()


def test_committed_rerun_is_idempotent_noop(tmp_path):
    import catalyst_data.index.v1_promote as vp

    root, conn, active_path, candidate, lexical, dense = _promotable(tmp_path)
    kwargs = _promote_kwargs(root, candidate, lexical, dense)
    first = vp.promote_v1_generation(conn, **kwargs)
    journal_bytes = kwargs["journal_path"].read_bytes()
    snapshot_after_first = _pointer_state(conn, active_path)

    second = vp.promote_v1_generation(conn, **kwargs)

    assert second.state == "COMMITTED"
    assert second.admitted is True
    assert second.promotion_id == first.promotion_id
    assert kwargs["journal_path"].read_bytes() == journal_bytes
    assert _pointer_state(conn, active_path) == snapshot_after_first
    conn.close()


def test_journal_corruption_and_identity_mismatch_fail_closed(tmp_path):
    import catalyst_data.index.v1_promote as vp

    root, conn, active_path, candidate, lexical, dense = _promotable(
        tmp_path, name="corrupt"
    )
    kwargs = _promote_kwargs(root, candidate, lexical, dense)
    kwargs["journal_path"].write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError):
        vp.promote_v1_generation(conn, **kwargs)
    conn.close()

    root, conn, active_path, candidate, lexical, dense = _promotable(
        tmp_path, name="mismatch"
    )
    kwargs = _promote_kwargs(root, candidate, lexical, dense)
    vp.promote_v1_generation(conn, **kwargs)
    bad = dict(kwargs)
    bad["build_id"] = "f" * 64
    with pytest.raises(ValueError):
        vp.promote_v1_generation(conn, **bad)
    conn.close()


def test_active_generation_replacement_is_atomic(tmp_path):
    import catalyst_data.index.v1_promote as vp

    root, conn, active_path, candidate, lexical, dense = _promotable(tmp_path)
    kwargs = _promote_kwargs(root, candidate, lexical, dense)
    vp.promote_v1_generation(conn, **kwargs)
    # No leftover tmp files; live file parses as complete JSON.
    assert not list(active_path.parent.glob("active_generation.json.tmp"))
    payload = json.loads(active_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "active_generation_v1"
    conn.close()
