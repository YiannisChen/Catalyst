"""Q-011 candidate dense staging corrective contracts (RED/GREEN).

These tests lock the fail-closed corrections on top of the original
``test_candidate_staging_cli`` suite:

- B1 mandatory active-path protection on the mutating execute command; execute
  without the active LanceDB dir/pointer is rejected by the parser before any
  write.
- B2 identity chain coherence for a pre-existing ``candidate_generation.json``
  (inactive-only resume; identity disagreement rejects before mutation).
- B3 readiness coherence requires ``status == lexical_ready`` AND
  ``lexical_ready == 1``; contradictory states reject.
- B8 bounded active-index verification: the staged directory receives the
  authoritative ``index_manifest.json`` and active fingerprints stay unchanged.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
from catalyst_data.retrieval.index_manifest import IndexManifest

HEX40 = "a" * 40
REPO_ROOT = Path(__file__).resolve().parents[3]

# Self-contained fixture helpers (mirror the original suite; local copies keep
# the corrective suite independent of pytest package-root layouts).


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _order_checksum(chunk_ids: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(chunk_ids, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _code_revision() -> str:
    from catalyst_data.retrieval.git_revision import resolve_git_revision

    return resolve_git_revision(repo_root=REPO_ROOT, require_clean=False)


def _write_source_bundle(root: Path, records: list[dict]) -> tuple[Path, str]:
    from catalyst_data.retrieval.source_bundle import (
        _chunk_record_hash,
        _stream_source_bundle_id,
    )

    bundle_id = _stream_source_bundle_id(
        corpus_manifest_id="c" * 64,
        snapshot_id="6" * 64,
        probe_report_id="7" * 64,
        postbuild_readiness_id="8" * 64,
        record_hashes=[_chunk_record_hash(record) for record in records],
    )
    bundle = root / f"source_{bundle_id[:16]}"
    bundle.mkdir()
    (bundle / "chunks.jsonl").write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "1.1.0",
        "source_bundle_id": bundle_id,
        "corpus_manifest_id": "c" * 64,
        "snapshot_id": "6" * 64,
        "probe_report_id": "7" * 64,
        "postbuild_readiness_id": "8" * 64,
        "chunk_count": len(records),
        "universe_manifest_id": "u" * 64,
    }
    (bundle / "source_bundle_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    checksums = {
        name: _sha(bundle / name)
        for name in ("chunks.jsonl", "source_bundle_manifest.json")
    }
    (bundle / "checksums.sha256").write_text(
        "\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())) + "\n",
        encoding="utf-8",
    )
    return bundle, bundle_id


def _artifact_dir(tmp_path: Path, *, count: int = 2, code_revision: str | None = None):
    import numpy as np

    if code_revision is None:
        code_revision = _code_revision()
    chunk_ids = [f"chunk:{index:04d}" for index in range(count)]
    vectors = np.zeros((count, BGE_M3_DIMENSION), dtype=np.float32)
    for index in range(count):
        vectors[index, index] = 1.0
    records = []
    texts = [f"fixture {index}" for index in range(count)]
    for chunk_id, text in zip(chunk_ids, texts):
        records.append(
            {
                "chunk_id": chunk_id,
                "document_id": f"doc:{chunk_id}",
                "content_text": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "metadata_hash": "b" * 64,
                "available_at": "2026-08-01T00:00:00Z",
                "ticker_associations": '["AAPL"]',
                "corpus_manifest_id": "c" * 64,
                "chunk_profile_version": "news_v2",
            }
        )
    bundle, bundle_id = _write_source_bundle(tmp_path, records)
    root = tmp_path / "embedding-artifact"
    root.mkdir()
    np.save(root / "vectors.npy", vectors)
    (root / "chunk_ids.json").write_text(
        json.dumps(chunk_ids, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    checksums = {
        "vectors.npy": _sha(root / "vectors.npy"),
        "chunk_ids.json": _sha(root / "chunk_ids.json"),
    }
    manifest = IndexManifest(
        model_name=BGE_M3_MODEL,
        model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION,
        normalization_mode="l2",
        dtype="float32",
        dimension=BGE_M3_DIMENSION,
        corpus_manifest_id="c" * 64,
        source_bundle_id=bundle_id,
        snapshot_id="6" * 64,
        probe_report_id="7" * 64,
        postbuild_readiness_id="8" * 64,
        artifact_hashes={
            "vectors.npy": checksums["vectors.npy"],
            "chunk_ids.json": checksums["chunk_ids.json"],
            "lancedb_table": "c" * 64,
        },
        code_revision=code_revision,
        vector_count=count,
        artifact_state="vectors_staged",
        vectors_checksum=checksums["vectors.npy"],
        chunk_order_checksum=_order_checksum(chunk_ids),
    )
    (root / "index_manifest.json").write_text(
        json.dumps(manifest.to_dict(), sort_keys=True), encoding="utf-8"
    )
    checksums["index_manifest.json"] = _sha(root / "index_manifest.json")
    (root / "checksums.sha256").write_text(
        "\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())) + "\n",
        encoding="utf-8",
    )
    return root, chunk_ids, manifest, bundle


def _derivative(
    tmp_path: Path,
    *,
    manifest: IndexManifest,
    chunk_ids: list[str],
    build_id: str = "b" * 64,
    is_current: int = 0,
    lexical_ready: int = 1,
    status: str = "lexical_ready",
) -> Path:
    db = tmp_path / "derivative.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE corpus_manifest (manifest_id TEXT PRIMARY KEY, "
        "manifest_json TEXT, is_current INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE corpus_publication_builds (build_id TEXT PRIMARY KEY, "
        "manifest_id TEXT NOT NULL, status TEXT NOT NULL, lexical_ready INTEGER "
        "NOT NULL DEFAULT 0, chunk_count INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current) "
        "VALUES (?, ?, ?)",
        (manifest.corpus_manifest_id, json.dumps({}), int(is_current)),
    )
    conn.execute(
        "INSERT INTO corpus_publication_builds "
        "(build_id, manifest_id, status, lexical_ready, chunk_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (build_id, manifest.corpus_manifest_id, status, int(lexical_ready), len(chunk_ids)),
    )
    conn.execute(
        """
        CREATE TABLE corpus_build_chunks (
            build_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL,
            section_key TEXT NOT NULL,
            ordinal TEXT NOT NULL,
            content_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            metadata_hash TEXT NOT NULL,
            source_class TEXT NOT NULL,
            dedup_cluster_id TEXT,
            cluster_first_available_at TEXT,
            representative_document_id TEXT,
            available_at TEXT NOT NULL,
            ticker_associations TEXT NOT NULL,
            eligibility TEXT NOT NULL,
            status TEXT NOT NULL,
            boundary_kind TEXT NOT NULL,
            body_token_start INTEGER NOT NULL,
            body_token_end INTEGER NOT NULL,
            body_overlap_tokens INTEGER NOT NULL,
            prefix_token_count INTEGER NOT NULL,
            prefix_truncated INTEGER NOT NULL,
            section_parse_degraded INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            provider TEXT,
            source_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            canonical_asset_id TEXT,
            content_version_id TEXT,
            corpus_document_id TEXT,
            content_state TEXT,
            independence_group_id TEXT,
            parse_quality TEXT
        )
        """
    )
    texts = ["fixture 0", "fixture 1"]
    for index, chunk_id in enumerate(chunk_ids):
        text = texts[index]
        conn.execute(
            "INSERT INTO corpus_build_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                build_id, chunk_id, f"doc:{chunk_id}", "news_v2", "body", "0001",
                text, hashlib.sha256(text.encode()).hexdigest(), "b" * 64,
                "reported_news" if index % 2 == 0 else "sec_filing",
                f"v1:dedup:cluster{index}", "2026-08-01T00:00:00Z", f"doc:{chunk_id}",
                "2026-08-01T00:00:00Z", '["AAPL"]' if index % 2 == 0 else '["MSFT"]',
                "eligible", "pending_embedding", "served", 0, 10, 10, 0, 0, 0,
                "news" if index % 2 == 0 else "filing", "provider", "type",
                "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z", None, None, None,
                "METADATA_ONLY", None, "not_applicable",
            ),
        )
    conn.commit()
    conn.close()
    return db


def _active_pointers(tmp_path: Path) -> tuple[Path, Path]:
    active_dir = tmp_path / "active_lancedb"
    active_dir.mkdir(parents=True)
    pointer = active_dir / "active_generation.json"
    pointer.write_text(
        json.dumps(
            {
                "schema_version": "active_generation_v1",
                "table_name": "live_table",
                "index_manifest_id": "d" * 64,
            }
        ),
        encoding="utf-8",
    )
    return active_dir, pointer


def _fixture(tmp_path: Path):
    artifact, chunk_ids, manifest, bundle = _artifact_dir(tmp_path)
    db = _derivative(tmp_path, manifest=manifest, chunk_ids=chunk_ids)
    active_dir, pointer = _active_pointers(tmp_path)
    candidate_dir = tmp_path / "candidate_manifest"
    return {
        "artifact": artifact,
        "manifest": manifest,
        "db": db,
        "build_id": "b" * 64,
        "chunk_ids": chunk_ids,
        "active_dir": active_dir,
        "pointer": pointer,
        "candidate_dir": candidate_dir,
        "bundle": bundle,
        "code_revision": manifest.code_revision,
    }


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "stage_candidate_dense.py"
    spec = importlib.util.spec_from_file_location("stage_candidate_dense", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _candidate_generation_payload(manifest: IndexManifest, *, count: int = 2) -> dict:
    return {
        "schema_version": "candidate_generation_v1",
        "status": "inactive",
        "index_manifest_id": manifest.index_manifest_id,
        "source_bundle_id": manifest.source_bundle_id,
        "table_name": f"candidate_{manifest.index_manifest_id[:16]}",
        "chunk_count": count,
        "corpus_manifest_id": manifest.corpus_manifest_id,
        "embedding_model": BGE_M3_MODEL,
        "embedding_revision": BGE_M3_REVISION,
        "embedding_dimension": BGE_M3_DIMENSION,
    }


def _prewrite_candidate(
    fx: dict, *, status: str = "inactive", payload_extra: dict | None = None
) -> Path:
    """Write a candidate_generation.json into the future candidate directory."""
    fx["candidate_dir"].mkdir(parents=True, exist_ok=True)
    payload = _candidate_generation_payload(fx["manifest"])
    payload["status"] = status
    if payload_extra:
        payload.update(payload_extra)
    path = fx["candidate_dir"] / "candidate_generation.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _write_manifest_copy(fx: dict) -> Path:
    """Write a matching index_manifest.json copy next to the generation."""
    fx["candidate_dir"].mkdir(parents=True, exist_ok=True)
    path = fx["candidate_dir"] / "index_manifest.json"
    path.write_text(
        json.dumps(fx["manifest"].to_dict(), sort_keys=True), encoding="utf-8"
    )
    return path


def _argv(mode: str, fx: dict, *, extra: list[str] | None = None) -> list[str]:
    args = [
        mode,
        "--derivative", str(fx["db"]),
        "--build-id", fx["build_id"],
        "--embedding-artifact", str(fx["artifact"]),
        "--candidate-manifest-dir", str(fx["candidate_dir"]),
        "--active-lancedb-dir", str(fx["active_dir"]),
        "--active-generation-pointer", str(fx["pointer"]),
    ]
    if mode == "execute":
        args.extend(
            [
                "--source-bundle", str(fx["bundle"]),
                "--expected-index-manifest-id", fx["manifest"].index_manifest_id,
                "--code-revision", fx["code_revision"],
            ]
        )
    args.extend(extra or [])
    return args


def test_execute_parser_requires_active_generation_arguments(tmp_path):
    """B1: execute without explicit active paths fails in the parser (no writes)."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    argv = [
        "execute",
        "--derivative", str(fx["db"]),
        "--build-id", fx["build_id"],
        "--embedding-artifact", str(fx["artifact"]),
        "--candidate-manifest-dir", str(fx["candidate_dir"]),
    ]
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert not fx["candidate_dir"].exists()
    assert fx["pointer"].read_bytes() == fx["pointer"].read_bytes()


