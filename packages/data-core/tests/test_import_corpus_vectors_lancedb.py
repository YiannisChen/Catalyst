"""Production operator CLI contract for the LanceDB corpus import (Task 3).

Preflight is strictly zero-write; execute writes through a unique staging
table and an atomic active-generation pointer; resume continues from a
preserved checkpoint. No --force or identity-bypass argument exists.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

CODE_REVISION = "04dabde9f44e9638d1b5ebad0eed002f5475c1a3"

CORPUS = "c" * 64
SNAPSHOT = "6" * 64
PROBE = "7" * 64
POSTBUILD = "8" * 64


def _bundle(tmp_path: Path, count: int = 4) -> tuple[Path, dict]:
    from catalyst_data.retrieval.source_bundle import _chunk_record_hash, _stream_source_bundle_id

    records = []
    for index in range(count):
        text = f"fixture content {index} — 中文"
        records.append({
            "chunk_id": f"chunk:{index:04d}", "document_id": f"doc:{index}",
            "content_text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "metadata_hash": hashlib.sha256(f"meta:{index}".encode()).hexdigest(),
            "available_at": "2026-01-01T00:00:00Z",
            "ticker_associations": "[\"AAPL\"]", "corpus_manifest_id": CORPUS,
            "chunk_profile_version": "news_v2",
        })
    identity = _stream_source_bundle_id(
        corpus_manifest_id=CORPUS, snapshot_id=SNAPSHOT,
        probe_report_id=PROBE, postbuild_readiness_id=POSTBUILD,
        record_hashes=[_chunk_record_hash(r) for r in records],
    )
    root = tmp_path / f"source_{identity}"
    root.mkdir(parents=True)
    (root / "chunks.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records)
    )
    manifest = {
        "schema_version": "1.1.0", "source_bundle_id": identity,
        "corpus_manifest_id": CORPUS, "snapshot_id": SNAPSHOT,
        "probe_report_id": PROBE, "postbuild_readiness_id": POSTBUILD,
        "chunk_count": count, "universe_manifest_id": "u" * 64,
    }
    (root / "source_bundle_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    )
    checksums = {}
    for name in ("chunks.jsonl", "source_bundle_manifest.json"):
        checksums[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    (root / "checksums.sha256").write_text(
        "\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())) + "\n"
    )
    return root, manifest


def _artifact(tmp_path: Path, bundle: Path, count: int = 4, *, shard_size: int = 2) -> Path:
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    artifact = tmp_path / "artifact"
    build_embedding_artifacts(
        bundle, artifact,
        embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        batch_size=32, shard_size=shard_size, mock=False, code_revision=CODE_REVISION,
    )
    return artifact


def _db_file(tmp_path: Path, count: int = 4, *, with_view: bool = True) -> Path:
    path = tmp_path / "candidate.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE corpus_manifest ("
        " manifest_id TEXT PRIMARY KEY, is_current INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO corpus_manifest (manifest_id, is_current) VALUES (?, 1)",
        (CORPUS,),
    )
    conn.execute(
        """CREATE TABLE corpus_chunks (
             chunk_id TEXT, document_id TEXT, content_text TEXT,
             content_hash TEXT, metadata_hash TEXT, available_at TEXT,
             ticker_associations TEXT, source_class TEXT, chunk_profile_version TEXT,
             status TEXT, eligibility TEXT, manifest_id TEXT,
             dedup_cluster_id TEXT, cluster_first_available_at TEXT,
             representative_document_id TEXT
           )"""
    )
    for index in range(count):
        text = f"fixture content {index} — 中文"
        conn.execute(
            """INSERT INTO corpus_chunks (
                 chunk_id, document_id, content_text, content_hash, metadata_hash,
                 available_at, ticker_associations, source_class,
                 chunk_profile_version, status, eligibility, manifest_id,
                 dedup_cluster_id, cluster_first_available_at,
                 representative_document_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"chunk:{index:04d}", f"doc:{index}", text,
                hashlib.sha256(text.encode()).hexdigest(),
                hashlib.sha256(f"meta:{index}".encode()).hexdigest(),
                "2026-01-01T00:00:00Z", json.dumps(["AAPL"]), "reported_news",
                "news_v2", "active", "eligible", CORPUS,
                f"cluster:{index}", "2026-01-01T00:00:00Z", f"doc:{index}",
            ),
        )
    if with_view:
        conn.execute(
            """CREATE VIEW corpus_served_chunks AS
                 SELECT chunk_id, document_id, content_text, content_hash,
                        metadata_hash, available_at, ticker_associations,
                        source_class, chunk_profile_version, status, eligibility,
                        manifest_id, dedup_cluster_id, cluster_first_available_at,
                        representative_document_id
                 FROM corpus_chunks"""
        )
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()
    return path


def _pointer(tmp_path: Path, db_path: Path) -> Path:
    pointer = tmp_path / "active_data_snapshot.json"
    pointer.write_text(json.dumps({
        "schema_version": "1.0.0",
        "db_path": str(db_path.resolve()),
        "sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
        "snapshot_id": SNAPSHOT,
    }))
    return pointer


def _args(
    pointer: Path,
    bundle: Path,
    artifact: Path,
    lancedb_dir: Path,
    active_pointer: Path,
    bundle_id: str,
    extra: list[str] | None = None,
) -> list[str]:
    pointer_payload = json.loads(pointer.read_text())
    db_sha = hashlib.sha256(Path(pointer_payload["db_path"]).read_bytes()).hexdigest()
    values = [
        "--active-snapshot-pointer", str(pointer),
        "--source-bundle", str(bundle),
        "--embedding-artifact", str(artifact),
        "--lancedb-dir", str(lancedb_dir),
        "--active-generation-pointer", str(active_pointer),
        "--expected-source-bundle-id", bundle_id,
        "--expected-snapshot-id", SNAPSHOT,
        "--expected-corpus-manifest-id", CORPUS,
        "--expected-probe-report-id", PROBE,
        "--expected-postbuild-readiness-id", POSTBUILD,
        "--expected-db-sha", db_sha,
        "--code-revision", CODE_REVISION,
    ]
    if extra:
        values.extend(extra)
    return values


