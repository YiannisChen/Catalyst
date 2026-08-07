from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

CODE_REVISION = "04dabde9f44e9638d1b5ebad0eed002f5475c1a3"


class _BoundedTable:
    """Fake staging table matching the bounded-import contract."""

    def __init__(self, name: str = "staging"):
        self.name = name
        self.rows: list[dict] = []

    def add(self, rows):
        self.rows.extend(list(rows))

    def delete(self, _predicate):
        self.rows = []

    def count_rows(self):
        return len(self.rows)

    def take_offsets(self, offsets):
        import pyarrow as pa

        selected = [dict(self.rows[index]) for index in offsets if index < len(self.rows)]
        return _ArrowBuilder(pa.Table.from_pylist(selected) if selected else pa.Table.from_pylist([]))

    def update(self, where=None, values=None):
        for row in self.rows:
            row.update(values or {})
        return type("UpdateResult", (), {"rows_updated": len(self.rows)})()

    def to_arrow(self):
        raise AssertionError("full-table to_arrow must not be called")

    def to_list(self):
        raise AssertionError("full-table to_list must not be called")


class _ArrowBuilder:
    def __init__(self, arrow):
        self._arrow = arrow

    def to_arrow(self):
        return self._arrow


def _metadata_rows(count: int) -> list[dict]:
    rows = []
    for index in range(count):
        text = f"fixture content {index} — 中文"
        rows.append({
            "chunk_id": f"chunk:{index:04d}", "document_id": f"doc:{index}",
            "content_text": text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "metadata_hash": hashlib.sha256(f"meta:{index}".encode()).hexdigest(),
            "available_at": "2026-01-01T00:00:00Z", "ticker_associations": ["AAPL"],
            "source_class": "reported_news", "chunk_profile_version": "news_v2",
            "status": "active", "eligibility": "eligible",
            "dedup_cluster_id": f"cluster:{index}",
            "cluster_first_available_at": "2026-01-01T00:00:00Z",
            "representative_document_id": f"doc:{index}",
        })
    return rows