def test_execute_core_rejects_missing_active_guards(tmp_path):
    """B1: core-level execute without explicit active dir/pointer fails closed."""
    from catalyst_data.index.candidate_staging_cli import (
        CandidateDenseStagingInputs,
        execute_stage_candidate_dense,
    )

    fx = _fixture(tmp_path)
    inputs = CandidateDenseStagingInputs(
        derivative=fx["db"],
        build_id=fx["build_id"],
        embedding_artifact_dir=fx["artifact"],
        candidate_manifest_dir=fx["candidate_dir"],
        source_bundle=None,
        active_lancedb_dir=None,
        active_generation_pointer=None,
    )
    with pytest.raises(Exception, match="active-generation protection|--active-lancedb-dir"):
        execute_stage_candidate_dense(inputs)
    assert not fx["candidate_dir"].exists()


def test_preflight_rejects_active_pre_existing_generation(tmp_path):
    """B2: an existing candidate_generation.json with status=active rejects."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    _prewrite_candidate(fx, status="active")
    with pytest.raises(Exception, match="not inactive|pointer-free"):
        cli.main(_argv("preflight", fx))


def test_preflight_rejects_disagreeing_pre_existing_generation(tmp_path):
    """B2: pre-existing candidate identity disagreement rejects before mutation."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    _prewrite_candidate(fx, payload_extra={"chunk_count": 99})
    with pytest.raises(Exception, match="identity disagreement"):
        cli.main(_argv("preflight", fx))
    # candidate dir is untouched beyond the pre-existing file
    assert json.loads((fx["candidate_dir"] / "candidate_generation.json").read_text())[
        "chunk_count"
    ] == 99