def _cli():
    import scripts.import_corpus_vectors_lancedb as cli

    return cli


def _table_names(db) -> list[str]:
    if hasattr(db, "list_tables"):
        return list(db.list_tables().tables)
    return list(db.table_names())


def _run_preflight(monkeypatch, tmp_path, count=4):
    bundle, manifest = _bundle(tmp_path, count)
    artifact = _artifact(tmp_path / "build", bundle, count)
    db_path = _db_file(tmp_path, count)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args = _args(
        pointer, bundle, artifact, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    return cli, args, bundle, artifact, db_path, pointer, manifest


def test_preflight_is_zero_write_and_prints_verified_report(monkeypatch, tmp_path, capsys):
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)
    rc = cli.main(["preflight", *args])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["chunk_count"] == 4
    assert report["db"]["user_version"] == 13
    assert report["db"]["integrity"] == "ok"
    assert report["db"]["foreign_key_violations"] == 0
    assert report["metadata"]["count"] == 4
    # zero-write: no lancedb dir, no checkpoint, no report, no staging state
    assert not (tmp_path / "lance").exists()
    assert not (tmp_path / "active_generation.json").exists()
    assert not list(tmp_path.glob("import_report.json"))
    assert not list(tmp_path.glob("staging_state.json"))
    # inputs unchanged
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == report["snapshot_pointer"]["sha256"]


def test_preflight_rejects_db_sha_mismatch(monkeypatch, tmp_path):
    cli, args, *_ = _run_preflight(monkeypatch, tmp_path)
    bad_args = list(args)
    index = bad_args.index("--expected-db-sha")
    bad_args[index + 1] = "0" * 64
    with pytest.raises(ValueError, match="SHA mismatch"):
        cli.main(["preflight", *bad_args])


def test_preflight_rejects_current_manifest_mismatch(monkeypatch, tmp_path):
    cli, args, *_ = _run_preflight(monkeypatch, tmp_path)
    bad_args = list(args)
    index = bad_args.index("--expected-corpus-manifest-id")
    bad_args[index + 1] = "b" * 64
    with pytest.raises(ValueError, match="manifest"):
        cli.main(["preflight", *bad_args])


def test_preflight_rejects_batch_size_over_500(monkeypatch, tmp_path):
    cli, args, *_ = _run_preflight(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["preflight", *args, "--import-batch-size", "501"])


def test_cli_has_no_force_or_bypass_argument(monkeypatch, tmp_path):
    cli, args, *_ = _run_preflight(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["preflight", *args, "--force"])


def test_execute_imports_staging_validates_and_writes_active_pointer(monkeypatch, tmp_path):
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)
    rc = cli.main(["execute", *args])
    assert rc == 0

    active_pointer = tmp_path / "active_generation.json"
    assert active_pointer.is_file()
    pointer_payload = json.loads(active_pointer.read_text())
    assert pointer_payload["chunk_count"] == 4
    assert pointer_payload["source_bundle_id"] == manifest["source_bundle_id"]
    assert pointer_payload["corpus_manifest_id"] == CORPUS

    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    table = db.open_table(pointer_payload["table_name"])
    assert table.count_rows() == 4
    assert table.name.startswith("chunks__staging__")

    report = json.loads((tmp_path / "lance" / "import_report.json").read_text())
    assert report["ok"] is True
    assert report["table_name"] == pointer_payload["table_name"]
    assert not any(
        key in report for key in ("api_key", "token", "password", "secret", "credential")
    )
    # checkpoint and staging state are cleaned up on success
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()
    assert not (tmp_path / "lance" / "staging_state.json").exists()


def test_execute_dense_filtered_query_over_real_lancedb(monkeypatch, tmp_path):
    from catalyst_data.retrieval.dense import retrieve_dense

    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)
    assert cli.main(["execute", *args]) == 0
    import lancedb

    pointer_payload = json.loads((tmp_path / "active_generation.json").read_text())
    table = lancedb.connect(str(tmp_path / "lance")).open_table(pointer_payload["table_name"])
    result = retrieve_dense(
        None,
        np.ones(1024, dtype=np.float32),
        ticker="AAPL",
        cutoff="2026-12-31T23:59:59Z",
        requested_manifest_id=CORPUS,
        index_manifest_id=pointer_payload["index_manifest_id"],
        lancedb_table=table,
    )
    assert [item.chunk_id for item in result.results] == [
        "chunk:0000", "chunk:0001", "chunk:0002", "chunk:0003",
    ]
    assert result.results[0].dedup_cluster_id == "cluster:0"


