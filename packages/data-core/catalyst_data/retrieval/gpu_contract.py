"""Offline-testable source-bundle and GPU artifact contracts.

This module contains no model download or provider access. The production driver
injects the actual CUDA embedder only after the source bundle is authenticated.

Production LanceDB import contract (B6-L manager review):

- Fixed ``IMPORT_BATCH_SIZE`` (max 500).
- Source bundle records, metadata rows, chunk IDs, and memmapped vectors are
  consumed lockstep in bounded batches.
- Every batch creates at most 500 LanceDB records and is added immediately.
- No production path retains Python row/vector containers linear in total rows.
- Import writes into a staging table; only after full persisted validation
  succeeds is the active pointer atomically updated.
- Failures delete/mark the staging generation; the active generation is never
  touched.
- Batch checkpoint/resume is bound to source_bundle_id, snapshot_id,
  corpus_manifest_id, chunk checksum, vector checksum, model revision, and the
  completed row offset.
- Content hashes are computed incrementally over bounded batches.
- Persisted validation reads the table through bounded batch reads
  (``take_offsets``/scanner), never ``to_pylist()``/``to_list()``/``fetchall()``
  over the whole table.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from .index_manifest import IndexManifest
from .source_bundle import _chunk_record_hash, _content_hash, _stream_source_bundle_id

IMPORT_BATCH_SIZE = 500
MAX_PERSISTED_VALIDATION_BATCH = 500

_REQUIRED_METADATA_FIELDS = frozenset({
    "chunk_id", "document_id", "content_text", "content_hash", "metadata_hash",
    "available_at", "ticker_associations", "source_class",
    "chunk_profile_version", "status", "eligibility", "dedup_cluster_id",
    "cluster_first_available_at", "representative_document_id",
})
def production_lancedb_schema():
    """The single production PyArrow schema for the persisted dense index.

    ``vector`` is a fixed-size list of 1024 float32 values and
    ``ticker_associations`` is a list of strings; dedup identity fields are
    nullable. Every metadata field from the served corpus stream is present so
    metadata drift fails before the active pointer can switch.
    """
    import pyarrow as pa

    return pa.schema([
        pa.field("chunk_id", pa.string(), nullable=False),
        pa.field("document_id", pa.string(), nullable=False),
        pa.field("content_text", pa.string(), nullable=False),
        pa.field("content_hash", pa.string(), nullable=False),
        pa.field("metadata_hash", pa.string(), nullable=False),
        pa.field("available_at", pa.string(), nullable=False),
        pa.field("ticker_associations", pa.list_(pa.string()), nullable=False),
        pa.field("source_class", pa.string(), nullable=False),
        pa.field("chunk_profile_version", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("eligibility", pa.string(), nullable=False),
        pa.field("dedup_cluster_id", pa.string(), nullable=True),
        pa.field("cluster_first_available_at", pa.string(), nullable=True),
        pa.field("representative_document_id", pa.string(), nullable=True),
        pa.field("corpus_manifest_id", pa.string(), nullable=False),
        pa.field("source_bundle_id", pa.string(), nullable=False),
        pa.field("snapshot_id", pa.string(), nullable=False),
        pa.field("probe_report_id", pa.string(), nullable=False),
        pa.field("postbuild_readiness_id", pa.string(), nullable=False),
        pa.field("index_manifest_id", pa.string(), nullable=False),
        pa.field("vector", pa.list_(pa.float32(), 1024), nullable=False),
    ])


_MISSING = object()
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class VerifiedSourceBundle:
    path: Path
    source_bundle_id: str
    snapshot_id: str
    corpus_manifest_id: str
    probe_report_id: str
    postbuild_readiness_id: str
    chunk_count: int


@dataclass(frozen=True)
class LanceImportCheckpoint:
    """Batch resume state bound to the full import identity chain."""

    source_bundle_id: str
    snapshot_id: str
    corpus_manifest_id: str
    chunk_checksum: str
    vector_checksum: str
    model_revision: str
    completed_row_offset: int

    def __post_init__(self) -> None:
        for name in (
            "source_bundle_id", "snapshot_id", "corpus_manifest_id",
            "chunk_checksum", "vector_checksum",
        ):
            if not _HEX64.fullmatch(getattr(self, name)):
                raise ValueError(f"checkpoint {name} must be lowercase SHA-256")
        if not _HEX40.fullmatch(self.model_revision):
            raise ValueError("checkpoint model_revision must be a pinned 40-character revision")
        if self.completed_row_offset < 0:
            raise ValueError("checkpoint completed_row_offset must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "lancedb_import_checkpoint_v1",
            "source_bundle_id": self.source_bundle_id,
            "snapshot_id": self.snapshot_id,
            "corpus_manifest_id": self.corpus_manifest_id,
            "chunk_checksum": self.chunk_checksum,
            "vector_checksum": self.vector_checksum,
            "model_revision": self.model_revision,
            "completed_row_offset": self.completed_row_offset,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LanceImportCheckpoint":
        if raw.get("schema_version") != "lancedb_import_checkpoint_v1":
            raise ValueError("checkpoint schema mismatch")
        return cls(
            source_bundle_id=raw["source_bundle_id"],
            snapshot_id=raw["snapshot_id"],
            corpus_manifest_id=raw["corpus_manifest_id"],
            chunk_checksum=raw["chunk_checksum"],
            vector_checksum=raw["vector_checksum"],
            model_revision=raw["model_revision"],
            completed_row_offset=int(raw["completed_row_offset"]),
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _lancedb_table_identity_hash(
    *,
    corpus_manifest_id: str,
    source_bundle_id: str,
    chunk_order_checksum: str,
    vectors_checksum: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "corpus_manifest_id": corpus_manifest_id,
                "source_bundle_id": source_bundle_id,
                "chunk_order_checksum": chunk_order_checksum,
                "vectors_checksum": vectors_checksum,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _checksums(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (path / "checksums.sha256").read_text().splitlines():
        digest, name = line.split(None, 1)
        values[name.strip()] = digest
    return values


def _iter_bundle_records(bundle_path: Path):
    with (Path(bundle_path) / "chunks.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


class _CharReader:
    """Buffered single-character reader used by the streaming JSON parser."""

    def __init__(self, handle: Any, block_size: int = 64 * 1024) -> None:
        self._handle = handle
        self._block_size = block_size
        self._buffer = ""
        self._position = 0

    def read(self) -> str:
        if self._position >= len(self._buffer):
            block = self._handle.read(self._block_size)
            if not block:
                return ""
            self._buffer = block
            self._position = 0
        character = self._buffer[self._position]
        self._position += 1
        return character


def _read_json_string(reader: _CharReader) -> str:
    """Read a JSON string body after the opening quote."""
    _ESCAPES = {
        '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
        "n": "\n", "r": "\r", "t": "\t",
    }
    characters: list[str] = []
    while True:
        character = reader.read()
        if not character:
            raise ValueError("unterminated string in chunk_ids.json")
        if character == '"':
            return "".join(characters)
        if character == "\\":
            escaped = reader.read()
            if not escaped:
                raise ValueError("unterminated escape in chunk_ids.json")
            if escaped in _ESCAPES:
                characters.append(_ESCAPES[escaped])
            elif escaped == "u":
                hex_digits: list[str] = []
                for _ in range(4):
                    digit = reader.read()
                    if not digit:
                        raise ValueError("malformed \\u escape in chunk_ids.json")
                    hex_digits.append(digit)
                characters.append(chr(int("".join(hex_digits), 16)))
            else:
                raise ValueError("malformed escape in chunk_ids.json")
        else:
            characters.append(character)
    raise ValueError("unterminated string in chunk_ids.json")  # pragma: no cover


def _iter_chunk_ids(path: Path) -> Iterable[str]:
    """Stream a flat JSON array of chunk-id strings without materializing it.

    The artifact writes ``json.dumps(chunk_ids, separators=(",", ":")) + "\\n"``.
    This parser walks the raw bytes, yields each string, and validates the array
    structure so a tampered file cannot produce a wrong row count.
    """
    with path.open("r", encoding="utf-8") as handle:
        reader = _CharReader(handle)

        def skip_whitespace() -> str:
            while True:
                character = reader.read()
                if not character:
                    return ""
                if not character.isspace():
                    return character

        first = skip_whitespace()
        if first != "[":
            raise ValueError("chunk_ids.json must be a JSON array of strings")
        expect_value = True
        while True:
            character = skip_whitespace()
            if not character:
                raise ValueError("chunk_ids.json ended before closing bracket")
            if character == "]":
                trailing = skip_whitespace()
                if trailing:
                    raise ValueError("chunk_ids.json has trailing data")
                return
            if expect_value:
                if character != '"':
                    raise ValueError("chunk_ids.json elements must be JSON strings")
                yield _read_json_string(reader)
                expect_value = False
            else:
                if character != ",":
                    raise ValueError("chunk_ids.json expected ',' between elements")
                expect_value = True


def _chunk_ids_order_checksum(path: Path) -> str:
    """SHA-256 of the canonical chunk-order JSON array text (no trailing newline)."""
    size = path.stat().st_size
    if size < 2:
        raise ValueError("chunk_ids.json is too small to be a valid order file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        total = 0
        while total < size:
            block = handle.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total == size:
                block = block[:-1]  # drop the trailing newline written by the driver
            digest.update(block)
    return digest.hexdigest()


def _count_rows(table: Any) -> int:
    if not hasattr(table, "count_rows"):
        raise TypeError("LanceDB table must provide count_rows() for bounded validation")
    return int(table.count_rows())


def _pyarrow_table_to_rows(arrow: Any) -> list[dict[str, Any]]:
    """Convert a bounded pyarrow table without calling whole-table to_pylist()."""
    if not hasattr(arrow, "column_names"):
        raise TypeError("persisted batch read must return a pyarrow table")
    names = list(arrow.column_names)
    rows: list[dict[str, Any]] = []
    for row_index in range(arrow.num_rows):
        row: dict[str, Any] = {}
        for name in names:
            scalar = arrow.column(name)[row_index]
            if scalar is None:
                row[name] = None
            elif hasattr(scalar, "as_py"):
                row[name] = scalar.as_py()
            else:
                row[name] = scalar
        rows.append(row)
    return rows


def _read_offsets(table: Any, offsets: Sequence[int]) -> list[dict[str, Any]]:
    """Bounded row read for at most ``MAX_PERSISTED_VALIDATION_BATCH`` offsets.

    Only bounded read APIs are used; the production path never calls
    ``to_pylist()``/``to_list()``/``fetchall()`` on the persisted table.
    """
    if not offsets:
        return []
    if hasattr(table, "take_offsets"):
        builder = table.take_offsets(list(offsets))
        if hasattr(builder, "to_arrow"):
            arrow = builder.to_arrow()
            if hasattr(arrow, "column_names"):
                return _pyarrow_table_to_rows(arrow)
        if hasattr(builder, "to_batches"):
            rows: list[dict[str, Any]] = []
            for batch in builder.to_batches():
                if hasattr(batch, "column_names"):
                    rows.extend(_pyarrow_table_to_rows(batch))
                elif isinstance(batch, (list, tuple)):
                    rows.extend(batch)
                else:
                    raise TypeError("persisted batch read returned an unsupported batch type")
            return rows
        if hasattr(builder, "to_list"):
            return builder.to_list()
        raise TypeError("LanceDB take_offsets builder must expose to_arrow() or to_list()")
    if hasattr(table, "to_lance"):
        dataset = table.to_lance()
        scanner = dataset.scanner(batch_size=len(offsets))
        rows: list[dict[str, Any]] = []
        for batch in scanner.to_reader():
            if hasattr(batch, "column_names"):
                rows.extend(_pyarrow_table_to_rows(batch))
            elif isinstance(batch, (list, tuple)):
                rows.extend(batch)
            else:
                raise TypeError("persisted scanner batch returned an unsupported batch type")
        return rows
    raise TypeError("LanceDB table must provide take_offsets() or to_lance() for bounded validation")


def _iter_persisted_row_batches(
    table: Any, *, batch_size: int, chunk_count: int | None = None,
) -> Iterable[list[dict[str, Any]]]:
    """Yield persisted rows in bounded batches (max ``MAX_PERSISTED_VALIDATION_BATCH``)."""
    if batch_size < 1 or batch_size > MAX_PERSISTED_VALIDATION_BATCH:
        raise ValueError(f"persisted validation batch size must be <= {MAX_PERSISTED_VALIDATION_BATCH}")
    if chunk_count is None:
        chunk_count = _count_rows(table)
    for start in range(0, chunk_count, batch_size):
        offsets = list(range(start, min(start + batch_size, chunk_count)))
        yield _read_offsets(table, offsets)


def _validate_persisted_lancedb_table(
    table: Any,
    *,
    chunk_ids_path: Path,
    chunk_count: int,
    dimension: int,
    index_manifest_id: str,
    expected_identities: dict[str, str],
    expected_hash: str,
    batch_size: int = MAX_PERSISTED_VALIDATION_BATCH,
) -> None:
    """Validate the persisted staging table with bounded batch reads only."""
    count = _count_rows(table)
    if count != chunk_count:
        raise ValueError(f"LanceDB persisted row count mismatch: {count} != {chunk_count}")
    chunk_ids = iter(_iter_chunk_ids(chunk_ids_path))
    previous_id = ""
    digest = hashlib.sha256()
    processed = 0
    for batch in _iter_persisted_row_batches(table, batch_size=batch_size, chunk_count=count):
        if len(batch) > batch_size:
            raise ValueError("persisted validation batch exceeds maximum")
        canonical_rows: list[dict[str, Any]] = []
        for row in batch:
            chunk_id = row.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                raise ValueError("persisted row missing chunk_id")
            if previous_id and chunk_id <= previous_id:
                raise ValueError("persisted chunk_id order or duplicate violation")
            expected_id = next(chunk_ids, _MISSING)
            if expected_id is _MISSING or expected_id != chunk_id:
                raise ValueError("persisted chunk_id drift from artifact order")
            previous_id = chunk_id
            for key, expected in expected_identities.items():
                if row.get(key) != expected:
                    raise ValueError(f"persisted row {key} mismatch")
            if row.get("index_manifest_id") != index_manifest_id:
                raise ValueError("LanceDB persisted index_manifest_id mismatch")
            metadata_hash = row.get("metadata_hash")
            if not isinstance(metadata_hash, str) or _HEX64.fullmatch(metadata_hash) is None:
                raise ValueError("LanceDB persisted metadata_hash mismatch")
            for key in ("dedup_cluster_id", "cluster_first_available_at", "representative_document_id"):
                value = row.get(key)
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"LanceDB persisted {key} must be null or string")
            vector = row.get("vector")
            if not isinstance(vector, (list, tuple)) or len(vector) != dimension:
                raise ValueError("persisted row vector dimension mismatch")
            if not all(
                isinstance(value, (int, float)) and math.isfinite(float(value))
                for value in vector
            ):
                raise ValueError("persisted row vector is not finite")
            canonical_rows.append({
                key: value for key, value in row.items() if key != "index_manifest_id"
            })
            processed += 1
        digest.update(_canonical(canonical_rows))
    if next(chunk_ids, _MISSING) is not _MISSING:
        raise ValueError("persisted rows are missing chunks")
    if processed != chunk_count:
        raise ValueError("persisted validation processed count mismatch")
    if digest.hexdigest() != expected_hash:
        raise ValueError("LanceDB persisted artifact hash mismatch")


def validate_persisted_lancedb_table(
    table: Any,
    *,
    chunk_ids_path: Path,
    chunk_count: int,
    dimension: int,
    index_manifest_id: str,
    expected_identities: dict[str, str],
    expected_hash: str,
    batch_size: int = MAX_PERSISTED_VALIDATION_BATCH,
) -> None:
    """Public wrapper around the production bounded-batch persisted validator.

    Reuses the single production persisted-table hash algorithm; never call
    whole-table ``to_pylist()``/``to_list()``/``fetchall()``. Every persisted
    drift (row count, chunk order, identity columns, dimension, vector
    finiteness, artifact hash) fails closed before the active pointer may be
    treated as committed or used for retrieval.
    """
    _validate_persisted_lancedb_table(
        table,
        chunk_ids_path=chunk_ids_path,
        chunk_count=chunk_count,
        dimension=dimension,
        index_manifest_id=index_manifest_id,
        expected_identities=expected_identities,
        expected_hash=expected_hash,
        batch_size=batch_size,
    )


def _clear_staging(table: Any) -> None:
    """Delete or mark the staging generation after an in-process failure."""
    if hasattr(table, "delete"):
        try:
            table.delete("1=1")
        except Exception:
            pass


def _pointer_matches_table(pointer_path: Path, table: Any) -> bool:
    """True only when the active pointer already names this table.

    This is the single source of truth for "the generation is committed": once
    the pointer names the table, no failure may clear/drop/roll back the table.
    A missing or corrupt pointer is treated as not-committed.
    """
    try:
        raw = json.loads(Path(pointer_path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    return raw.get("table_name") == getattr(table, "name", None)


def _stamp_index_manifest_id(table: Any, index_manifest_id: str) -> None:
    if not hasattr(table, "update"):
        raise TypeError("LanceDB table must provide update() to bind index_manifest_id")
    table.update(values={"index_manifest_id": index_manifest_id})


def verify_source_bundle(
    bundle_path: Path,
    *,
    expected_source_bundle_id: str | None = None,
    expected_snapshot_id: str | None = None,
    expected_corpus_manifest_id: str | None = None,
    expected_probe_report_id: str | None = None,
    expected_postbuild_readiness_id: str | None = None,
) -> VerifiedSourceBundle:
    bundle = Path(bundle_path)
    expected_files = {"chunks.jsonl", "source_bundle_manifest.json", "checksums.sha256"}
    if {path.name for path in bundle.iterdir() if path.is_file()} != expected_files:
        raise ValueError("source bundle artifact set mismatch")
    checksums = _checksums(bundle)
    if set(checksums) != {"chunks.jsonl", "source_bundle_manifest.json"}:
        raise ValueError("source bundle checksum listing mismatch")
    for name, digest in checksums.items():
        if _file_sha256(bundle / name) != digest:
            raise ValueError(f"checksum mismatch for {name}")
    manifest = json.loads((bundle / "source_bundle_manifest.json").read_text())
    if manifest.get("schema_version") != "1.1.0":
        raise ValueError("source bundle schema mismatch")
    expected = {
        "source_bundle_id": expected_source_bundle_id,
        "snapshot_id": expected_snapshot_id,
        "corpus_manifest_id": expected_corpus_manifest_id,
        "probe_report_id": expected_probe_report_id,
        "postbuild_readiness_id": expected_postbuild_readiness_id,
    }
    for key, value in expected.items():
        if value is not None and manifest.get(key) != value:
            raise ValueError(f"source bundle {key} mismatch")
    previous = ""
    count = 0
    with (bundle / "chunks.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                raise ValueError("blank line in chunks.jsonl")
            record = json.loads(line)
            chunk_id = record.get("chunk_id", "")
            if not chunk_id or (previous and chunk_id <= previous):
                raise ValueError("chunk_id order or duplicate violation")
            if record.get("corpus_manifest_id") != manifest.get("corpus_manifest_id"):
                raise ValueError("record corpus_manifest_id mismatch")
            if record.get("content_hash") != _content_hash(record.get("content_text", "")):
                raise ValueError(f"content_hash mismatch for {chunk_id}")
            previous = chunk_id
            count += 1
    if count != int(manifest.get("chunk_count", -1)):
        raise ValueError("source bundle count mismatch")
    def record_hashes():
        with (bundle / "chunks.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                yield _chunk_record_hash(record)

    computed = _stream_source_bundle_id(
        corpus_manifest_id=manifest["corpus_manifest_id"], snapshot_id=manifest["snapshot_id"],
        probe_report_id=manifest["probe_report_id"], postbuild_readiness_id=manifest["postbuild_readiness_id"],
        record_hashes=record_hashes(),
    )
    if computed != manifest.get("source_bundle_id"):
        raise ValueError("source_bundle_id mismatch")
    if expected_source_bundle_id is not None and computed != expected_source_bundle_id:
        raise ValueError("source_bundle_id mismatch")
    return VerifiedSourceBundle(
        bundle, computed, manifest["snapshot_id"], manifest["corpus_manifest_id"],
        manifest["probe_report_id"], manifest["postbuild_readiness_id"], count,
    )


def embed_with_oom_backoff(
    texts: Sequence[str], embed_batch: Callable[[Sequence[str]], np.ndarray], *, initial_batch_size: int = 32
) -> np.ndarray:
    if initial_batch_size < 1:
        raise ValueError("initial_batch_size must be positive")
    batch_size = min(initial_batch_size, 32)
    vectors: list[np.ndarray] = []
    offset = 0
    while offset < len(texts):
        current = min(batch_size, len(texts) - offset)
        try:
            matrix = np.asarray(embed_batch(texts[offset:offset + current]))
            if matrix.dtype != np.float32 or matrix.ndim != 2 or matrix.shape != (current, 1024):
                raise ValueError("embedder returned non-contract matrix")
            norms = np.linalg.norm(matrix, axis=1)
            if np.any(norms == 0):
                raise ValueError("zero vector returned")
            vectors.append((matrix / norms[:, None]).astype(np.float32, copy=False))
            offset += current
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower():
                raise
            if current == 1:
                raise RuntimeError("CUDA OOM at batch=1; hard fail") from exc
            _clear_cuda_cache()
            batch_size = max(1, current // 2)
    return np.vstack(vectors) if vectors else np.empty((0, 1024), dtype=np.float32)


def _clear_cuda_cache() -> None:
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def validate_embedding_import(
    bundle_path: Path,
    artifact_path: Path,
    *,
    allow_non_production: bool = False,
    lancedb_table: Any = None,
) -> IndexManifest:
    verified = verify_source_bundle(bundle_path)
    artifact = Path(artifact_path)
    early_manifest = artifact / "index_manifest.json"
    if early_manifest.is_file():
        early = json.loads(early_manifest.read_text())
        if early.get("non_production") and not allow_non_production:
            raise ValueError("non_production mock artifact rejected")
    required_files = {"vectors.npy", "chunk_ids.json", "index_manifest.json", "checksums.sha256"}
    allowed_files = required_files | {"checkpoint.json", "import_checkpoint.json"}
    if not required_files.issubset({path.name for path in artifact.iterdir() if path.is_file()}):
        raise ValueError("embedding artifact set mismatch")
    if {path.name for path in artifact.iterdir() if path.is_file()} - allowed_files:
        raise ValueError("embedding artifact has unexpected files")
    checksums = _checksums(artifact)
    if set(checksums) != {"vectors.npy", "chunk_ids.json", "index_manifest.json"}:
        raise ValueError("embedding checksum listing mismatch")
    for name, digest in checksums.items():
        if _file_sha256(artifact / name) != digest:
            raise ValueError(f"embedding checksum mismatch for {name}")
    raw_manifest = json.loads((artifact / "index_manifest.json").read_text())
    if raw_manifest.get("non_production") and not allow_non_production:
        raise ValueError("non_production mock artifact rejected")
    if raw_manifest.get("source_bundle_id") != verified.source_bundle_id:
        raise ValueError("embedding source_bundle_id mismatch")
    for key, expected in {
        "snapshot_id": verified.snapshot_id,
        "corpus_manifest_id": verified.corpus_manifest_id,
        "probe_report_id": verified.probe_report_id,
        "postbuild_readiness_id": verified.postbuild_readiness_id,
    }.items():
        if raw_manifest.get(key) != expected:
            raise ValueError(f"embedding {key} mismatch")
    chunk_ids_path = artifact / "chunk_ids.json"
    previous_chunk_id = ""
    chunk_id_count = 0
    for chunk_id in _iter_chunk_ids(chunk_ids_path):
        if not chunk_id or (previous_chunk_id and chunk_id <= previous_chunk_id):
            raise ValueError("embedding chunk-id order/count mismatch")
        previous_chunk_id = chunk_id
        chunk_id_count += 1
    if chunk_id_count != verified.chunk_count:
        raise ValueError("embedding chunk-id order/count mismatch")
    bundle_iter = iter(_iter_bundle_records(bundle_path))
    for chunk_id in _iter_chunk_ids(chunk_ids_path):
        record = next(bundle_iter, _MISSING)
        if record is _MISSING or record["chunk_id"] != chunk_id:
            raise ValueError("embedding chunk-id frozen order mismatch")
    if next(bundle_iter, _MISSING) is not _MISSING:
        raise ValueError("embedding chunk-id frozen order mismatch")
    vectors = np.load(artifact / "vectors.npy", mmap_mode="r")
    if vectors.dtype != np.float32 or vectors.shape != (verified.chunk_count, 1024):
        raise ValueError("embedding vector shape/dtype mismatch")
    manifest = IndexManifest.from_dict({key: value for key, value in raw_manifest.items() if key != "non_production"})
    manifest.assert_approved_identities(
        source_bundle_id=verified.source_bundle_id,
        snapshot_id=verified.snapshot_id,
        corpus_manifest_id=verified.corpus_manifest_id,
        probe_report_id=verified.probe_report_id,
        postbuild_readiness_id=verified.postbuild_readiness_id,
    )
    if manifest.vector_count != verified.chunk_count:
        raise ValueError("embedding vector count mismatch")
    vectors_checksum = _file_sha256(artifact / "vectors.npy")
    chunk_ids_checksum = _file_sha256(chunk_ids_path)
    if manifest.vectors_checksum != vectors_checksum:
        raise ValueError("embedding vectors checksum mismatch")
    if manifest.artifact_hashes["vectors.npy"] != vectors_checksum:
        raise ValueError("IndexManifest vectors artifact hash mismatch")
    if manifest.artifact_hashes["chunk_ids.json"] != chunk_ids_checksum:
        raise ValueError("IndexManifest chunk index artifact hash mismatch")
    if manifest.chunk_order_checksum != _chunk_ids_order_checksum(chunk_ids_path):
        raise ValueError("embedding chunk order checksum mismatch")
    for start in range(0, verified.chunk_count, MAX_PERSISTED_VALIDATION_BATCH):
        block = vectors[start:start + MAX_PERSISTED_VALIDATION_BATCH]
        norms = np.linalg.norm(block, axis=1)
        if not np.allclose(norms, 1.0, rtol=1e-4, atol=1e-4):
            raise ValueError("embedding vectors are not L2-normalized")
    if manifest.artifact_state == "lancedb_imported":
        if lancedb_table is None:
            raise ValueError("LanceDB table is required to verify imported artifact")
        _validate_persisted_lancedb_table(
            lancedb_table,
            chunk_ids_path=chunk_ids_path,
            chunk_count=verified.chunk_count,
            dimension=manifest.dimension,
            index_manifest_id=manifest.index_manifest_id,
            expected_identities={
                "corpus_manifest_id": manifest.corpus_manifest_id,
                "source_bundle_id": manifest.source_bundle_id,
                "snapshot_id": manifest.snapshot_id,
                "probe_report_id": manifest.probe_report_id,
                "postbuild_readiness_id": manifest.postbuild_readiness_id,
            },
            expected_hash=manifest.artifact_hashes["lancedb_table"],
        )
    return manifest


def import_vectors_to_lancedb(
    bundle_path: Path,
    artifact_path: Path,
    table: Any,
    *,
    metadata_rows: Iterable[dict[str, Any]],
    allow_non_production: bool = False,
    import_batch_size: int = IMPORT_BATCH_SIZE,
    checkpoint_path: str | Path | None = None,
    resume: bool = False,
    active_pointer_path: str | Path | None = None,
) -> IndexManifest:
    """Import verified vectors into a staging LanceDB table in bounded batches.

    On success the staging table is validated through bounded batch reads and
    the active pointer is atomically updated. On an ordinary exception the
    staging table is cleared and the checkpoint removed; on ``KeyboardInterrupt``
    the staging rows and checkpoint are preserved for ``resume=True``.
    """
    if import_batch_size < 1 or import_batch_size > IMPORT_BATCH_SIZE:
        raise ValueError(f"import_batch_size must be between 1 and {IMPORT_BATCH_SIZE}")
    manifest = validate_embedding_import(
        bundle_path, artifact_path, allow_non_production=allow_non_production,
        # On resume the artifact may already be lancedb_imported (interrupt
        # during persisted validation); validate it against the staging table.
        lancedb_table=table if resume else None,
    )
    artifact = Path(artifact_path)
    raw_manifest = json.loads((artifact / "index_manifest.json").read_text())
    checkpoint = Path(checkpoint_path) if checkpoint_path is not None else artifact / "import_checkpoint.json"
    pointer = Path(active_pointer_path) if active_pointer_path is not None else artifact.parent / "active_generation.json"
    chunk_ids_path = artifact / "chunk_ids.json"
    chunk_count = manifest.vector_count
    vectors = np.load(artifact / "vectors.npy", mmap_mode="r")

    completed = 0
    if resume:
        if not checkpoint.is_file():
            raise ValueError("resume requires an existing import checkpoint")
        stored = LanceImportCheckpoint.from_dict(json.loads(checkpoint.read_text(encoding="utf-8")))
        for key, actual, expected in (
            ("source_bundle_id", stored.source_bundle_id, manifest.source_bundle_id),
            ("snapshot_id", stored.snapshot_id, manifest.snapshot_id),
            ("corpus_manifest_id", stored.corpus_manifest_id, manifest.corpus_manifest_id),
            ("chunk_checksum", stored.chunk_checksum, manifest.chunk_order_checksum or ""),
            ("vector_checksum", stored.vector_checksum, manifest.vectors_checksum or ""),
            ("model_revision", stored.model_revision, manifest.model_revision),
        ):
            if actual != expected:
                raise ValueError(f"resume checkpoint {key} mismatch")
        completed = stored.completed_row_offset
        if completed < 0 or completed > chunk_count:
            raise ValueError("resume checkpoint completed_row_offset out of range")
        # Every checkpoint is written at batch boundaries; the final partial
        # batch is also a checkpoint (completed == chunk_count) and is a valid
        # resume point when the interrupt happened after the last add.
        if completed != chunk_count and completed % import_batch_size != 0:
            raise ValueError("resume checkpoint must be batch-aligned")
        if _count_rows(table) != completed:
            raise ValueError("resume staging table row count does not match checkpoint")
    elif checkpoint.is_file():
        raise ValueError("import checkpoint exists; pass resume=True to continue")

    manifest_path = artifact / "index_manifest.json"
    checksums_path = artifact / "checksums.sha256"
    previous_manifest = manifest_path.read_bytes()
    previous_checksums = checksums_path.read_bytes()
    final_manifest = manifest
    try:
        bundle_iter = iter(_iter_bundle_records(bundle_path))
        metadata_iter = iter(metadata_rows)
        chunk_ids_iter = iter(_iter_chunk_ids(chunk_ids_path))
        digest = hashlib.sha256()
        batch_meta: list[dict[str, Any]] = []
        batch_start = 0
        offset = 0
        previous_chunk_id = ""
        for chunk_id in chunk_ids_iter:
            if not chunk_id or (previous_chunk_id and chunk_id <= previous_chunk_id):
                raise ValueError("chunk_id order or duplicate violation")
            previous_chunk_id = chunk_id
            source_record = next(bundle_iter, _MISSING)
            if source_record is _MISSING:
                raise ValueError("source bundle missing chunk during import")
            if source_record.get("chunk_id") != chunk_id:
                raise ValueError("source bundle lockstep drift during import")
            raw_metadata = next(metadata_iter, _MISSING)
            if raw_metadata is _MISSING:
                raise ValueError("import metadata missing chunk during import")
            row = dict(raw_metadata)
            if row.get("chunk_id") != chunk_id:
                raise ValueError("metadata lockstep drift during import")
            if row.get("content_text") != source_record.get("content_text"):
                raise ValueError(f"LanceDB import content mismatch: {chunk_id}")
            if row.get("content_hash", source_record.get("content_hash")) != source_record.get("content_hash"):
                raise ValueError(f"LanceDB import content hash mismatch: {chunk_id}")
            missing = sorted(_REQUIRED_METADATA_FIELDS - set(row))
            if missing:
                raise ValueError("LanceDB import metadata missing fields: " + ",".join(missing))
            batch_meta.append(row)
            if len(batch_meta) == import_batch_size or offset == chunk_count - 1:
                vector_slice = vectors[batch_start:offset + 1]
                records: list[dict[str, Any]] = []
                for index, metadata_row in enumerate(batch_meta):
                    vector = np.asarray(vector_slice[index], dtype=np.float32)
                    if not np.isfinite(vector).all():
                        raise ValueError(f"LanceDB import vector is not finite: {metadata_row['chunk_id']}")
                    record = dict(metadata_row)
                    record.update({
                        "corpus_manifest_id": manifest.corpus_manifest_id,
                        "source_bundle_id": manifest.source_bundle_id,
                        "snapshot_id": manifest.snapshot_id,
                        "probe_report_id": manifest.probe_report_id,
                        "postbuild_readiness_id": manifest.postbuild_readiness_id,
                        "index_manifest_id": manifest.index_manifest_id,
                        "vector": vector.tolist(),
                    })
                    records.append(record)
                canonical = [
                    {key: value for key, value in record.items() if key != "index_manifest_id"}
                    for record in records
                ]
                digest.update(_canonical(canonical))
                if offset + 1 > completed:
                    if len(records) > import_batch_size:
                        raise ValueError("import batch exceeds configured batch size")
                    table.add(records)
                    completed = offset + 1
                    _atomic_json(
                        checkpoint,
                        LanceImportCheckpoint(
                            source_bundle_id=manifest.source_bundle_id,
                            snapshot_id=manifest.snapshot_id,
                            corpus_manifest_id=manifest.corpus_manifest_id,
                            chunk_checksum=manifest.chunk_order_checksum or "",
                            vector_checksum=manifest.vectors_checksum or "",
                            model_revision=manifest.model_revision,
                            completed_row_offset=completed,
                        ).to_dict(),
                    )
                batch_meta = []
                batch_start = offset + 1
            offset += 1
        if next(metadata_iter, _MISSING) is not _MISSING:
            raise ValueError("LanceDB import metadata has extra chunks")
        if next(bundle_iter, _MISSING) is not _MISSING:
            raise ValueError("LanceDB import source bundle has extra rows")
        if offset != chunk_count:
            raise ValueError("LanceDB import chunk count mismatch")

        table_hash = digest.hexdigest()
        final_manifest = replace(
            manifest,
            artifact_hashes={**manifest.artifact_hashes, "lancedb_table": table_hash},
            artifact_state="lancedb_imported",
        )
        final_raw = final_manifest.to_dict()
        if raw_manifest.get("non_production"):
            final_raw["non_production"] = True
        _atomic_json(manifest_path, final_raw)
        checksums = {
            name: _file_sha256(artifact / name)
            for name in ("vectors.npy", "chunk_ids.json", "index_manifest.json")
        }
        _atomic_text(
            checksums_path,
            "\n".join(f"{digest}  {name}" for name, digest in sorted(checksums.items())) + "\n",
        )
        _stamp_index_manifest_id(table, final_manifest.index_manifest_id)
        validate_embedding_import(
            bundle_path, artifact_path, allow_non_production=allow_non_production,
            lancedb_table=table,
        )
        # All fallible work completes before the pointer commit: persisted
        # validation and artifact manifest/checksum updates above, and the
        # checkpoint cleanup immediately below. The pointer update is the
        # single, last commit point.
        _remove_if_exists(checkpoint)
        _atomic_json(pointer, {
            "schema_version": "active_generation_v1",
            "index_manifest_id": final_manifest.index_manifest_id,
            "table_name": getattr(table, "name", None),
            "source_bundle_id": final_manifest.source_bundle_id,
            "snapshot_id": final_manifest.snapshot_id,
            "corpus_manifest_id": final_manifest.corpus_manifest_id,
            "chunk_count": chunk_count,
        })
    except KeyboardInterrupt:
        # Preserve staging rows and the checkpoint so a later resume can
        # continue from the last completed batch offset.
        raise
    except Exception:
        if _pointer_matches_table(pointer, table):
            # The generation is already active. Never clear/drop the active
            # table or roll back the artifact; surface the failure unchanged.
            raise
        try:
            _clear_staging(table)
        except Exception:
            pass
        try:
            _remove_if_exists(checkpoint)
        except Exception:
            pass
        _atomic_bytes(manifest_path, previous_manifest)
        _atomic_bytes(checksums_path, previous_checksums)
        raise
    return final_manifest


def cleanup_stale_staging_tables(
    db: Any,
    *,
    active_pointer_path: str | Path,
    staging_prefix: str,
) -> list[str]:
    """Drop staging tables that are not referenced by the active generation."""
    pointer_raw = json.loads(Path(active_pointer_path).read_text(encoding="utf-8"))
    active_table = pointer_raw.get("table_name")
    names = db.table_names() if hasattr(db, "table_names") else []
    dropped: list[str] = []
    for name in names:
        if name.startswith(staging_prefix) and name != active_table:
            db.drop_table(name)
            dropped.append(name)
    return dropped


__all__ = [
    "IMPORT_BATCH_SIZE", "LanceImportCheckpoint", "VerifiedSourceBundle",
    "cleanup_stale_staging_tables", "embed_with_oom_backoff",
    "import_vectors_to_lancedb", "production_lancedb_schema",
    "validate_embedding_import", "verify_source_bundle",
]