def test_execute_rejects_active_corpus_contradiction(tmp_path):
    """B3: is_current=1 always rejects, even when the build claims lexical-ready."""
    cli = _load_cli()
    artifact, chunk_ids, manifest, bundle = _artifact_dir(tmp_path)
    db = _derivative(tmp_path, manifest=manifest, chunk_ids=chunk_ids, is_current=1)
    active_dir, pointer = _active_pointers(tmp_path)
    candidate_dir = tmp_path / "candidate_manifest"
    fx = {
        "db": db,
        "build_id": "b" * 64,
        "artifact": artifact,
        "manifest": manifest,
        "candidate_dir": candidate_dir,
        "active_dir": active_dir,
        "pointer": pointer,
        "bundle": bundle,
        "code_revision": manifest.code_revision,
    }
    with pytest.raises(Exception, match="is_current=1|inactive"):
        cli.main(_argv("execute", fx))
    assert not candidate_dir.exists()


def test_preflight_rejects_readiness_status_flag_disagreement(tmp_path):
    """B3: status=lexical_ready with lexical_ready=0 is incoherent and rejects."""
    cli = _load_cli()
    artifact, chunk_ids, manifest, _bundle = _artifact_dir(tmp_path)
    db = _derivative(
        tmp_path, manifest=manifest, chunk_ids=chunk_ids,
        lexical_ready=0, status="lexical_ready",
    )
    active_dir, pointer = _active_pointers(tmp_path)
    with pytest.raises(Exception, match="lexical-ready|lexical_ready"):
        cli.main([
            "preflight", "--derivative", str(db), "--build-id", "b" * 64,
            "--embedding-artifact", str(artifact), "--candidate-manifest-dir", str(tmp_path / "cand"),
            "--active-lancedb-dir", str(active_dir), "--active-generation-pointer", str(pointer),
        ])