def test_execute_resume_after_keyboard_interrupt(monkeypatch, tmp_path):
    count = 1500
    bundle, manifest = _bundle(tmp_path, count)
    artifact = _artifact(tmp_path / "build", bundle, count, shard_size=512)
    db_path = _db_file(tmp_path, count)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args = _args(
        pointer, bundle, artifact, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )

    import catalyst_data.retrieval.import_metadata as metadata_mod

    interrupt_state = {"armed": True}

    def interrupt_first_import(conn, *, corpus_manifest_id, page_size=500):
        source = metadata_mod.iter_import_metadata(conn, corpus_manifest_id=corpus_manifest_id, page_size=page_size)
        for index, row in enumerate(source):
            if interrupt_state["armed"] and index >= 1000:
                interrupt_state["armed"] = False
                raise KeyboardInterrupt("simulated operator interrupt")
            yield row

    monkeypatch.setattr(cli, "iter_import_metadata", interrupt_first_import)

    try:
        cli.main(["execute", *args])
        raise AssertionError("expected KeyboardInterrupt")
    except KeyboardInterrupt:
        pass
    # staging + checkpoint preserved
    assert (tmp_path / "lance" / "import_checkpoint.json").is_file()
    assert (tmp_path / "lance" / "staging_state.json").is_file()
    assert not (tmp_path / "active_generation.json").exists()
    import lancedb

    interrupted = lancedb.connect(str(tmp_path / "lance"))
    staging_name = json.loads((tmp_path / "lance" / "staging_state.json").read_text())["table_name"]
    assert interrupted.open_table(staging_name).count_rows() == 1000

    # resume completes the import
    rc = cli.main(["execute", "--resume", *args])
    assert rc == 0
    assert json.loads((tmp_path / "active_generation.json").read_text())["chunk_count"] == count
    assert interrupted.open_table(staging_name).count_rows() == count
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()
    assert not (tmp_path / "lance" / "staging_state.json").exists()
def test_execute_ordinary_failure_drops_staging_and_preserves_pointer(monkeypatch, tmp_path):
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)

    # establish an existing active pointer that must stay byte-identical
    existing_pointer = tmp_path / "active_generation.json"
    existing_pointer.write_bytes(b'{"schema_version":"active_generation_v1","table_name":"chunks__staging__active","chunk_count":999}')
    pointer_before = existing_pointer.read_bytes()
    manifest_before = (artifact / "index_manifest.json").read_bytes()
    checksums_before = (artifact / "checksums.sha256").read_bytes()

    import catalyst_data.retrieval.import_metadata as metadata_mod

    def fail_after_two(conn, *, corpus_manifest_id, page_size=500):
        source = metadata_mod.iter_import_metadata(conn, corpus_manifest_id=corpus_manifest_id, page_size=page_size)
        for index, row in enumerate(source):
            if index >= 2:
                raise RuntimeError("injected import failure")
            yield row

    monkeypatch.setattr(cli, "iter_import_metadata", fail_after_two)

    with pytest.raises(RuntimeError, match="injected import failure"):
        cli.main(["execute", *args])
    # active pointer bytes never change
    assert existing_pointer.read_bytes() == pointer_before
    # the newly created staging table is dropped, not left empty
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    assert _table_names(db) == []
    # checkpoint and staging_state are removed
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()
    assert not (tmp_path / "lance" / "staging_state.json").exists()
    # embedding artifact is restored to its pre-import state
    assert (artifact / "index_manifest.json").read_bytes() == manifest_before
    assert (artifact / "checksums.sha256").read_bytes() == checksums_before


