"""RED/GREEN contract for bounded-memory LanceDB production import (Finding 1).

The production import path must never materialize whole-corpus Python row/vector
containers, never read the persisted table with to_pylist()/to_list()/fetchall(),
never use a single IN predicate with all chunk IDs, and must write through a
staging generation with an atomic active pointer and resumable checkpoints.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

CODE_REVISION = "04dabde9f44e9638d1b5ebad0eed002f5475c1a3"


def _bundle(tmp_path, count: int = 4) -> tuple[Path, dict]:
    from catalyst_data.retrieval.source_bundle import _chunk_record_hash, _stream_source_bundle_id

    width = max(4, len(str(count - 1)))
    records = []
    for index in range(count):
        text = f"fixture content {index} — 中文"
        records.append({
            "chunk_id": f"chunk:{index:0{width}d}", "document_id": f"doc:{index}",
            "content_text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "metadata_hash": "a" * 64, "available_at": "2026-01-01T00:00:00Z",
            "ticker_associations": "[\"AAPL\"]", "corpus_manifest_id": "c" * 64,
            "chunk_profile_version": "news_v2",
        })
    identity = _stream_source_bundle_id(
        corpus_manifest_id="c" * 64, snapshot_id="6" * 64,
        probe_report_id="7" * 64, postbuild_readiness_id="8" * 64,
        record_hashes=[_chunk_record_hash(r) for r in records],
    )
    root = tmp_path / f"source_{identity}"
    root.mkdir(parents=True)
    (root / "chunks.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records)
    )
    manifest = {
        "schema_version": "1.1.0", "source_bundle_id": identity,
        "corpus_manifest_id": "c" * 64, "snapshot_id": "6" * 64,
        "probe_report_id": "7" * 64, "postbuild_readiness_id": "8" * 64,
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


def _metadata_row(index: int, width: int = 4) -> dict:
    text = f"fixture content {index} — 中文"
    return {
        "chunk_id": f"chunk:{index:0{width}d}",
        "document_id": f"doc:{index}",
        "content_text": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "metadata_hash": hashlib.sha256(f"meta:{index}".encode()).hexdigest(),
        "available_at": "2026-01-01T00:00:00Z",
        "ticker_associations": ["AAPL"],
        "source_class": "reported_news",
        "chunk_profile_version": "news_v2",
        "status": "active",
        "eligibility": "eligible",
        "dedup_cluster_id": f"cluster:{index}",
        "cluster_first_available_at": "2026-01-01T00:00:00Z",
        "representative_document_id": f"doc:{index}",
    }


def _metadata_generator(count: int, width: int | None = None):
    """Single-use stream of metadata rows in frozen chunk_id order."""
    width = width or max(4, len(str(count - 1)))
    for index in range(count):
        yield _metadata_row(index, width=width)


class _SingleUseMetadata:
    """Iterable that forbids len(), repeat traversal, and full conversion."""

    def __init__(self, count: int):
        self._inner = _metadata_generator(count)
        self._exhausted = False

    def __iter__(self):
        if self._exhausted:
            raise RuntimeError("single-use metadata iterable was traversed twice")
        return self

    def __next__(self):
        try:
            return next(self._inner)
        except StopIteration:
            self._exhausted = True
            raise

    def __len__(self):
        raise AssertionError("production import must not call len() on metadata iterable")


def _artifact(tmp_path, count: int) -> tuple[Path, Path]:
    from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts

    bundle, _ = _bundle(tmp_path, count)
    artifact = tmp_path / "artifact"
    build_embedding_artifacts(
        bundle, artifact,
        embed_batch=lambda batch: np.ones((len(batch), 1024), dtype=np.float32),
        mock=True, code_revision=CODE_REVISION,
    )
    return bundle, artifact


class _TakeBuilder:
    def __init__(self, rows):
        self.rows = rows

    def to_arrow(self):
        import pyarrow as pa

        return pa.Table.from_pylist(copy.deepcopy(self.rows))

    def to_batches(self):
        raise AttributeError("to_batches is unavailable in this lancedb build")

    def to_list(self):
        raise AssertionError("builder to_list must not be used by persisted validation")


class StagingTable:
    """Fake LanceDB table that records bounded reads and can inject failures."""

    def __init__(self, *, name: str = "staging", fail_on_add: int | None = None, fail_exc: Exception | None = None):
        self.name = name
        self.rows: list[dict] = []
        self.add_batch_sizes: list[int] = []
        self.add_calls = 0
        self.take_offsets_sizes: list[int] = []
        self.update_calls = 0
        self.delete_calls = 0
        self.fail_on_add = fail_on_add
        self.fail_exc = fail_exc or RuntimeError("injected add failure")

    def add(self, batch):
        self.add_calls += 1
        self.add_batch_sizes.append(len(batch))
        if self.fail_on_add is not None and self.add_calls == self.fail_on_add:
            raise self.fail_exc
        self.rows.extend(copy.deepcopy(batch))

    def delete(self, _predicate):
        self.delete_calls += 1
        self.rows = []

    def count_rows(self):
        return len(self.rows)

    def take_offsets(self, offsets):
        self.take_offsets_sizes.append(len(offsets))
        rows = [dict(self.rows[index]) for index in offsets if index < len(self.rows)]
        return _TakeBuilder(rows)

    def update(self, where=None, values=None):
        self.update_calls += 1
        for row in self.rows:
            row.update(values or {})
        return type("UpdateResult", (), {"rows_updated": len(self.rows)})()

    # Full-table materializers must never be called by production code.
    def to_list(self):
        raise AssertionError("full-table to_list must not be called")

    def to_arrow(self):
        raise AssertionError("full-table to_arrow must not be called")

    def to_pylist(self):
        raise AssertionError("full-table to_pylist must not be called")

    def fetchall(self):
        raise AssertionError("fetchall must not be called")


def _read_pointer(path: Path) -> dict:
    return json.loads(path.read_text())


def test_import_batch_size_constant_is_fixed_at_500(tmp_path):
    from catalyst_data.retrieval.gpu_contract import IMPORT_BATCH_SIZE, import_vectors_to_lancedb

    assert IMPORT_BATCH_SIZE == 500
    bundle, artifact = _artifact(tmp_path / "build", 4)
    table = StagingTable()
    with pytest.raises(ValueError, match="500"):
        import_vectors_to_lancedb(
            bundle, artifact, table, metadata_rows=_metadata_generator(4),
            allow_non_production=True, import_batch_size=501,
        )


def test_import_streams_10000_rows_with_batches_at_most_500(tmp_path):
    from catalyst_data.retrieval.gpu_contract import import_vectors_to_lancedb

    count = 10_002
    bundle, artifact = _artifact(tmp_path / "build", count)
    table = StagingTable(name="staging")
    manifest = import_vectors_to_lancedb(
        bundle, artifact, table,
        metadata_rows=_SingleUseMetadata(count),
        allow_non_production=True,
        active_pointer_path=tmp_path / "active.json",
    )
    assert table.count_rows() == count
    assert table.add_batch_sizes
    assert max(table.add_batch_sizes) <= 500
    assert len(table.add_batch_sizes) == 21  # ceil(10002/500)
    assert all(row["index_manifest_id"] == manifest.index_manifest_id for row in table.rows)
    assert _read_pointer(tmp_path / "active.json")["index_manifest_id"] == manifest.index_manifest_id


def test_import_rejects_batch_size_over_500(tmp_path):
    from catalyst_data.retrieval.gpu_contract import import_vectors_to_lancedb

    bundle, artifact = _artifact(tmp_path / "build", 4)
    with pytest.raises(ValueError, match="500"):
        import_vectors_to_lancedb(
            bundle, artifact, StagingTable(), metadata_rows=_metadata_generator(4),
            allow_non_production=True, import_batch_size=501,
        )


def test_mid_batch_failure_preserves_active_generation(tmp_path):
    from catalyst_data.retrieval.gpu_contract import import_vectors_to_lancedb

    import shutil

    count = 1_500
    bundle, artifact = _artifact(tmp_path / "build", count)
    failing_artifact = tmp_path / "failing_artifact"
    shutil.copytree(artifact, failing_artifact)
    active_table = StagingTable(name="chunks__staging__active")
    pointer = tmp_path / "active.json"
    import_vectors_to_lancedb(
        bundle, artifact, active_table,
        metadata_rows=_metadata_generator(count),
        allow_non_production=True,
        active_pointer_path=pointer,
    )
    pointer_before = pointer.read_bytes()
    rows_before = [dict(row) for row in active_table.rows]

    failing = StagingTable(name="chunks__staging__new", fail_on_add=2)
    with pytest.raises(RuntimeError, match="injected add failure"):
        import_vectors_to_lancedb(
            bundle, failing_artifact, failing,
            metadata_rows=_metadata_generator(count),
            allow_non_production=True,
            active_pointer_path=pointer,
        )
    assert pointer.read_bytes() == pointer_before
    assert active_table.rows == rows_before
    assert failing.count_rows() == 0
    assert failing.delete_calls >= 1


def test_resume_from_checkpoint_matches_uninterrupted_hash(tmp_path):
    from catalyst_data.retrieval.gpu_contract import import_vectors_to_lancedb

    count = 1_500
    bundle, artifact_a = _artifact(tmp_path / "build_a", count)
    import shutil

    artifact_b = tmp_path / "artifact_b"
    shutil.copytree(artifact_a, artifact_b)

    uninterrupted = StagingTable(name="uninterrupted")
    manifest_full = import_vectors_to_lancedb(
        bundle, artifact_a, uninterrupted,
        metadata_rows=_metadata_generator(count),
        allow_non_production=True,
        active_pointer_path=tmp_path / "active_a.json",
    )

    interrupted = StagingTable(
        name="interrupted", fail_on_add=3, fail_exc=KeyboardInterrupt("simulated crash"),
    )
    with pytest.raises(KeyboardInterrupt):
        import_vectors_to_lancedb(
            bundle, artifact_b, interrupted,
            metadata_rows=_metadata_generator(count),
            allow_non_production=True,
            active_pointer_path=tmp_path / "active_b.json",
        )
    assert interrupted.count_rows() == 1_000  # first two batches survived the crash

    manifest_resumed = import_vectors_to_lancedb(
        bundle, artifact_b, interrupted,
        metadata_rows=_metadata_generator(count),
        allow_non_production=True,
        resume=True,
        active_pointer_path=tmp_path / "active_b.json",
    )
    assert interrupted.count_rows() == count
    assert manifest_resumed.artifact_hashes["lancedb_table"] == manifest_full.artifact_hashes["lancedb_table"]
    assert manifest_resumed.index_manifest_id == manifest_full.index_manifest_id


def test_resume_rejects_checkpoint_identity_drift(tmp_path):
    from catalyst_data.retrieval.gpu_contract import LanceImportCheckpoint, import_vectors_to_lancedb
    from catalyst_data.retrieval.index_manifest import IndexManifest

    count = 1_500
    bundle, artifact = _artifact(tmp_path / "build", count)
    raw_manifest = json.loads((artifact / "index_manifest.json").read_text())
    manifest = IndexManifest.from_dict({k: v for k, v in raw_manifest.items() if k != "non_production"})
    checkpoint = LanceImportCheckpoint(
        source_bundle_id="0" * 64,
        snapshot_id=manifest.snapshot_id,
        corpus_manifest_id=manifest.corpus_manifest_id,
        chunk_checksum=manifest.chunk_order_checksum or "",
        vector_checksum=manifest.vectors_checksum or "",
        model_revision=manifest.model_revision,
        completed_row_offset=500,
    )
    (artifact / "import_checkpoint.json").write_text(json.dumps(checkpoint.to_dict()))
    table = StagingTable()
    with pytest.raises(ValueError, match="source_bundle_id"):
        import_vectors_to_lancedb(
            bundle, artifact, table,
            metadata_rows=_metadata_generator(count),
            allow_non_production=True, resume=True,
        )


def test_persisted_validation_uses_bounded_batch_reads_never_full_table(tmp_path):
    from catalyst_data.retrieval.gpu_contract import (
        import_vectors_to_lancedb, validate_embedding_import,
    )

    count = 1_200
    bundle, artifact = _artifact(tmp_path / "build", count)
    table = StagingTable(name="staging")
    manifest = import_vectors_to_lancedb(
        bundle, artifact, table,
        metadata_rows=_metadata_generator(count),
        allow_non_production=True,
    )
    assert table.take_offsets_sizes
    assert max(table.take_offsets_sizes) <= 500

    validated = validate_embedding_import(
        bundle, artifact, allow_non_production=True, lancedb_table=table,
    )
    assert validated.index_manifest_id == manifest.index_manifest_id
    assert table.take_offsets_sizes
    assert max(table.take_offsets_sizes) <= 500


def test_stale_staging_cleanup_preserves_active_generation(tmp_path):
    from catalyst_data.retrieval.gpu_contract import cleanup_stale_staging_tables

    class FakeDb:
        def __init__(self):
            self.tables = {
                "chunks__staging__active": "alive",
                "chunks__staging__stale": "alive",
                "unrelated": "alive",
            }

        def table_names(self):
            return list(self.tables)

        def drop_table(self, name):
            del self.tables[name]

    pointer = tmp_path / "active.json"
    pointer.write_text(json.dumps({
        "schema_version": "active_generation_v1",
        "index_manifest_id": "1" * 64,
        "table_name": "chunks__staging__active",
    }))
    db = FakeDb()
    dropped = cleanup_stale_staging_tables(db, active_pointer_path=pointer, staging_prefix="chunks__staging__")
    assert dropped == ["chunks__staging__stale"]
    assert "chunks__staging__active" in db.tables
    assert "unrelated" in db.tables


# ---------------------------------------------------------------------------
# Task 2: Complete production LanceDB schema and metadata drift gates
# ---------------------------------------------------------------------------


def test_required_metadata_fields_cover_full_production_set():
    from catalyst_data.retrieval.gpu_contract import _REQUIRED_METADATA_FIELDS

    assert _REQUIRED_METADATA_FIELDS == {
        "chunk_id", "document_id", "content_text", "content_hash", "metadata_hash",
        "available_at", "ticker_associations", "source_class",
        "chunk_profile_version", "status", "eligibility", "dedup_cluster_id",
        "cluster_first_available_at", "representative_document_id",
    }


def test_production_lancedb_schema_is_explicit_and_complete():
    import pyarrow as pa

    from catalyst_data.retrieval.gpu_contract import production_lancedb_schema

    schema = production_lancedb_schema()
    names = set(schema.names)
    assert {
        "chunk_id", "document_id", "content_text", "content_hash", "metadata_hash",
        "available_at", "ticker_associations", "source_class",
        "chunk_profile_version", "status", "eligibility", "dedup_cluster_id",
        "cluster_first_available_at", "representative_document_id",
        "corpus_manifest_id", "source_bundle_id", "snapshot_id",
        "probe_report_id", "postbuild_readiness_id", "index_manifest_id",
        "vector",
    }.issubset(names)

    vector_field = schema.field("vector")
    assert pa.types.is_fixed_size_list(vector_field.type)
    assert vector_field.type.value_type == pa.float32()
    assert vector_field.type.list_size == 1024

    ticker_field = schema.field("ticker_associations")
    assert pa.types.is_list(ticker_field.type)
    assert ticker_field.type.value_type == pa.string()

    for name in ("dedup_cluster_id", "cluster_first_available_at", "representative_document_id"):
        assert schema.field(name).nullable is True
    for name in ("chunk_id", "content_hash", "metadata_hash", "vector"):
        assert schema.field(name).nullable is False


def test_real_lancedb_round_trip_with_production_schema(tmp_path):
    import lancedb
    import numpy as np
    import pyarrow as pa

    from catalyst_data.retrieval.dense import retrieve_dense
    from catalyst_data.retrieval.gpu_contract import (
        import_vectors_to_lancedb, production_lancedb_schema, validate_embedding_import,
    )

    count = 4
    bundle, artifact = _artifact(tmp_path / "build", count)
    table = lancedb.connect(str(tmp_path / "lance")).create_table(
        "staging", data=[], schema=production_lancedb_schema(),
    )
    manifest = import_vectors_to_lancedb(
        bundle, artifact, table,
        metadata_rows=_metadata_generator(count),
        allow_non_production=True,
        active_pointer_path=tmp_path / "active.json",
    )
    assert table.count_rows() == count
    assert table.schema.field("vector").type == pa.list_(pa.float32(), 1024)
    validated = validate_embedding_import(
        bundle, artifact, allow_non_production=True, lancedb_table=table,
    )
    assert validated.index_manifest_id == manifest.index_manifest_id

    result = retrieve_dense(
        None,
        np.ones(1024, dtype=np.float32),
        ticker="AAPL",
        cutoff="2026-12-31T23:59:59Z",
        requested_manifest_id=manifest.corpus_manifest_id,
        index_manifest_id=manifest.index_manifest_id,
        lancedb_table=table,
    )
    assert [item.chunk_id for item in result.results] == [
        "chunk:0000", "chunk:0001", "chunk:0002", "chunk:0003",
    ]
    assert result.results[0].dedup_cluster_id == "cluster:0"
    assert result.results[0].representative_document_id == "doc:0"


def test_persisted_validation_rejects_metadata_hash_drift(tmp_path):
    from catalyst_data.retrieval.gpu_contract import (
        import_vectors_to_lancedb, validate_embedding_import,
    )

    count = 4
    bundle, artifact = _artifact(tmp_path / "build", count)
    table = StagingTable(name="staging")
    import_vectors_to_lancedb(
        bundle, artifact, table,
        metadata_rows=_metadata_generator(count),
        allow_non_production=True,
    )
    table.rows[0]["metadata_hash"] = "0" * 64
    with pytest.raises(ValueError, match="artifact hash|metadata_hash"):
        validate_embedding_import(
            bundle, artifact, allow_non_production=True, lancedb_table=table,
        )


def test_import_rejects_metadata_missing_new_fields(tmp_path):
    from catalyst_data.retrieval.gpu_contract import import_vectors_to_lancedb

    count = 4
    bundle, artifact = _artifact(tmp_path / "build", count)

    def rows_without_new_fields():
        for index in range(count):
            row = _metadata_row(index)
            row.pop("metadata_hash")
            yield row

    with pytest.raises(ValueError, match="metadata_hash"):
        import_vectors_to_lancedb(
            bundle, artifact, StagingTable(),
            metadata_rows=rows_without_new_fields(),
            allow_non_production=True,
        )


# ---------------------------------------------------------------------------
# Final Pre-GPU convergence: activation commit boundary (P0)
# ---------------------------------------------------------------------------


def test_checkpoint_removal_failure_rolls_back_before_pointer_commit(monkeypatch, tmp_path):
    """Checkpoint cleanup must precede the pointer commit; a removal failure
    must roll back with the old pointer bytes untouched (never a post-commit
    clear of the active table)."""
    import catalyst_data.retrieval.gpu_contract as gc

    count = 4
    bundle, artifact = _artifact(tmp_path / "build", count)
    table = StagingTable(name="staging")
    pointer = tmp_path / "active.json"
    pointer.write_bytes(b'{"schema_version":"active_generation_v1","old":true}')
    pointer_before = pointer.read_bytes()

    real_remove = gc._remove_if_exists

    def failing_checkpoint_removal(path):
        if Path(path).name == "import_checkpoint.json":
            raise RuntimeError("injected checkpoint removal failure")
        return real_remove(path)

    monkeypatch.setattr(gc, "_remove_if_exists", failing_checkpoint_removal)
    with pytest.raises(RuntimeError, match="checkpoint removal failure"):
        gc.import_vectors_to_lancedb(
            bundle, artifact, table,
            metadata_rows=_metadata_generator(count),
            allow_non_production=True,
            active_pointer_path=pointer,
        )
    # old pointer bytes unchanged
    assert pointer.read_bytes() == pointer_before
    # artifact restored to the pre-import (vectors_staged) state
    raw = json.loads((artifact / "index_manifest.json").read_text())
    assert raw["artifact_state"] == "vectors_staged"
    # staging was cleaned (never active)
    assert table.count_rows() == 0
    assert table.delete_calls >= 1


def test_pointer_atomic_write_failure_keeps_old_pointer_and_restores_artifact(monkeypatch, tmp_path):
    """A failure while atomically writing the pointer (after persisted
    validation) must leave the old pointer bytes and restore the artifact."""
    import catalyst_data.retrieval.gpu_contract as gc

    count = 4
    bundle, artifact = _artifact(tmp_path / "build", count)
    table = StagingTable(name="staging")
    pointer = tmp_path / "active.json"
    pointer.write_bytes(b'{"schema_version":"active_generation_v1","old":true}')
    pointer_before = pointer.read_bytes()
    manifest_before = (artifact / "index_manifest.json").read_bytes()
    checksums_before = (artifact / "checksums.sha256").read_bytes()

    real_atomic_json = gc._atomic_json

    def failing_pointer_write(path, payload):
        if Path(path).name == "active.json":
            raise RuntimeError("injected pointer write failure")
        return real_atomic_json(path, payload)

    monkeypatch.setattr(gc, "_atomic_json", failing_pointer_write)
    with pytest.raises(RuntimeError, match="pointer write failure"):
        gc.import_vectors_to_lancedb(
            bundle, artifact, table,
            metadata_rows=_metadata_generator(count),
            allow_non_production=True,
            active_pointer_path=pointer,
        )
    assert pointer.read_bytes() == pointer_before
    assert (artifact / "index_manifest.json").read_bytes() == manifest_before
    assert (artifact / "checksums.sha256").read_bytes() == checksums_before
    assert table.count_rows() == 0
    assert not (artifact / "import_checkpoint.json").exists()


def test_post_commit_exception_never_clears_active_table(monkeypatch, tmp_path):
    """If an exception surfaces after the pointer was already committed, the
    active table must never be cleared or dropped."""
    import catalyst_data.retrieval.gpu_contract as gc

    count = 4
    bundle, artifact = _artifact(tmp_path / "build", count)
    table = StagingTable(name="staging")
    pointer = tmp_path / "active.json"

    real_atomic_json = gc._atomic_json

    def commit_then_raise(path, payload):
        if Path(path).name == "active.json":
            real_atomic_json(path, payload)  # pointer committed
            raise RuntimeError("injected post-commit exception")
        return real_atomic_json(path, payload)

    monkeypatch.setattr(gc, "_atomic_json", commit_then_raise)
    with pytest.raises(RuntimeError, match="post-commit exception"):
        gc.import_vectors_to_lancedb(
            bundle, artifact, table,
            metadata_rows=_metadata_generator(count),
            allow_non_production=True,
            active_pointer_path=pointer,
        )
    # active table rows must be preserved (no _clear_staging rollback)
    assert table.count_rows() == count
    assert table.delete_calls == 0
    # pointer committed to this table
    assert _read_pointer(pointer)["table_name"] == table.name