def test_preflight_rejects_readiness_flag_disagreement_reverse(tmp_path):
    """B3: status=reconciliation_ready with lexical_ready=1 is incoherent too."""
    cli = _load_cli()
    artifact, chunk_ids, manifest, _bundle = _artifact_dir(tmp_path)
    db = _derivative(
        tmp_path, manifest=manifest, chunk_ids=chunk_ids,
        lexical_ready=1, status="reconciliation_ready",
    )
    active_dir, pointer = _active_pointers(tmp_path)
    with pytest.raises(Exception, match="lexical-ready|lexical_ready"):
        cli.main([
            "preflight", "--derivative", str(db), "--build-id", "b" * 64,
            "--embedding-artifact", str(artifact), "--candidate-manifest-dir", str(tmp_path / "cand"),
            "--active-lancedb-dir", str(active_dir), "--active-generation-pointer", str(pointer),
        ])


def test_execute_stages_self_describing_candidate_directory(tmp_path):
    """Execute persists an authoritative index_manifest.json next to the generation."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    code = cli.main(_argv("execute", fx))
    assert code == 0
    manifest_copy = fx["candidate_dir"] / "index_manifest.json"
    assert manifest_copy.is_file()
    assert json.loads(manifest_copy.read_text()) == fx["manifest"].to_dict()
    generation = json.loads(
        (fx["candidate_dir"] / "candidate_generation.json").read_text()
    )
    assert generation["index_manifest_id"] == fx["manifest"].index_manifest_id


def test_execute_resume_validates_inactive_coherent_generation(tmp_path):
    """A coherent inactive pre-existing candidate resumes unchanged."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    code = cli.main(_argv("execute", fx))
    assert code == 0
    generation_before = (fx["candidate_dir"] / "candidate_generation.json").read_bytes()
    code2 = cli.main(_argv("execute", fx))
    assert code2 == 0
    assert (fx["candidate_dir"] / "candidate_generation.json").read_bytes() == generation_before