def test_ordinary_failure_drops_only_current_staging_table_and_preserves_active(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    artifact_a = _artifact(tmp_path / "build_a", bundle, 4)
    artifact_b = _artifact(tmp_path / "build_b", bundle, 4)
    db_path = _db_file(tmp_path, 4)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args_a = _args(
        pointer, bundle, artifact_a, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    args_b = _args(
        pointer, bundle, artifact_b, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    assert cli.main(["execute", *args_a]) == 0
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    active_pointer_path = tmp_path / "active_generation.json"
    active_name = json.loads(active_pointer_path.read_text())["table_name"]
    pointer_before = active_pointer_path.read_bytes()
    assert db.open_table(active_name).count_rows() == 4

    import catalyst_data.retrieval.import_metadata as metadata_mod

    def fail_after_two(conn, *, corpus_manifest_id, page_size=500):
        source = metadata_mod.iter_import_metadata(conn, corpus_manifest_id=corpus_manifest_id, page_size=page_size)
        for index, row in enumerate(source):
            if index >= 2:
                raise RuntimeError("injected second-run failure")
            yield row

    monkeypatch.setattr(cli, "iter_import_metadata", fail_after_two)
    with pytest.raises(RuntimeError, match="injected second-run failure"):
        cli.main(["execute", *args_b])

    # only the failed current staging table is dropped; the active table and
    # pointer bytes survive untouched
    assert _table_names(db) == [active_name]
    assert db.open_table(active_name).count_rows() == 4
    assert active_pointer_path.read_bytes() == pointer_before
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()
    assert not (tmp_path / "lance" / "staging_state.json").exists()


def test_repeated_ordinary_failures_do_not_accumulate_staging_tables(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    artifact_1 = _artifact(tmp_path / "build_1", bundle, 4)
    artifact_2 = _artifact(tmp_path / "build_2", bundle, 4)
    db_path = _db_file(tmp_path, 4)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args_1 = _args(
        pointer, bundle, artifact_1, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    args_2 = _args(
        pointer, bundle, artifact_2, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    import catalyst_data.retrieval.import_metadata as metadata_mod

    fail_state = {"armed": True}

    def fail_first_import(conn, *, corpus_manifest_id, page_size=500):
        source = metadata_mod.iter_import_metadata(conn, corpus_manifest_id=corpus_manifest_id, page_size=page_size)
        for index, row in enumerate(source):
            if fail_state["armed"] and index >= 2:
                fail_state["armed"] = False
                raise RuntimeError("injected repeated failure")
            yield row

    monkeypatch.setattr(cli, "iter_import_metadata", fail_first_import)
    with pytest.raises(RuntimeError, match="injected repeated failure"):
        cli.main(["execute", *args_1])
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    assert _table_names(db) == []
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()
    assert not (tmp_path / "lance" / "staging_state.json").exists()

    # second ordinary failure must also leave no staging table behind
    monkeypatch.setattr(cli, "iter_import_metadata", lambda conn, **kwargs: (_ for _ in ()).throw(RuntimeError("second injected failure")))
    with pytest.raises(RuntimeError, match="second injected failure"):
        cli.main(["execute", *args_2])
    assert _table_names(db) == []
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()
    assert not (tmp_path / "lance" / "staging_state.json").exists()


def test_execute_never_creates_second_staging_over_active(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    artifact_a = _artifact(tmp_path / "build_a", bundle, 4)
    artifact_b = _artifact(tmp_path / "build_b", bundle, 4)
    db_path = _db_file(tmp_path, 4)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args_a = _args(
        pointer, bundle, artifact_a, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    args_b = _args(
        pointer, bundle, artifact_b, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    assert cli.main(["execute", *args_a]) == 0
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    names = [name for name in _table_names(db) if name.startswith("chunks__staging__")]
    assert len(names) == 1
    # second execute creates a new staging table, keeps the active pointer table intact
    assert cli.main(["execute", *args_b]) == 0
    names = [name for name in _table_names(db) if name.startswith("chunks__staging__")]
    assert len(names) == 2
    active = json.loads((tmp_path / "active_generation.json").read_text())
    assert db.open_table(active["table_name"]).count_rows() == 4


def test_execute_report_is_atomic_json_with_expected_identities(monkeypatch, tmp_path):
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)
    assert cli.main(["execute", *args]) == 0
    report = json.loads((tmp_path / "lance" / "import_report.json").read_text())
    assert report["schema_version"] == "lancedb_import_report_v1"
    assert report["source_bundle_id"] == manifest["source_bundle_id"]
    assert report["snapshot_id"] == SNAPSHOT
    assert report["corpus_manifest_id"] == CORPUS
    assert report["probe_report_id"] == PROBE
    assert report["postbuild_readiness_id"] == POSTBUILD
    assert report["import_batch_size"] == 500


def _open_db_for_fixture(db_path: Path):
    return sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)


# ---------------------------------------------------------------------------
# Task 1 (Pre-GPU safety): active-table deletion protection
# ---------------------------------------------------------------------------


def _state_payload(artifact: Path, manifest: dict, table_name: str, **overrides) -> dict:
    from catalyst_data.retrieval.index_manifest import IndexManifest

    raw = json.loads((artifact / "index_manifest.json").read_text())
    index_manifest = IndexManifest.from_dict(
        {k: v for k, v in raw.items() if k != "non_production"}
    )
    payload = {
        "schema_version": "lancedb_staging_state_v1",
        "table_name": table_name,
        "source_bundle_id": manifest["source_bundle_id"],
        "snapshot_id": SNAPSHOT,
        "corpus_manifest_id": CORPUS,
        "index_manifest_id": index_manifest.index_manifest_id,
        "code_revision": CODE_REVISION,
        "artifact_checksum": hashlib.sha256(
            (artifact / "checksums.sha256").read_bytes()
        ).hexdigest(),
    }
    payload.update(overrides)
    return payload


def test_corrupted_state_pointing_to_active_table_rejected_before_writes(monkeypatch, tmp_path):
    import lancedb

    from catalyst_data.retrieval.gpu_contract import production_lancedb_schema

    bundle, manifest = _bundle(tmp_path, 4)
    artifact = _artifact(tmp_path / "build", bundle, 4)
    db_path = _db_file(tmp_path, 4)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args = _args(
        pointer, bundle, artifact, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    # simulate an existing active generation + a corrupt staging_state that
    # points at the active table
    db = lancedb.connect(str(tmp_path / "lance"))
    active_table = db.create_table(
        "chunks__staging__active", data=[], schema=production_lancedb_schema(),
    )
    (tmp_path / "active_generation.json").write_text(json.dumps({
        "schema_version": "active_generation_v1",
        "table_name": active_table.name,
        "index_manifest_id": "1" * 64,
        "chunk_count": 4,
    }))
    corrupt_state = _state_payload(artifact, manifest, active_table.name)
    state_path = tmp_path / "lance" / "staging_state.json"
    state_path.write_text(json.dumps(corrupt_state))
    state_bytes_before = state_path.read_bytes()
    tables_before = sorted(_table_names(db))

    with pytest.raises(ValueError, match="active"):
        cli.main(["execute", "--resume", *args])
    # no writes happened: state untouched, no new tables, checkpoint untouched
    assert state_path.read_bytes() == state_bytes_before
    assert sorted(_table_names(db)) == tables_before
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()


def test_drop_staging_table_refuses_active_table(tmp_path):
    import lancedb

    from catalyst_data.retrieval.gpu_contract import production_lancedb_schema

    cli = _cli()
    lance_dir = tmp_path / "lance"
    db = lancedb.connect(str(lance_dir))
    db.create_table("chunks__staging__active", data=[], schema=production_lancedb_schema())
    pointer = tmp_path / "active_generation.json"
    pointer.write_text(json.dumps({
        "schema_version": "active_generation_v1",
        "table_name": "chunks__staging__active",
        "index_manifest_id": "1" * 64,
        "chunk_count": 4,
    }))
    with pytest.raises(ValueError, match="refusing to drop active"):
        cli._drop_staging_table(lance_dir, "chunks__staging__active", pointer)
    assert "chunks__staging__active" in _table_names(db)


def test_active_pointer_changing_between_load_and_cleanup_fails_closed(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    artifact_a = _artifact(tmp_path / "build_a", bundle, 4)
    artifact_b = _artifact(tmp_path / "build_b", bundle, 4)
    db_path = _db_file(tmp_path, 4)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args_a = _args(
        pointer, bundle, artifact_a, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    args_b = _args(
        pointer, bundle, artifact_b, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    assert cli.main(["execute", *args_a]) == 0
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    active_pointer_path = tmp_path / "active_generation.json"
    active_name = json.loads(active_pointer_path.read_text())["table_name"]

    import catalyst_data.retrieval.import_metadata as metadata_mod

    def fail_after_two(conn, *, corpus_manifest_id, page_size=500):
        source = metadata_mod.iter_import_metadata(conn, corpus_manifest_id=corpus_manifest_id, page_size=page_size)
        for index, row in enumerate(source):
            if index >= 2:
                raise RuntimeError("injected concurrency failure")
            yield row

    monkeypatch.setattr(cli, "iter_import_metadata", fail_after_two)
    real_drop = cli._drop_staging_table

    def changing_drop(lancedb_dir, table_name, active_pointer_path):
        # simulate concurrent switch: active pointer now points at our staging table
        active_pointer_path.write_text(json.dumps({
            "schema_version": "active_generation_v1",
            "table_name": table_name,
            "index_manifest_id": "1" * 64,
            "chunk_count": 999,
        }))
        return real_drop(lancedb_dir, table_name, active_pointer_path)

    monkeypatch.setattr(cli, "_drop_staging_table", changing_drop)
    with pytest.raises(RuntimeError, match="injected concurrency failure") as excinfo:
        cli.main(["execute", *args_b])
    notes = list(getattr(excinfo.value, "__notes__", []) or [])
    assert any("refusing to drop active" in note for note in notes)
    # staging table was NOT dropped under a concurrent active pointer
    staging_tables = [
        name for name in _table_names(db)
        if name.startswith("chunks__staging__") and name != active_name
    ]
    assert len(staging_tables) == 1
    assert active_name in _table_names(db)


def test_state_identity_mismatch_rejects_resume(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    artifact = _artifact(tmp_path / "build", bundle, 4)
    db_path = _db_file(tmp_path, 4)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args = _args(
        pointer, bundle, artifact, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    state_path = tmp_path / "lance" / "staging_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    mismatched = _state_payload(
        artifact, manifest, "chunks__staging__ghost",
        source_bundle_id="0" * 64,
    )
    state_path.write_text(json.dumps(mismatched))
    state_bytes_before = state_path.read_bytes()

    with pytest.raises(ValueError, match="source_bundle_id"):
        cli.main(["execute", "--resume", *args])
    assert state_path.read_bytes() == state_bytes_before
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    assert _table_names(db) == []


def test_resume_after_completion_is_rejected_idempotently(monkeypatch, tmp_path):
    count = 1500
    bundle, manifest = _bundle(tmp_path, count)
    artifact = _artifact(tmp_path / "build", bundle, count, shard_size=512)
    db_path = _db_file(tmp_path, count)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args = _args(
        pointer, bundle, artifact, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    import catalyst_data.retrieval.import_metadata as metadata_mod

    interrupt_state = {"armed": True}

    def interrupt_first_import(conn, *, corpus_manifest_id, page_size=500):
        source = metadata_mod.iter_import_metadata(conn, corpus_manifest_id=corpus_manifest_id, page_size=page_size)
        for index, row in enumerate(source):
            if interrupt_state["armed"] and index >= 1000:
                interrupt_state["armed"] = False
                raise KeyboardInterrupt("simulated operator interrupt")
            yield row

    monkeypatch.setattr(cli, "iter_import_metadata", interrupt_first_import)
    try:
        cli.main(["execute", *args])
        raise AssertionError("expected KeyboardInterrupt")
    except KeyboardInterrupt:
        pass
    assert (tmp_path / "lance" / "staging_state.json").is_file()
    assert (tmp_path / "lance" / "import_checkpoint.json").is_file()

    assert cli.main(["execute", "--resume", *args]) == 0
    assert json.loads((tmp_path / "active_generation.json").read_text())["chunk_count"] == count
    assert not (tmp_path / "lance" / "staging_state.json").exists()
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()

    # a second resume is rejected cleanly (artifact consumed / no state left);
    # no duplicate import may occur
    with pytest.raises(ValueError):
        cli.main(["execute", "--resume", *args])
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    active_name = json.loads((tmp_path / "active_generation.json").read_text())["table_name"]
    assert sorted(_table_names(db)) == [active_name]
    assert db.open_table(active_name).count_rows() == count


def test_cleanup_error_preserves_original_exception(monkeypatch, tmp_path):
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)

    import catalyst_data.retrieval.import_metadata as metadata_mod

    def fail_first_import(conn, *, corpus_manifest_id, page_size=500):
        source = metadata_mod.iter_import_metadata(conn, corpus_manifest_id=corpus_manifest_id, page_size=page_size)
        for index, row in enumerate(source):
            if index >= 2:
                raise RuntimeError("injected import failure")
            yield row

    monkeypatch.setattr(cli, "iter_import_metadata", fail_first_import)

    def boom_drop(lancedb_dir, table_name, active_pointer_path):
        raise RuntimeError("cleanup boom")

    monkeypatch.setattr(cli, "_drop_staging_table", boom_drop)
    with pytest.raises(RuntimeError, match="injected import failure") as excinfo:
        cli.main(["execute", *args])
    notes = list(getattr(excinfo.value, "__notes__", []) or [])
    assert any("cleanup boom" in note for note in notes)


def test_only_current_non_active_staging_table_is_dropped(monkeypatch, tmp_path):
    import lancedb

    from catalyst_data.retrieval.gpu_contract import production_lancedb_schema

    bundle, manifest = _bundle(tmp_path, 4)
    artifact_a = _artifact(tmp_path / "build_a", bundle, 4)
    artifact_b = _artifact(tmp_path / "build_b", bundle, 4)
    db_path = _db_file(tmp_path, 4)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args_a = _args(
        pointer, bundle, artifact_a, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    args_b = _args(
        pointer, bundle, artifact_b, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )
    assert cli.main(["execute", *args_a]) == 0
    db = lancedb.connect(str(tmp_path / "lance"))
    active_name = json.loads((tmp_path / "active_generation.json").read_text())["table_name"]
    # an unrelated stale staging table that must survive cleanup
    db.create_table("chunks__staging__unrelated", data=[], schema=production_lancedb_schema())

    import catalyst_data.retrieval.import_metadata as metadata_mod

    def fail_after_two(conn, *, corpus_manifest_id, page_size=500):
        source = metadata_mod.iter_import_metadata(conn, corpus_manifest_id=corpus_manifest_id, page_size=page_size)
        for index, row in enumerate(source):
            if index >= 2:
                raise RuntimeError("injected failure")
            yield row

    monkeypatch.setattr(cli, "iter_import_metadata", fail_after_two)
    with pytest.raises(RuntimeError, match="injected failure"):
        cli.main(["execute", *args_b])

    names = sorted(_table_names(db))
    assert names == sorted([active_name, "chunks__staging__unrelated"])
    assert db.open_table(active_name).count_rows() == 4
    assert db.open_table("chunks__staging__unrelated").count_rows() == 0


# ---------------------------------------------------------------------------
# Task 1 (Pre-GPU amendment): CLI post-commit state machine
# ---------------------------------------------------------------------------


def _active_pointer_payload(tmp_path) -> dict:
    return json.loads((tmp_path / "active_generation.json").read_text())


def test_post_commit_staging_state_cleanup_failure_preserves_committed_generation(monkeypatch, tmp_path):
    """Once the pointer is committed, a staging_state deletion failure must
    produce committed_with_warning (exit 2) and never roll back the active
    table or pointer."""
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)

    real_remove = cli._remove_if_exists

    def failing_state_removal(path):
        if Path(path).name == "staging_state.json":
            raise PermissionError(f"injected state removal failure at {path}")
        return real_remove(path)

    monkeypatch.setattr(cli, "_remove_if_exists", failing_state_removal)
    rc = cli.main(["execute", *args])
    assert rc == 2

    pointer_payload = _active_pointer_payload(tmp_path)
    assert pointer_payload["chunk_count"] == 4
    assert pointer_payload["source_bundle_id"] == manifest["source_bundle_id"]

    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    active = db.open_table(pointer_payload["table_name"])
    assert active.count_rows() == 4
    assert _table_names(db) == [pointer_payload["table_name"]]

    report = json.loads((tmp_path / "lance" / "import_report.json").read_text())
    assert report["status"] == "committed_with_warning"
    assert report["ok"] is True
    assert report["table_name"] == pointer_payload["table_name"]
    assert [w["code"] for w in report["warnings"]] == ["staging_state_cleanup_failed"]
    # warning is sanitized: no raw filesystem path leakage
    assert str(tmp_path) not in report["warnings"][0]["message"]
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()


def test_post_commit_report_write_failure_preserves_committed_generation(monkeypatch, tmp_path, capsys):
    """A report write failure after commit must exit 2 with a structured
    committed_with_warning on stderr and must never drop or roll back the
    active table."""
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)

    real_atomic_json = cli._atomic_json

    def failing_report_write(path, payload):
        if Path(path).name == "import_report.json":
            raise OSError("injected report write failure")
        return real_atomic_json(path, payload)

    monkeypatch.setattr(cli, "_atomic_json", failing_report_write)
    rc = cli.main(["execute", *args])
    assert rc == 2

    pointer_payload = _active_pointer_payload(tmp_path)
    assert pointer_payload["chunk_count"] == 4
    assert pointer_payload["index_manifest_id"] != ""
    assert pointer_payload["source_bundle_id"] == manifest["source_bundle_id"]
    assert pointer_payload["snapshot_id"] == SNAPSHOT
    assert pointer_payload["corpus_manifest_id"] == CORPUS

    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    active = db.open_table(pointer_payload["table_name"])
    assert active.count_rows() == 4
    assert _table_names(db) == [pointer_payload["table_name"]]
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()

    # structured committed_with_warning on stderr with exact committed identity
    err = capsys.readouterr().err
    emitted = json.loads(err)
    assert emitted["status"] == "committed_with_warning"
    assert emitted["table_name"] == pointer_payload["table_name"]
    assert emitted["index_manifest_id"] == pointer_payload["index_manifest_id"]
    assert emitted["source_bundle_id"] == manifest["source_bundle_id"]
    assert emitted["snapshot_id"] == SNAPSHOT
    assert emitted["corpus_manifest_id"] == CORPUS
    assert [w["code"] for w in emitted["warnings"]] == ["import_report_write_failed"]


def test_post_commit_pointer_identity_drift_fails_closed_without_rollback(monkeypatch, tmp_path):
    """If the active pointer drifts between the commit and the CLI's
    post-commit verification, the run must fail closed as committed_with_warning
    and must not roll back or drop the committed table."""
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)

    active_pointer_path = tmp_path / "active_generation.json"
    real_import = cli.import_vectors_to_lancedb

    def drift_after_commit(*call_args, **call_kwargs):
        result = real_import(*call_args, **call_kwargs)
        # concurrent actor repoints the pointer to a different generation
        active_pointer_path.write_text(json.dumps({
            "schema_version": "active_generation_v1",
            "table_name": "chunks__staging__ghost",
            "index_manifest_id": "1" * 64,
            "source_bundle_id": "0" * 64,
            "snapshot_id": "0" * 64,
            "corpus_manifest_id": "0" * 64,
            "chunk_count": 999,
        }))
        return result

    monkeypatch.setattr(cli, "import_vectors_to_lancedb", drift_after_commit)
    rc = cli.main(["execute", *args])
    assert rc == 2

    report = json.loads((tmp_path / "lance" / "import_report.json").read_text())
    assert report["status"] == "committed_with_warning"
    assert [w["code"] for w in report["warnings"]] == ["pointer_identity_mismatch"]
    # the real committed table is preserved and untouched
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    committed_name = report["table_name"]
    assert committed_name in _table_names(db)
    assert db.open_table(committed_name).count_rows() == 4
    assert len(_table_names(db)) == 1


def test_retry_after_committed_with_warning_does_not_create_duplicate_generation(monkeypatch, tmp_path):
    """Retrying execute after a committed_with_warning exit must detect the
    already-committed generation and must not create a second staging table or
    duplicate the import."""
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)

    real_atomic_json = cli._atomic_json

    def failing_report_write(path, payload):
        if Path(path).name == "import_report.json":
            raise OSError("injected first-run report failure")
        return real_atomic_json(path, payload)

    monkeypatch.setattr(cli, "_atomic_json", failing_report_write)
    assert cli.main(["execute", *args]) == 2
    pointer_before = _active_pointer_payload(tmp_path)

    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    assert len(_table_names(db)) == 1

    # retry without the injected failure
    monkeypatch.setattr(cli, "_atomic_json", real_atomic_json)
    rc = cli.main(["execute", *args])
    assert rc == 0
    report = json.loads((tmp_path / "lance" / "import_report.json").read_text())
    assert report["status"] == "already_committed"
    assert report["table_name"] == pointer_before["table_name"]
    assert _active_pointer_payload(tmp_path) == pointer_before
    # no duplicate generation was created
    assert len(_table_names(db)) == 1
    assert db.open_table(pointer_before["table_name"]).count_rows() == 4


def test_resume_after_interrupt_during_persisted_validation(monkeypatch, tmp_path):
    """A KeyboardInterrupt after the artifact was mutated to lancedb_imported
    but before the pointer commit must be recoverable by execute --resume."""
    import catalyst_data.retrieval.gpu_contract as gc

    bundle, manifest = _bundle(tmp_path, 4)
    artifact = _artifact(tmp_path / "build", bundle, 4)
    db_path = _db_file(tmp_path, 4)
    pointer = _pointer(tmp_path, db_path)
    cli = _cli()
    monkeypatch.setattr(cli, "resolve_git_revision", lambda **kwargs: CODE_REVISION)
    args = _args(
        pointer, bundle, artifact, tmp_path / "lance", tmp_path / "active_generation.json",
        manifest["source_bundle_id"],
    )

    real_validate = gc.validate_embedding_import
    state = {"calls": 0}

    def interrupt_on_persisted_validation(*call_args, **call_kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            raise KeyboardInterrupt("operator interrupt during persisted validation")
        return real_validate(*call_args, **call_kwargs)

    monkeypatch.setattr(gc, "validate_embedding_import", interrupt_on_persisted_validation)
    try:
        cli.main(["execute", *args])
        raise AssertionError("expected KeyboardInterrupt")
    except KeyboardInterrupt:
        pass

    # artifact was mutated to lancedb_imported, pointer was NOT committed
    raw = json.loads((artifact / "index_manifest.json").read_text())
    assert raw["artifact_state"] == "lancedb_imported"
    assert not (tmp_path / "active_generation.json").exists()
    assert (tmp_path / "lance" / "import_checkpoint.json").is_file()
    assert (tmp_path / "lance" / "staging_state.json").is_file()

    rc = cli.main(["execute", "--resume", *args])
    assert rc == 0
    active = json.loads((tmp_path / "active_generation.json").read_text())
    assert active["chunk_count"] == 4
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    assert db.open_table(active["table_name"]).count_rows() == 4
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()
    assert not (tmp_path / "lance" / "staging_state.json").exists()


# ---------------------------------------------------------------------------
# B6-L final convergence: activation commit ambiguity (Item 1) + pointer
# identity drift (Item 2)
# ---------------------------------------------------------------------------


def test_ambiguous_pointer_commit_after_os_replace_is_committed_warning(monkeypatch, tmp_path):
    """The pointer atomic writer completes os.replace and then raises. The
    generation IS committed: the CLI must detect it by re-reading the pointer,
    skip pre-commit cleanup, preserve recovery state, and exit 2 with a
    structured sanitized committed_with_warning."""
    import catalyst_data.retrieval.gpu_contract as gc

    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)
    real_atomic_json = gc._atomic_json

    def commit_then_raise(path, payload):
        if Path(path).name == "active_generation.json":
            real_atomic_json(path, payload)  # pointer is truly committed
            raise RuntimeError("injected exception after pointer os.replace")
        return real_atomic_json(path, payload)

    monkeypatch.setattr(gc, "_atomic_json", commit_then_raise)
    rc = cli.main(["execute", *args])
    assert rc == 2

    pointer_payload = _active_pointer_payload(tmp_path)
    assert pointer_payload["chunk_count"] == 4
    assert pointer_payload["source_bundle_id"] == manifest["source_bundle_id"]
    assert pointer_payload["snapshot_id"] == SNAPSHOT
    assert pointer_payload["corpus_manifest_id"] == CORPUS
    assert pointer_payload["index_manifest_id"]

    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    active = db.open_table(pointer_payload["table_name"])
    assert active.count_rows() == 4
    assert _table_names(db) == [pointer_payload["table_name"]]

    # No pre-commit cleanup ran: the CLI's recovery state (staging_state) is
    # preserved. The import checkpoint is already gone because checkpoint
    # cleanup is required to happen BEFORE the pointer commit inside
    # gpu_contract; the CLI must not delete the remaining staging_state.
    assert (tmp_path / "lance" / "staging_state.json").is_file()
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()

    report = json.loads((tmp_path / "lance" / "import_report.json").read_text())
    assert report["status"] == "committed_with_warning"
    assert report["table_name"] == pointer_payload["table_name"]
    assert report["index_manifest_id"] == pointer_payload["index_manifest_id"]
    codes = [w["code"] for w in report["warnings"]]
    assert "post_commit_exception" in codes
    # structured + sanitized (no absolute path leakage)
    assert isinstance(report["warnings"][0]["message"], str)
    assert str(tmp_path) not in report["warnings"][0]["message"]


def test_retry_after_ambiguous_commit_recognizes_already_committed(monkeypatch, tmp_path):
    """After the ambiguous-commit warning (recovery state preserved), a plain
    execute retry must recognize already_committed, recover the report, and
    never create a second staging table."""
    import catalyst_data.retrieval.gpu_contract as gc

    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)
    real_atomic_json = gc._atomic_json

    def commit_then_raise(path, payload):
        if Path(path).name == "active_generation.json":
            real_atomic_json(path, payload)
            raise RuntimeError("injected exception after pointer os.replace")
        return real_atomic_json(path, payload)

    monkeypatch.setattr(gc, "_atomic_json", commit_then_raise)
    assert cli.main(["execute", *args]) == 2
    pointer_before = _active_pointer_payload(tmp_path)
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    assert len(_table_names(db)) == 1

    # retry: no injected failure
    monkeypatch.setattr(gc, "_atomic_json", real_atomic_json)
    rc = cli.main(["execute", *args])
    assert rc == 0
    report = json.loads((tmp_path / "lance" / "import_report.json").read_text())
    assert report["status"] == "already_committed"
    assert report["table_name"] == pointer_before["table_name"]
    assert _active_pointer_payload(tmp_path) == pointer_before
    assert len(_table_names(db)) == 1
    assert db.open_table(pointer_before["table_name"]).count_rows() == 4
    # stale recovery state cleaned up by the already-committed recovery
    assert not (tmp_path / "lance" / "import_checkpoint.json").exists()
    assert not (tmp_path / "lance" / "staging_state.json").exists()


def test_pointer_identity_drift_preserves_staging_state(monkeypatch, tmp_path):
    """When the committed pointer identity does not match after commit, the
    CLI must preserve staging_state and exit 2 without touching the table."""
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)
    active_pointer_path = tmp_path / "active_generation.json"
    real_import = cli.import_vectors_to_lancedb

    def drift_after_commit(*call_args, **call_kwargs):
        result = real_import(*call_args, **call_kwargs)
        active_pointer_path.write_text(json.dumps({
            "schema_version": "active_generation_v1",
            "table_name": "chunks__staging__ghost",
            "index_manifest_id": "1" * 64,
            "source_bundle_id": "0" * 64,
            "snapshot_id": "0" * 64,
            "corpus_manifest_id": "0" * 64,
            "chunk_count": 999,
        }))
        return result

    monkeypatch.setattr(cli, "import_vectors_to_lancedb", drift_after_commit)
    rc = cli.main(["execute", *args])
    assert rc == 2

    report = json.loads((tmp_path / "lance" / "import_report.json").read_text())
    assert report["status"] == "committed_with_warning"
    assert [w["code"] for w in report["warnings"]] == ["pointer_identity_mismatch"]
    # staging_state preserved because pointer identity was NOT verified
    assert (tmp_path / "lance" / "staging_state.json").is_file()
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    committed_name = report["table_name"]
    assert committed_name in _table_names(db)
    assert db.open_table(committed_name).count_rows() == 4
    assert len(_table_names(db)) == 1


def test_retry_after_pointer_drift_fails_closed_without_duplicate_generation(monkeypatch, tmp_path):
    """After pointer drift preserved staging_state, a plain execute retry must
    fail closed (no second staging table, no duplicate import)."""
    cli, args, bundle, artifact, db_path, pointer, manifest = _run_preflight(monkeypatch, tmp_path)
    active_pointer_path = tmp_path / "active_generation.json"
    real_import = cli.import_vectors_to_lancedb

    def drift_after_commit(*call_args, **call_kwargs):
        result = real_import(*call_args, **call_kwargs)
        active_pointer_path.write_text(json.dumps({
            "schema_version": "active_generation_v1",
            "table_name": "chunks__staging__ghost",
            "index_manifest_id": "1" * 64,
            "source_bundle_id": "0" * 64,
            "snapshot_id": "0" * 64,
            "corpus_manifest_id": "0" * 64,
            "chunk_count": 999,
        }))
        return result

    monkeypatch.setattr(cli, "import_vectors_to_lancedb", drift_after_commit)
    assert cli.main(["execute", *args]) == 2
    assert (tmp_path / "lance" / "staging_state.json").is_file()
    import lancedb

    db = lancedb.connect(str(tmp_path / "lance"))
    tables_before = sorted(_table_names(db))
    assert len(tables_before) == 1

    # The retry fails closed before creating any new table (either at input
    # verification because the drifted pointer names a nonexistent table, or
    # at the staging_state guard); it must never start a duplicate import.
    with pytest.raises(ValueError):
        cli.main(["execute", *args])
    assert sorted(_table_names(db)) == tables_before
    assert db.open_table(tables_before[0]).count_rows() == 4
    assert (tmp_path / "lance" / "staging_state.json").is_file()
