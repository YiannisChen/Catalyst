"""Inactive dense generation staging (M3-10). Never activates pointers."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.retrieval.gpu_contract import _atomic_json, production_lancedb_schema
from catalyst_data.retrieval.index_manifest import IndexManifest

_STAGING_PAGE_SIZE = 500
_STAGING_ADD_BATCH = 512

# Every LanceDB row field that cannot be synthesized; the artifact-only path
# requires all of them, and the derivative path must supply each one.
_REQUIRED_METADATA_FIELDS = (
    "chunk_id",
    "document_id",
    "content_text",
    "content_hash",
    "metadata_hash",
    "available_at",
    "ticker_associations",
    "chunk_profile_version",
    "source_class",
    "status",
    "eligibility",
)
_NULLABLE_METADATA_FIELDS = (
    "dedup_cluster_id",
    "cluster_first_available_at",
    "representative_document_id",
)


@dataclass(frozen=True)
class InactiveDenseCandidate:
    manifest_dir: Path
    candidate_generation_path: Path
    table_name: str
    index_manifest_id: str
    source_bundle_id: str
    chunk_count: int
    corpus_manifest_id: str
    embedding_model: str
    embedding_revision: str
    embedding_dimension: int


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_checksums(path: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        digest, name = line.split(None, 1)
        mapping[name.strip()] = digest.strip()
    return mapping


def _load_chunk_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise ValueError("chunk_ids.json must be a JSON array of strings")
    return payload


def _iter_chunk_records(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            chunk_id = record.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                raise ValueError("chunks.jsonl missing chunk_id")
            records[chunk_id] = record
    return records


def _validate_ticker_associations(value: Any, *, chunk_id: str) -> list[str]:
    """Parse the persisted JSON ticker list; fail closed on any malformed value."""
    if isinstance(value, list):
        tickers = value
    elif isinstance(value, str) and value:
        try:
            tickers = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ticker_associations malformed for {chunk_id}") from exc
    else:
        raise ValueError(f"ticker_associations missing for {chunk_id}")
    if not isinstance(tickers, list) or not all(isinstance(item, str) for item in tickers):
        raise ValueError(f"ticker_associations must be a JSON array of strings for {chunk_id}")
    return list(tickers)


def _validate_metadata_record(record: dict[str, Any]) -> None:
    """Fail closed when a metadata record omits any required non-synthesized field."""
    chunk_id = record.get("chunk_id")
    if not isinstance(chunk_id, str) or not chunk_id:
        raise ValueError("chunk metadata missing chunk_id")
    missing = [
        name for name in _REQUIRED_METADATA_FIELDS
        if record.get(name) is None or (isinstance(record.get(name), str) and not record[name])
    ]
    if missing:
        raise ValueError(
            "chunk metadata missing required field(s): " + ",".join(missing)
        )
    content_hash = record["content_hash"]
    expected_hash = hashlib.sha256(record["content_text"].encode("utf-8")).hexdigest()
    if content_hash != expected_hash:
        raise ValueError(f"content_hash mismatch for {chunk_id}")
    _validate_ticker_associations(record["ticker_associations"], chunk_id=chunk_id)


def _iter_candidate_metadata(
    conn: sqlite3.Connection,
    *,
    build_id: str,
) -> Iterator[dict[str, Any]]:
    """Stream the authoritative candidate build rows in chunk_id order.

    Reads only ``corpus_build_chunks WHERE build_id=?`` (never the served
    corpus view, never the active generation). Rows are bounded via
    ``fetchmany`` and validated per row: chunk order, content hash, and ticker
    JSON. Missing/placeholder fields fail closed.
    """
    cursor = conn.execute(
        """
        SELECT chunk_id, document_id, content_text, content_hash, metadata_hash,
               available_at, ticker_associations, chunk_profile_version,
               source_class, status, eligibility, dedup_cluster_id,
               cluster_first_available_at, representative_document_id
        FROM corpus_build_chunks
        WHERE build_id=?
        ORDER BY chunk_id COLLATE BINARY
        """,
        (build_id,),
    )
    previous = ""
    while True:
        page = cursor.fetchmany(_STAGING_PAGE_SIZE)
        if not page:
            return
        for raw in page:
            row = {
                "chunk_id": raw[0],
                "document_id": raw[1],
                "content_text": raw[2] or "",
                "content_hash": raw[3],
                "metadata_hash": raw[4],
                "available_at": raw[5],
                "ticker_associations": raw[6],
                "chunk_profile_version": raw[7],
                "source_class": raw[8],
                "status": raw[9],
                "eligibility": raw[10],
                "dedup_cluster_id": raw[11],
                "cluster_first_available_at": raw[12],
                "representative_document_id": raw[13],
            }
            chunk_id = str(row["chunk_id"] or "")
            if not chunk_id or (previous and chunk_id <= previous):
                raise ValueError(
                    "corpus_build_chunks chunk_id order or duplicate violation"
                )
            previous = chunk_id
            _validate_metadata_record(row)
            yield row


def _artifact_metadata_rows(
    artifact_records: dict[str, dict[str, Any]],
    chunk_ids: list[str],
) -> Iterator[dict[str, Any]]:
    """Artifact-only path: requires a complete metadata record per chunk id."""
    for chunk_id in chunk_ids:
        record = artifact_records.get(chunk_id)
        if record is None:
            raise ValueError(f"artifact chunks.jsonl missing {chunk_id}")
        _validate_metadata_record(record)
        yield record


def _candidate_payload(candidate: InactiveDenseCandidate) -> dict[str, Any]:
    return {
        "schema_version": "candidate_generation_v1",
        "status": "inactive",
        "index_manifest_id": candidate.index_manifest_id,
        "source_bundle_id": candidate.source_bundle_id,
        "table_name": candidate.table_name,
        "chunk_count": candidate.chunk_count,
        "corpus_manifest_id": candidate.corpus_manifest_id,
        "embedding_model": candidate.embedding_model,
        "embedding_revision": candidate.embedding_revision,
        "embedding_dimension": candidate.embedding_dimension,
    }


def validate_dense_candidate(
    candidate: InactiveDenseCandidate,
    *,
    expected_chunk_count: int,
    expected_model: str = BGE_M3_MODEL,
    expected_revision: str = BGE_M3_REVISION,
    expected_dimension: int = BGE_M3_DIMENSION,
) -> None:
    if candidate.chunk_count != expected_chunk_count:
        raise ValueError("dense candidate chunk_count mismatch")
    if candidate.embedding_model != expected_model:
        raise ValueError("dense candidate embedding_model mismatch")
    if candidate.embedding_revision != expected_revision:
        raise ValueError("dense candidate embedding_revision mismatch")
    if candidate.embedding_dimension != expected_dimension:
        raise ValueError("dense candidate embedding_dimension mismatch")
    path = Path(candidate.candidate_generation_path)
    if not path.is_file():
        raise ValueError("candidate_generation.json missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = _candidate_payload(candidate)
    if payload != expected:
        raise ValueError("candidate_generation.json identity mismatch")
    if payload.get("status") != "inactive":
        raise ValueError("dense candidate must remain inactive")


def stage_dense(
    manifest_dir: Path,
    *,
    embedding_artifact_dir: Path,
    new_index_manifest: Any,
    expected_chunk_count: int,
    source_conn: sqlite3.Connection | None = None,
    source_build_id: str | None = None,
) -> InactiveDenseCandidate:
    if not isinstance(new_index_manifest, IndexManifest):
        raise TypeError("new_index_manifest must be an IndexManifest")
    artifact = Path(embedding_artifact_dir)
    vectors_path = artifact / "vectors.npy"
    chunk_ids_path = artifact / "chunk_ids.json"
    checksum_path = artifact / "checksums.sha256"
    chunks_path = artifact / "chunks.jsonl"
    for path in (vectors_path, chunk_ids_path, checksum_path):
        if not path.is_file():
            raise ValueError("embedding artifact is incomplete")
    listed = _parse_checksums(checksum_path)
    for name in ("vectors.npy", "chunk_ids.json"):
        if listed.get(name) != _file_sha256(artifact / name):
            raise ValueError(f"checksum mismatch for {name}")
    chunk_ids = _load_chunk_ids(chunk_ids_path)
    vectors = np.load(vectors_path, mmap_mode="r")
    if vectors.ndim != 2 or vectors.shape[0] != expected_chunk_count:
        raise ValueError("vector count mismatch")
    if vectors.shape[1] != BGE_M3_DIMENSION:
        raise ValueError("vector dimension mismatch")
    if len(chunk_ids) != expected_chunk_count:
        raise ValueError("chunk_id count mismatch")
    if new_index_manifest.model_name != BGE_M3_MODEL:
        raise ValueError("embedding model mismatch")
    if new_index_manifest.model_revision != BGE_M3_REVISION:
        raise ValueError("embedding revision mismatch")
    if new_index_manifest.dimension != BGE_M3_DIMENSION:
        raise ValueError("embedding dimension mismatch")
    if new_index_manifest.vector_count != expected_chunk_count:
        raise ValueError("index manifest vector_count mismatch")

    artifact_records = _iter_chunk_records(chunks_path) if chunks_path.is_file() else {}
    if artifact_records and set(artifact_records) != set(chunk_ids):
        raise ValueError("chunk_id/content identity mismatch")

    has_db = source_conn is not None or source_build_id is not None
    if has_db and (source_conn is None or source_build_id is None):
        raise ValueError(
            "stage_dense requires both source_conn and source_build_id together"
        )
    if not has_db and not artifact_records:
        raise ValueError(
            "no authoritative chunk metadata source: pass source_conn/source_build_id "
            "or a complete artifact chunks.jsonl"
        )

    manifest_dir = Path(manifest_dir)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    table_name = f"candidate_{new_index_manifest.index_manifest_id[:16]}"
    candidate = InactiveDenseCandidate(
        manifest_dir=manifest_dir,
        candidate_generation_path=manifest_dir / "candidate_generation.json",
        table_name=table_name,
        index_manifest_id=new_index_manifest.index_manifest_id,
        source_bundle_id=new_index_manifest.source_bundle_id,
        chunk_count=expected_chunk_count,
        corpus_manifest_id=new_index_manifest.corpus_manifest_id,
        embedding_model=BGE_M3_MODEL,
        embedding_revision=BGE_M3_REVISION,
        embedding_dimension=BGE_M3_DIMENSION,
    )
    generation_path = candidate.candidate_generation_path
    manifest_path = Path(candidate.manifest_dir) / "index_manifest.json"
    manifest_payload = new_index_manifest.to_dict()

    def _require_persisted_manifest() -> None:
        if not manifest_path.is_file():
            raise ValueError(
                "candidate_generation.json exists without a matching "
                "index_manifest.json; refusing half-published candidate resume"
            )
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("staged index_manifest.json is malformed") from exc
        if existing_manifest != manifest_payload:
            raise ValueError("staged index_manifest.json identity mismatch")

    if generation_path.is_file():
        payload = json.loads(generation_path.read_text(encoding="utf-8"))
        if payload != _candidate_payload(candidate):
            raise ValueError("existing candidate_generation.json identity mismatch")
        # Resume requires both published records and exact identity agreement.
        # A generation record alone (crash between manifest/generation) is a
        # half-published candidate and must never be accepted.
        _require_persisted_manifest()
        validate_dense_candidate(candidate, expected_chunk_count=expected_chunk_count)
        return candidate
    if manifest_path.is_file():
        raise ValueError(
            "index_manifest.json exists without candidate_generation.json; "
            "refusing half-published candidate resume"
        )
    # A LanceDB table without either published identity record is also
    # half-published (crash before the manifest/generation publish step).
    table_dir = Path(candidate.manifest_dir) / f"{table_name}.lance"
    if table_dir.exists():
        raise ValueError(
            "candidate LanceDB table exists without published identity "
            "records; refusing half-published candidate resume"
        )

    def metadata_factory() -> Iterator[dict[str, Any]]:
        """Return a fresh authoritative metadata iterator for each pass.

        Both the derivative path and the artifact path build independent,
        repeatable iterators, so the validation pass and the staging pass can
        each consume the full source without materializing it in memory.
        """
        if has_db:
            return _iter_candidate_metadata(source_conn, build_id=source_build_id)
        return _artifact_metadata_rows(artifact_records, chunk_ids)

    # Pass 1 — complete bounded validation BEFORE any LanceDB mutation.
    # Validates count, order, duplicates, required fields, field types,
    # content_hash, ticker JSON and artifact/source alignment for every row.
    # No table is connected/created, no row is added and no
    # candidate_generation.json is written during this pass.
    validation_iter = metadata_factory()
    previous_chunk_id = ""
    for index, chunk_id in enumerate(chunk_ids):
        try:
            row = next(validation_iter)
        except StopIteration as exc:
            raise ValueError(
                "chunk metadata source has fewer rows than artifact chunk_ids"
            ) from exc
        if row["chunk_id"] != chunk_id:
            raise ValueError(
                "chunk_id mismatch between artifact and chunk metadata source"
            )
        if previous_chunk_id and chunk_id <= previous_chunk_id:
            raise ValueError("chunk_id order or duplicate violation")
        previous_chunk_id = chunk_id
        if artifact_records:
            artifact_row = artifact_records[chunk_id]
            if artifact_row.get("content_hash") != row["content_hash"]:
                raise ValueError(f"artifact/content_hash mismatch for {chunk_id}")
        # Per-row required-field/type/content-hash/ticker validation was
        # already performed by the metadata iterator (_validate_metadata_record).
    if next(validation_iter, None) is not None:
        raise ValueError("chunk metadata source has more rows than artifact chunk_ids")

    # Pass 2 — only after the complete validation pass succeeds may the
    # candidate table be created; rows are streamed in bounded batches.
    import lancedb

    db = lancedb.connect(str(manifest_dir))
    table = None
    try:
        table = db.create_table(table_name, data=[], schema=production_lancedb_schema())
        staging_iter = metadata_factory()
        batch: list[dict[str, Any]] = []
        for index, chunk_id in enumerate(chunk_ids):
            row = next(staging_iter)  # pass 1 already proved row availability
            if row["chunk_id"] != chunk_id:
                raise ValueError(
                    "chunk_id mismatch between artifact and chunk metadata source"
                )
            ticker_associations = _validate_ticker_associations(
                row["ticker_associations"], chunk_id=chunk_id
            )
            batch.append(
                {
                    "chunk_id": chunk_id,
                    "document_id": row["document_id"],
                    "content_text": row["content_text"],
                    "content_hash": row["content_hash"],
                    "metadata_hash": row["metadata_hash"],
                    "available_at": row["available_at"],
                    "ticker_associations": ticker_associations,
                    "source_class": row["source_class"],
                    "chunk_profile_version": row["chunk_profile_version"],
                    "status": row["status"],
                    "eligibility": row["eligibility"],
                    "dedup_cluster_id": row["dedup_cluster_id"],
                    "cluster_first_available_at": row["cluster_first_available_at"],
                    "representative_document_id": row["representative_document_id"],
                    "corpus_manifest_id": new_index_manifest.corpus_manifest_id,
                    "source_bundle_id": new_index_manifest.source_bundle_id,
                    "snapshot_id": new_index_manifest.snapshot_id,
                    "probe_report_id": new_index_manifest.probe_report_id,
                    "postbuild_readiness_id": new_index_manifest.postbuild_readiness_id,
                    "index_manifest_id": new_index_manifest.index_manifest_id,
                    "vector": vectors[index].tolist(),
                }
            )
            if len(batch) >= _STAGING_ADD_BATCH:
                table.add(batch)
                batch = []
        if next(staging_iter, None) is not None:
            raise ValueError("chunk metadata source has more rows than artifact chunk_ids")
        if batch:
            table.add(batch)
    except BaseException:
        # A failure after table creation must never leave a partial candidate
        # capable of being reused: drop the table we created.
        if table is not None:
            try:
                db.drop_table(table_name)
            except Exception:
                pass
        raise
    # Make the staged candidate directory self-describing.  The matching
    # index_manifest.json is published first (from the in-memory authoritative
    # IndexManifest), and candidate_generation.json only after it is safely
    # persisted, so a crash between the two can never look complete.
    _atomic_json(manifest_path, manifest_payload)
    _atomic_json(candidate.candidate_generation_path, _candidate_payload(candidate))
    return candidate