def test_generation_only_half_published_resume_rejected(tmp_path):
    """Item 6: a crash after generation before manifest cannot resume."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    _prewrite_candidate(fx)
    assert not (fx["candidate_dir"] / "index_manifest.json").exists()
    with pytest.raises(Exception, match="index_manifest.json|half-published"):
        cli.main(_argv("preflight", fx))
    with pytest.raises(Exception, match="index_manifest.json|half-published"):
        cli.main(_argv("execute", fx))


def test_manifest_only_half_published_resume_rejected(tmp_path):
    """Item 6: a crash after manifest before generation cannot resume."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    _write_manifest_copy(fx)
    assert not (fx["candidate_dir"] / "candidate_generation.json").exists()
    with pytest.raises(Exception, match="candidate_generation.json|half-published"):
        cli.main(_argv("preflight", fx))
    with pytest.raises(Exception, match="candidate_generation.json|half-published"):
        cli.main(_argv("execute", fx))


def test_half_published_disagreement_rejected(tmp_path):
    """Item 6: resume requires exact manifest/generation identity agreement."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    _prewrite_candidate(fx)
    manifest_copy = _write_manifest_copy(fx)
    manifest = json.loads(manifest_copy.read_text(encoding="utf-8"))
    manifest["vector_count"] = manifest["vector_count"] + 1
    manifest_copy.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(Exception, match="index_manifest.json|identity mismatch|disagreement"):
        cli.main(_argv("preflight", fx))


def test_manifest_written_before_generation_on_publish(tmp_path):
    """Item 6: publication order is manifest first, generation last."""
    from catalyst_data.index import v1_staging
    import catalyst_data.index.v1_staging as v1_mod

    writes: list[str] = []
    real_atomic = v1_mod._atomic_json

    def recording_atomic(path, payload):
        name = Path(path).name
        writes.append(name)
        real_atomic(path, payload)

    fx = _fixture(tmp_path)
    v1_mod._atomic_json = recording_atomic
    try:
        cli = _load_cli()
        code = cli.main(_argv("execute", fx))
    finally:
        v1_mod._atomic_json = real_atomic
    assert code == 0
    assert writes == ["index_manifest.json", "candidate_generation.json"]


def test_symlink_alias_to_active_directory_rejected(tmp_path):
    """B1: candidate path resolving through a symlink to an active dir rejects."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    link_parent = tmp_path / "links"
    link_parent.mkdir()
    link = link_parent / "candidate_link"
    link.symlink_to(fx["active_dir"], target_is_directory=True)
    with pytest.raises(Exception, match="aliases protected active path"):
        cli.main(_argv("preflight", fx, extra=["--candidate-manifest-dir", str(link)]))