def _bundle(tmp_path):
    from catalyst_data.retrieval.source_bundle import _chunk_record_hash, _stream_source_bundle_id

    records = []
    for index in range(4):
        text = f"fixture content {index} — 中文"
        records.append({
            "chunk_id": f"chunk:{index:04d}", "document_id": f"doc:{index}",
            "content_text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "metadata_hash": "a" * 64, "available_at": "2026-01-01T00:00:00Z",
            "ticker_associations": "[\"AAPL\"]", "corpus_manifest_id": "c" * 64,
            "chunk_profile_version": "news_v2",
        })
    identity = _stream_source_bundle_id(corpus_manifest_id="c" * 64, snapshot_id="6" * 64, probe_report_id="7" * 64, postbuild_readiness_id="8" * 64, record_hashes=[_chunk_record_hash(r) for r in records])
    root = tmp_path / f"source_{identity}"
    root.mkdir()
    (root / "chunks.jsonl").write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records))
    manifest = {"schema_version": "1.1.0", "source_bundle_id": identity, "corpus_manifest_id": "c" * 64, "snapshot_id": "6" * 64, "probe_report_id": "7" * 64, "postbuild_readiness_id": "8" * 64, "chunk_count": 4, "universe_manifest_id": "u" * 64}
    (root / "source_bundle_manifest.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    checksums = {}
    for name in ("chunks.jsonl", "source_bundle_manifest.json"):
        checksums[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    (root / "checksums.sha256").write_text("\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())) + "\n")
    return root, manifest


def test_verify_source_bundle_streams_and_binds_identity(tmp_path):
    from catalyst_data.retrieval.gpu_contract import verify_source_bundle

    root, manifest = _bundle(tmp_path)
    verified = verify_source_bundle(root, expected_snapshot_id="6" * 64, expected_corpus_manifest_id="c" * 64)
    assert verified.chunk_count == 4
    assert verified.source_bundle_id == manifest["source_bundle_id"]


def test_verify_source_bundle_rejects_wrong_identity_and_order(tmp_path):
    from catalyst_data.retrieval.gpu_contract import verify_source_bundle

    root, manifest = _bundle(tmp_path)
    with pytest.raises(ValueError, match="snapshot_id"):
        verify_source_bundle(root, expected_snapshot_id="x" * 64)
    chunks = (root / "chunks.jsonl").read_text().splitlines()
    (root / "chunks.jsonl").write_text("\n".join(reversed(chunks)) + "\n")
    with pytest.raises(ValueError):
        verify_source_bundle(root)


def test_import_rejects_mock_artifact_in_production(tmp_path):
    from catalyst_data.retrieval.gpu_contract import validate_embedding_import

    bundle, manifest = _bundle(tmp_path)
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    np.save(artifact / "vectors.npy", np.ones((4, 1024), dtype=np.float32))
    (artifact / "chunk_ids.json").write_text(json.dumps([f"chunk:{i:04d}" for i in range(4)]))
    (artifact / "index_manifest.json").write_text(json.dumps({"non_production": True, "source_bundle_id": manifest["source_bundle_id"]}))
    with pytest.raises(ValueError, match="non_production"):
        validate_embedding_import(bundle, artifact)


def test_oom_backoff_has_no_cpu_fallback():
    from catalyst_data.retrieval.gpu_contract import embed_with_oom_backoff

    seen = []
    def embed(batch):
        seen.append(len(batch))
        if len(batch) > 1:
            raise RuntimeError("CUDA out of memory")
        return np.ones((1, 1024), dtype=np.float32)

    vectors = embed_with_oom_backoff(["a", "b"], embed, initial_batch_size=2)
    assert seen == [2, 1, 1]
    assert vectors.shape == (2, 1024)


def test_non_oom_cuda_error_is_not_treated_as_oom():
    from catalyst_data.retrieval.gpu_contract import embed_with_oom_backoff

    def embed(_batch):
        raise RuntimeError("CUDA driver initialization failed")

    with pytest.raises(RuntimeError, match="initialization failed"):
        embed_with_oom_backoff(["a"], embed, initial_batch_size=1)


def test_driver_streams_fixture_bundle_and_writes_atomic_manifest(tmp_path):
    from catalyst_data.retrieval.gpu_contract import validate_embedding_import
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, manifest = _bundle(tmp_path)

    def embed(batch):
        return np.ones((len(batch), 1024), dtype=np.float32)

    artifact = build_embedding_artifacts(
        bundle, tmp_path / "out", embed_batch=embed, batch_size=2,
        mock=True, code_revision=CODE_REVISION,
    )
    assert artifact["vector_count"] == 4
    assert (tmp_path / "out" / "vectors.npy").is_file()
    assert not list((tmp_path / "out").glob("*.tmp"))
    assert not list((tmp_path / "out" / "shards").glob("*.tmp"))
    validated = validate_embedding_import(bundle, tmp_path / "out", allow_non_production=True)
    assert validated.vector_count == 4


def test_driver_resume_reuses_verified_shards(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = tmp_path / "out"
    calls = []
    def embed(batch):
        calls.append(tuple(batch))
        return np.ones((len(batch), 1024), dtype=np.float32)
    build_embedding_artifacts(bundle, out, embed_batch=embed, batch_size=2, shard_size=2, mock=True, code_revision=CODE_REVISION)
    before = len(calls)
    build_embedding_artifacts(bundle, out, embed_batch=embed, batch_size=2, shard_size=2, mock=True, resume=True, code_revision=CODE_REVISION)
    assert len(calls) == before


def test_driver_resume_rejects_different_bundle_identity(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = tmp_path / "out"
    build_embedding_artifacts(
        bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        batch_size=2, shard_size=2, mock=True, code_revision=CODE_REVISION,
    )
    checkpoint = json.loads((out / "checkpoint.json").read_text())
    checkpoint["source_bundle_id"] = "0" * 64
    (out / "checkpoint.json").write_text(json.dumps(checkpoint))
    with pytest.raises(ValueError, match="resume checkpoint source_bundle_id"):
        build_embedding_artifacts(
            bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
            mock=True, resume=True, code_revision=CODE_REVISION,
        )


def test_driver_resume_rejects_tampered_shard(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = tmp_path / "out"
    build_embedding_artifacts(
        bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        batch_size=2, shard_size=2, mock=True, code_revision=CODE_REVISION,
    )
    shard = out / "shards" / "shard_000000.npz"
    shard.write_bytes(shard.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="resume shard"):
        build_embedding_artifacts(
            bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
            batch_size=2, shard_size=2, mock=True, resume=True, code_revision=CODE_REVISION,
        )


def test_driver_non_resume_rejects_non_empty_output_directory(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "sentinel").write_text("preserve")
    with pytest.raises(ValueError, match="must be empty"):
        build_embedding_artifacts(
            bundle, out,
            embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
            mock=True, code_revision=CODE_REVISION,
        )
    assert (out / "sentinel").read_text() == "preserve"


def test_import_vectors_binds_each_row_to_index_manifest_lancedb_table(tmp_path):
    from catalyst_data.retrieval.gpu_contract import import_vectors_to_lancedb, validate_embedding_import
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, source_manifest = _bundle(tmp_path)
    artifact_path = tmp_path / "artifact"
    build_embedding_artifacts(
        bundle, artifact_path,
        embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        mock=True, code_revision=CODE_REVISION,
    )
    metadata = _metadata_rows(4)

    table = _BoundedTable()
    index_manifest = import_vectors_to_lancedb(
        bundle, artifact_path, table, metadata_rows=metadata,
        allow_non_production=True,
    )
    assert len(table.rows) == 4
    assert all(row["index_manifest_id"] == index_manifest.index_manifest_id for row in table.rows)
    assert all(row["corpus_manifest_id"] == source_manifest["corpus_manifest_id"] for row in table.rows)
    assert all(row["vector"] for row in table.rows)
    validated = validate_embedding_import(
        bundle, artifact_path, allow_non_production=True, lancedb_table=table,
    )
    assert validated.index_manifest_id == index_manifest.index_manifest_id
    assert validated.artifact_state == "lancedb_imported"


def test_imported_lancedb_artifact_rejects_tampered_persisted_rows(tmp_path):
    from catalyst_data.retrieval.gpu_contract import (
        import_vectors_to_lancedb, validate_embedding_import,
    )
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    artifact_path = tmp_path / "artifact"
    build_embedding_artifacts(
        bundle, artifact_path,
        embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        mock=True, code_revision=CODE_REVISION,
    )
    metadata = _metadata_rows(4)

    table = _BoundedTable()
    import_vectors_to_lancedb(
        bundle, artifact_path, table, metadata_rows=metadata,
        allow_non_production=True,
    )
    table.rows[0]["content_text"] = "tampered"
    with pytest.raises(ValueError, match="persisted artifact hash"):
        validate_embedding_import(
            bundle, artifact_path, allow_non_production=True, lancedb_table=table,
        )


def test_import_rolls_back_table_and_manifest_after_post_add_failure(tmp_path):
    from catalyst_data.retrieval.gpu_contract import import_vectors_to_lancedb
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    artifact_path = tmp_path / "artifact"
    build_embedding_artifacts(
        bundle, artifact_path,
        embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        mock=True, code_revision=CODE_REVISION,
    )
    previous_manifest = (artifact_path / "index_manifest.json").read_bytes()
    previous_checksums = (artifact_path / "checksums.sha256").read_bytes()
    metadata = _metadata_rows(4)

    class FailingTable:
        def __init__(self):
            self.rows = []

        def add(self, rows):
            self.rows = rows
            raise RuntimeError("partial table add")

        def delete(self, _predicate):
            self.rows = []

        def to_list(self):
            return self.rows

    table = FailingTable()
    with pytest.raises(RuntimeError, match="partial table add"):
        import_vectors_to_lancedb(
            bundle, artifact_path, table, metadata_rows=metadata,
            allow_non_production=True,
        )
    assert table.rows == []
    assert (artifact_path / "index_manifest.json").read_bytes() == previous_manifest
    assert (artifact_path / "checksums.sha256").read_bytes() == previous_checksums


def test_import_vectors_hashes_actual_fixture_lancedb_rows(tmp_path):
    import lancedb
    import pyarrow as pa
    from catalyst_data.retrieval.gpu_contract import import_vectors_to_lancedb
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    artifact_path = tmp_path / "artifact"
    build_embedding_artifacts(
        bundle, artifact_path,
        embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        mock=True, code_revision=CODE_REVISION,
    )
    metadata = _metadata_rows(4)
    from catalyst_data.retrieval.gpu_contract import production_lancedb_schema
    schema = production_lancedb_schema()
    table = lancedb.connect(str(tmp_path / "lance")).create_table("vectors", schema=schema)
    manifest = import_vectors_to_lancedb(
        bundle, artifact_path, table, metadata_rows=metadata,
        allow_non_production=True,
    )
    assert table.count_rows() == 4
    assert manifest.artifact_state == "lancedb_imported"
    assert manifest.artifact_hashes["lancedb_table"] != "0" * 64




def test_gpu_cli_production_fails_closed_before_loader(tmp_path, monkeypatch):
    import scripts.build_corpus_embeddings_gpu as cli

    bundle, manifest = _bundle(tmp_path)
    called = False

    def forbidden_loader(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("model loader must not run")

    monkeypatch.setattr(cli, "load_real_cuda_embedder", forbidden_loader)
    args = ["--source-bundle", str(bundle), "--output-dir", str(tmp_path / "out")]
    args.extend([
        "--expected-source-bundle-id", "wrong" + manifest["source_bundle_id"][5:],
        "--expected-snapshot-id", manifest["snapshot_id"],
        "--expected-corpus-manifest-id", manifest["corpus_manifest_id"],
        "--expected-probe-report-id", manifest["probe_report_id"],
        "--expected-postbuild-readiness-id", manifest["postbuild_readiness_id"],
        "--code-revision", CODE_REVISION,
    ])
    with pytest.raises((ValueError, RuntimeError)):
        cli.main(args)
    assert called is False


def test_gpu_cli_mock_mode_with_injected_revision(tmp_path, monkeypatch):
    import scripts.build_corpus_embeddings_gpu as cli

    bundle, manifest = _bundle(tmp_path)
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    args = ["--source-bundle", str(bundle), "--output-dir", str(tmp_path / "out"), "--mock"]
    args.extend([
        "--expected-source-bundle-id", manifest["source_bundle_id"],
        "--expected-snapshot-id", manifest["snapshot_id"],
        "--expected-corpus-manifest-id", manifest["corpus_manifest_id"],
        "--expected-probe-report-id", manifest["probe_report_id"],
        "--expected-postbuild-readiness-id", manifest["postbuild_readiness_id"],
        "--code-revision", CODE_REVISION,
    ])
    assert cli.main(args) == 0
    assert (tmp_path / "out" / "index_manifest.json").is_file()

def test_real_loader_rejects_unpinned_revision():
    from catalyst_data.retrieval.gpu_driver import load_real_cuda_embedder

    with pytest.raises(ValueError, match="pinned"):
        load_real_cuda_embedder(model_revision="0" * 40)


def test_gpu_cli_requires_manager_expected_identities_before_loader(tmp_path, monkeypatch):
    import scripts.build_corpus_embeddings_gpu as cli

    bundle, manifest = _bundle(tmp_path)
    called = False

    def forbidden_loader(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("model loader must not run")

    monkeypatch.setattr(cli, "load_real_cuda_embedder", forbidden_loader)
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    args = ["--source-bundle", str(bundle), "--output-dir", str(tmp_path / "out")]
    args.extend([
        "--expected-source-bundle-id", "wrong" + manifest["source_bundle_id"][5:],
        "--expected-snapshot-id", manifest["snapshot_id"],
        "--expected-corpus-manifest-id", manifest["corpus_manifest_id"],
        "--expected-probe-report-id", manifest["probe_report_id"],
        "--expected-postbuild-readiness-id", manifest["postbuild_readiness_id"],
        "--code-revision", CODE_REVISION,
    ])
    with pytest.raises(ValueError, match="source_bundle_id"):
        cli.main(args)
    assert called is False


def test_gpu_cli_missing_expected_identity_is_rejected_by_parser(tmp_path, monkeypatch):
    import scripts.build_corpus_embeddings_gpu as cli

    bundle, _ = _bundle(tmp_path)
    monkeypatch.setattr(cli, "load_real_cuda_embedder", lambda **kwargs: pytest.fail("loader called"))
    with pytest.raises(SystemExit):
        cli.main(["--source-bundle", str(bundle), "--output-dir", str(tmp_path / "out")])


def test_real_loader_rejects_cpu_before_snapshot_download():
    from catalyst_data.retrieval.gpu_driver import _load_real_cuda_embedder

    class FakeCuda:
        @staticmethod
        def is_available():
            return False

    class FakeTorch:
        cuda = FakeCuda()

    called = False

    def forbidden_download(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("download must not run on CPU")

    with pytest.raises(RuntimeError, match="CUDA is required"):
        _load_real_cuda_embedder(
            torch_module=FakeTorch,
            snapshot_download_fn=forbidden_download,
            model_class=object,
        )
    assert called is False


def test_real_loader_runs_batch_one_preflight_and_validates_contract():
    from catalyst_data.retrieval.gpu_driver import _load_real_cuda_embedder

    class FakeCuda:
        @staticmethod
        def is_available():
            return True

    class FakeTorch:
        cuda = FakeCuda()

    calls = []

    def download(**kwargs):
        calls.append(("download", kwargs))
        return "/fixture/model"

    class Model:
        def __init__(self, *args, **kwargs):
            calls.append(("load", args, kwargs))

        def encode(self, texts, **kwargs):
            calls.append(("encode", list(texts), kwargs))
            return {"dense_vecs": np.ones((len(texts), 1024), dtype=np.float32) / 32.0}

    embed = _load_real_cuda_embedder(
        torch_module=FakeTorch,
        snapshot_download_fn=download,
        model_class=Model,
    )
    assert calls[0][0] == "download"
    assert calls[1][0] == "load"
    assert calls[2][0] == "encode"
    assert calls[2][1] == ["catalyst B6-L CUDA preflight"]
    assert calls[2][2]["batch_size"] == 1
    assert np.asarray(embed(["fixture"])).shape == (1, 1024)


def test_driver_resume_rejects_shard_size_mismatch(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = tmp_path / "out"
    build_embedding_artifacts(
        bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        batch_size=2, shard_size=2, mock=True, code_revision=CODE_REVISION,
    )
    checkpoint = json.loads((out / "checkpoint.json").read_text())
    checkpoint["shard_size"] = 4
    (out / "checkpoint.json").write_text(json.dumps(checkpoint))
    with pytest.raises(ValueError, match="resume checkpoint shard_size"):
        build_embedding_artifacts(
            bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
            batch_size=2, shard_size=2, mock=True, resume=True, code_revision=CODE_REVISION,
        )


def test_driver_resume_rejects_checkpoint_missing_checksums(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = tmp_path / "out"
    build_embedding_artifacts(
        bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        batch_size=2, shard_size=2, mock=True, code_revision=CODE_REVISION,
    )
    checkpoint = json.loads((out / "checkpoint.json").read_text())
    del checkpoint["shard_checksums"]
    (out / "checkpoint.json").write_text(json.dumps(checkpoint))
    with pytest.raises(ValueError, match="shard checksums missing"):
        build_embedding_artifacts(
            bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
            batch_size=2, shard_size=2, mock=True, resume=True, code_revision=CODE_REVISION,
        )


def test_driver_resume_rejects_mock_mode_mismatch(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = tmp_path / "out"
    build_embedding_artifacts(
        bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        batch_size=2, shard_size=2, mock=True, code_revision=CODE_REVISION,
    )
    with pytest.raises(ValueError, match="resume checkpoint mock"):
        build_embedding_artifacts(
            bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
            batch_size=2, shard_size=2, mock=False, resume=True, code_revision=CODE_REVISION,
        )


def test_driver_resume_without_checkpoint_into_dirty_dir_fails_closed(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    prior = out / "vectors.npy"
    prior.write_bytes(b"prior-artifact")
    with pytest.raises(ValueError, match="checkpoint"):
        build_embedding_artifacts(
            bundle, out, embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
            batch_size=2, shard_size=2, mock=True, resume=True, code_revision=CODE_REVISION,
        )
    assert prior.read_bytes() == b"prior-artifact"


# ---------------------------------------------------------------------------
# B6-L final convergence: GPU checkpoint complete identity (Item 3)
# ---------------------------------------------------------------------------

CHECKPOINT_SCHEMA_VERSION = "embedding_checkpoint_v1"


def _partial_checkpoint_output(tmp_path, bundle, *, code_revision=CODE_REVISION):
    """SIGKILL-equivalent: interrupt after the first shard so only
    checkpoint.json + shards remain (no report, no index_manifest)."""
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    out = tmp_path / "out"
    interrupt_state = {"armed": True}

    def interrupted_embed(batch):
        if interrupt_state["armed"]:
            interrupt_state["armed"] = False
            return np.ones((len(batch), 1024), dtype=np.float32)
        raise KeyboardInterrupt("sigkill-equivalent interrupt")

    with pytest.raises(KeyboardInterrupt):
        build_embedding_artifacts(
            bundle, out, embed_batch=interrupted_embed,
            batch_size=2, shard_size=2, mock=True, code_revision=code_revision,
        )
    assert (out / "checkpoint.json").is_file()
    assert list((out / "shards").glob("shard_*.npz"))
    assert not (out / "index_manifest.json").exists()
    assert not (out / "gpu_run_report.json").exists()
    return out


def test_checkpoint_schema_binds_full_identity(tmp_path):
    from catalyst_data.config import BGE_M3_MODEL, BGE_M3_REVISION, BGE_M3_DIMENSION
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION

    bundle, manifest = _bundle(tmp_path)
    out = _partial_checkpoint_output(tmp_path, bundle)
    checkpoint = json.loads((out / "checkpoint.json").read_text())
    assert checkpoint["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    assert checkpoint["source_bundle_id"] == manifest["source_bundle_id"]
    assert checkpoint["snapshot_id"] == "6" * 64
    assert checkpoint["corpus_manifest_id"] == "c" * 64
    assert checkpoint["probe_report_id"] == "7" * 64
    assert checkpoint["postbuild_readiness_id"] == "8" * 64
    assert checkpoint["code_revision"] == CODE_REVISION
    assert checkpoint["model_name"] == BGE_M3_MODEL
    assert checkpoint["model_revision"] == BGE_M3_REVISION
    assert checkpoint["tokenizer_revision"] == TOKENIZER_REVISION
    assert checkpoint["dimension"] == BGE_M3_DIMENSION
    assert checkpoint["normalization_mode"] == "l2"
    assert checkpoint["dtype"] == "float32"
    assert checkpoint["mock"] is True
    assert checkpoint["shard_size"] == 2
    assert checkpoint["completed_rows"] == 2
    assert checkpoint["chunk_order_checksum"]
    assert isinstance(checkpoint["shard_checksums"], dict)


def test_resume_rejects_checkpoint_code_revision_mismatch_before_embedding(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = _partial_checkpoint_output(tmp_path, bundle)
    calls = []

    def embed(batch):
        calls.append(tuple(batch))
        return np.ones((len(batch), 1024), dtype=np.float32)

    with pytest.raises(ValueError, match="code_revision"):
        build_embedding_artifacts(
            bundle, out, embed_batch=embed, batch_size=2, shard_size=2,
            mock=True, resume=True, code_revision="0" * 40,
        )
    assert calls == []


def test_resume_rejects_checkpoint_model_revision_mismatch(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = _partial_checkpoint_output(tmp_path, bundle)
    checkpoint = json.loads((out / "checkpoint.json").read_text())
    checkpoint["model_revision"] = "0" * 40
    (out / "checkpoint.json").write_text(json.dumps(checkpoint))
    with pytest.raises(ValueError, match="model_revision"):
        build_embedding_artifacts(
            bundle, out, embed_batch=lambda b: np.ones((len(b), 1024), dtype=np.float32),
            batch_size=2, shard_size=2, mock=True, resume=True,
            code_revision=CODE_REVISION,
        )


def test_resume_rejects_checkpoint_data_identity_mismatch(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    for field in ("snapshot_id", "corpus_manifest_id", "probe_report_id", "postbuild_readiness_id"):
        out = _partial_checkpoint_output(tmp_path / f"case_{field}", bundle)
        checkpoint = json.loads((out / "checkpoint.json").read_text())
        checkpoint[field] = "0" * 64
        (out / "checkpoint.json").write_text(json.dumps(checkpoint))
        with pytest.raises(ValueError, match=field):
            build_embedding_artifacts(
                bundle, out, embed_batch=lambda b: np.ones((len(b), 1024), dtype=np.float32),
                batch_size=2, shard_size=2, mock=True, resume=True,
                code_revision=CODE_REVISION,
            )


def test_resume_same_identity_succeeds_without_regenerating_existing_shards(tmp_path):
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = _partial_checkpoint_output(tmp_path, bundle)
    shard_before = (out / "shards" / "shard_000000.npz").read_bytes()
    calls = []

    def embed(batch):
        calls.append(tuple(batch))
        return np.ones((len(batch), 1024), dtype=np.float32)

    manifest = build_embedding_artifacts(
        bundle, out, embed_batch=embed, batch_size=2, shard_size=2,
        mock=True, resume=True, code_revision=CODE_REVISION,
    )
    assert manifest["vector_count"] == 4
    # only the unfinished chunk range was embedded; existing shard untouched
    assert calls == [("fixture content 2 — 中文", "fixture content 3 — 中文")]
    assert (out / "shards" / "shard_000000.npz").read_bytes() == shard_before


# ---------------------------------------------------------------------------
# B6-L final convergence: production offline enforcement (Item 4)
# ---------------------------------------------------------------------------


def test_real_loader_uses_local_files_only():
    from catalyst_data.config import BGE_M3_MODEL, BGE_M3_REVISION
    from catalyst_data.retrieval.gpu_driver import _load_real_cuda_embedder

    class FakeCuda:
        @staticmethod
        def is_available():
            return True

    class FakeTorch:
        cuda = FakeCuda()

    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        return "/fixture/model"

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def encode(self, texts, **kwargs):
            return {"dense_vecs": np.ones((len(texts), 1024), dtype=np.float32) / 32.0}

    _load_real_cuda_embedder(
        torch_module=FakeTorch, snapshot_download_fn=download, model_class=Model,
    )
    assert calls, "snapshot_download must be called"
    assert calls[0]["local_files_only"] is True
    assert calls[0]["repo_id"] == BGE_M3_MODEL
    assert calls[0]["revision"] == BGE_M3_REVISION


def test_real_loader_fails_closed_when_model_not_cached():
    from catalyst_data.retrieval.gpu_driver import _load_real_cuda_embedder

    class FakeCuda:
        @staticmethod
        def is_available():
            return True

    class FakeTorch:
        cuda = FakeCuda()

    class Model:
        def __init__(self, *args, **kwargs):
            raise AssertionError("model must not load on offline cache miss")

    def download(**kwargs):
        raise FileNotFoundError("cache miss: BAAI/bge-m3 not in local HF cache")

    with pytest.raises(RuntimeError, match="offline|bootstrap|cache"):
        _load_real_cuda_embedder(
            torch_module=FakeTorch, snapshot_download_fn=download, model_class=Model,
        )


# ---------------------------------------------------------------------------
# B6-L final convergence: shard checksum incremental maintenance (Item 7)
# ---------------------------------------------------------------------------


def test_driver_does_not_rehash_historical_shards_during_continuation(tmp_path, monkeypatch):
    from pathlib import Path

    from catalyst_data.retrieval import gpu_driver as driver_mod
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = tmp_path / "out"
    counts: dict[str, int] = {}
    real_sha = driver_mod._sha256_file

    def counting_sha(path):
        name = Path(path).name
        if name.startswith("shard_"):
            counts[name] = counts.get(name, 0) + 1
        return real_sha(path)

    monkeypatch.setattr(driver_mod, "_sha256_file", counting_sha)
    build_embedding_artifacts(
        bundle, out, embed_batch=lambda b: np.ones((len(b), 1024), dtype=np.float32),
        batch_size=2, shard_size=2, mock=True, code_revision=CODE_REVISION,
    )
    # count=4, shard_size=2 => two shards; each shard file hashed exactly once
    assert counts == {"shard_000000.npz": 1, "shard_000001.npz": 1}


def test_driver_resume_does_not_rehash_validated_shards_at_continuation_checkpoint(tmp_path, monkeypatch):
    from pathlib import Path

    from catalyst_data.retrieval import gpu_driver as driver_mod
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path)
    out = tmp_path / "out"
    counts: dict[str, int] = {}
    real_sha = driver_mod._sha256_file

    def counting_sha(path):
        name = Path(path).name
        if name.startswith("shard_"):
            counts[name] = counts.get(name, 0) + 1
        return real_sha(path)

    monkeypatch.setattr(driver_mod, "_sha256_file", counting_sha)

    interrupt_state = {"armed": True}

    def interrupted_embed(batch):
        if interrupt_state["armed"]:
            interrupt_state["armed"] = False
            return np.ones((len(batch), 1024), dtype=np.float32)
        raise KeyboardInterrupt("stop after shard 0")

    with pytest.raises(KeyboardInterrupt):
        build_embedding_artifacts(
            bundle, out, embed_batch=interrupted_embed,
            batch_size=2, shard_size=2, mock=True, code_revision=CODE_REVISION,
        )
    assert counts.get("shard_000000.npz", 0) == 1

    build_embedding_artifacts(
        bundle, out, embed_batch=lambda b: np.ones((len(b), 1024), dtype=np.float32),
        batch_size=2, shard_size=2, mock=True, resume=True, code_revision=CODE_REVISION,
    )
    # resume validates shard_000000 once; the continuation checkpoint writes
    # shard_000001 WITHOUT re-hashing the historical shard
    assert counts["shard_000000.npz"] == 2
    assert counts["shard_000001.npz"] == 1