def test_execute_parser_requires_source_bundle_manifest_id_and_code_revision(tmp_path):
    """Execute without --source-bundle/--expected-index-manifest-id/--code-revision
    is rejected by the parser before any write."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    base = [
        "execute",
        "--derivative", str(fx["db"]),
        "--build-id", fx["build_id"],
        "--embedding-artifact", str(fx["artifact"]),
        "--candidate-manifest-dir", str(fx["candidate_dir"]),
        "--active-lancedb-dir", str(fx["active_dir"]),
        "--active-generation-pointer", str(fx["pointer"]),
    ]
    with pytest.raises(SystemExit):
        cli.main(base)
    with pytest.raises(SystemExit):
        cli.main(base + ["--source-bundle", str(tmp_path / "bundle")])
    with pytest.raises(SystemExit):
        cli.main(base + ["--expected-index-manifest-id", "a" * 64])
    with pytest.raises(SystemExit):
        cli.main(base + ["--code-revision", "a" * 40])
    assert not fx["candidate_dir"].exists()


def test_execute_core_rejects_missing_source_bundle_manifest_id_and_code_revision(tmp_path):
    """Core-level execute without the three identity guards fails closed."""
    from catalyst_data.index.candidate_staging_cli import (
        CandidateDenseStagingError,
        CandidateDenseStagingInputs,
        execute_stage_candidate_dense,
    )

    fx = _fixture(tmp_path)
    inputs = CandidateDenseStagingInputs(
        derivative=fx["db"],
        build_id=fx["build_id"],
        embedding_artifact_dir=fx["artifact"],
        candidate_manifest_dir=fx["candidate_dir"],
        source_bundle=None,
        active_lancedb_dir=fx["active_dir"],
        active_generation_pointer=fx["pointer"],
        expected_index_manifest_id=None,
        code_revision=None,
    )
    with pytest.raises(
        CandidateDenseStagingError,
        match="source-bundle|expected-index-manifest-id|code-revision",
    ):
        execute_stage_candidate_dense(inputs)
    assert not fx["candidate_dir"].exists()


def test_table_only_interrupted_staging_is_fail_closed_with_operator_cleanup(tmp_path):
    """A leftover candidate_*.lance table without published identity records
    remains fail-closed; the error names the operator cleanup/restart procedure.
    """
    cli = _load_cli()
    fx = _fixture(tmp_path)
    table_name = f"candidate_{fx['manifest'].index_manifest_id[:16]}"
    table_dir = fx["candidate_dir"] / f"{table_name}.lance"
    table_dir.mkdir(parents=True)
    (table_dir / "partial").write_text("interrupted", encoding="utf-8")
    with pytest.raises(Exception, match="Operator cleanup"):
        cli.main(_argv("preflight", fx))
    with pytest.raises(Exception, match="identical execute command"):
        cli.main(_argv("execute", fx))
    assert (fx["candidate_dir"] / "candidate_generation.json").exists() is False


def _assert_no_candidate_lancedb_mutation(fx: dict) -> None:
    candidate = fx["candidate_dir"]
    if candidate.exists():
        assert list(candidate.glob("*.lance")) == []
        assert not (candidate / "candidate_generation.json").exists()


def _rebuild_self_consistent_artifact(
    artifact: Path, chunk_ids: list[str], *, template: IndexManifest
) -> IndexManifest:
    """Rewrite vectors/chunk_ids/manifest/checksums for a new ID sequence.

    The rewritten artifact is internally consistent and keeps the original
    source-bundle identity so only the ordered chunk sequence disagrees.
    """
    import numpy as np

    chunks_jsonl = artifact / "chunks.jsonl"
    if chunks_jsonl.exists():
        chunks_jsonl.unlink()
    count = len(chunk_ids)
    vectors = np.zeros((count, BGE_M3_DIMENSION), dtype=np.float32)
    for index in range(count):
        vectors[index, index] = 1.0
    np.save(artifact / "vectors.npy", vectors)
    (artifact / "chunk_ids.json").write_text(
        json.dumps(chunk_ids, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    checksums = {
        "vectors.npy": _sha(artifact / "vectors.npy"),
        "chunk_ids.json": _sha(artifact / "chunk_ids.json"),
    }
    manifest = IndexManifest(
        model_name=template.model_name,
        model_revision=template.model_revision,
        tokenizer_revision=template.tokenizer_revision,
        normalization_mode=template.normalization_mode,
        dtype=template.dtype,
        dimension=template.dimension,
        corpus_manifest_id=template.corpus_manifest_id,
        source_bundle_id=template.source_bundle_id,
        snapshot_id=template.snapshot_id,
        probe_report_id=template.probe_report_id,
        postbuild_readiness_id=template.postbuild_readiness_id,
        artifact_hashes={
            "vectors.npy": checksums["vectors.npy"],
            "chunk_ids.json": checksums["chunk_ids.json"],
            "lancedb_table": template.artifact_hashes["lancedb_table"],
        },
        code_revision=template.code_revision,
        vector_count=count,
        artifact_state="vectors_staged",
        vectors_checksum=checksums["vectors.npy"],
        chunk_order_checksum=_order_checksum(chunk_ids),
    )
    (artifact / "index_manifest.json").write_text(
        json.dumps(manifest.to_dict(), sort_keys=True), encoding="utf-8"
    )
    checksums["index_manifest.json"] = _sha(artifact / "index_manifest.json")
    (artifact / "checksums.sha256").write_text(
        "\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())) + "\n",
        encoding="utf-8",
    )
    return manifest


def test_execute_rejects_equal_count_different_chunk_ids_before_lancedb(tmp_path):
    """Source bundle and embedding artifact share count but not chunk IDs."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    new_ids = ["chunk:0000", "chunk:9999"]
    fx["manifest"] = _rebuild_self_consistent_artifact(
        fx["artifact"], new_ids, template=fx["manifest"]
    )
    alt = tmp_path / "wrong-ids"
    alt.mkdir()
    fx["db"] = _derivative(alt, manifest=fx["manifest"], chunk_ids=new_ids)
    with pytest.raises(Exception, match="frozen order|chunk-id"):
        cli.main(_argv("execute", fx))
    _assert_no_candidate_lancedb_mutation(fx)


def test_execute_rejects_reordered_chunk_ids_before_lancedb(tmp_path):
    cli = _load_cli()
    fx = _fixture(tmp_path)
    new_ids = list(reversed(fx["chunk_ids"]))
    fx["manifest"] = _rebuild_self_consistent_artifact(
        fx["artifact"], new_ids, template=fx["manifest"]
    )
    alt = tmp_path / "reordered-ids"
    alt.mkdir()
    fx["db"] = _derivative(alt, manifest=fx["manifest"], chunk_ids=new_ids)
    with pytest.raises(Exception, match="frozen order|chunk-id|order"):
        cli.main(_argv("execute", fx))
    _assert_no_candidate_lancedb_mutation(fx)


def test_execute_rejects_self_consistent_artifact_for_wrong_bundle_sequence(tmp_path):
    """A fully recomputed artifact for the wrong IDs is still rejected."""
    cli = _load_cli()
    fx = _fixture(tmp_path)
    new_ids = ["chunk:aaaa", "chunk:bbbb"]
    fx["manifest"] = _rebuild_self_consistent_artifact(
        fx["artifact"], new_ids, template=fx["manifest"]
    )
    alt = tmp_path / "wrong-seq"
    alt.mkdir()
    fx["db"] = _derivative(alt, manifest=fx["manifest"], chunk_ids=new_ids)
    with pytest.raises(Exception, match="frozen order|chunk-id"):
        cli.main(_argv("execute", fx))
    _assert_no_candidate_lancedb_mutation(fx)


def test_execute_rejects_validated_manifest_id_disagreeing_with_expected(tmp_path):
    cli = _load_cli()
    fx = _fixture(tmp_path)
    argv = _argv("execute", fx)
    idx = argv.index("--expected-index-manifest-id")
    argv[idx + 1] = "a" * 64
    with pytest.raises(Exception, match="index manifest id mismatch|expected"):
        cli.main(argv)
    _assert_no_candidate_lancedb_mutation(fx)


def test_execute_rejects_non_production_mock_artifact_before_lancedb(tmp_path):
    cli = _load_cli()
    fx = _fixture(tmp_path)
    manifest_path = fx["artifact"] / "index_manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["non_production"] = True
    manifest_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    checksums = {
        "vectors.npy": _sha(fx["artifact"] / "vectors.npy"),
        "chunk_ids.json": _sha(fx["artifact"] / "chunk_ids.json"),
        "index_manifest.json": _sha(manifest_path),
    }
    (fx["artifact"] / "checksums.sha256").write_text(
        "\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="non_production"):
        cli.main(_argv("execute", fx))
    _assert_no_candidate_lancedb_mutation(fx)
