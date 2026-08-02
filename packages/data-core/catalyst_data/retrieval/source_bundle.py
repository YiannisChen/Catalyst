"""Production source bundle export (text + hashes, no vectors)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator

from catalyst_data.corpus.streaming_publication import served_chunks_relation
from catalyst_data.manifests.universe import sha256_identity


_PAGE_SIZE = 500
_SEARCHABLE_STATUSES = ("active", "pending_embedding", "embedded", "metadata_only")
_ALLOWED_ARTIFACTS = frozenset(("chunks.jsonl", "source_bundle_manifest.json"))
_MISSING = object()


def _chunk_record_hash(rec: dict[str, Any]) -> str:
    return sha256_identity(rec)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_checksums(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError("checksums.sha256 missing")
    mapping: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise ValueError(f"malformed checksums line: {line!r}")
            mapping[parts[1].strip()] = parts[0].strip()
    if not mapping:
        raise ValueError("checksums.sha256 empty")
    return mapping


def _iter_chunk_rows(
    conn: sqlite3.Connection,
    chunks_relation: str,
    corpus_manifest_id: str,
    *,
    exclude_tombstones: bool = False,
) -> Iterator[tuple[Any, ...]]:
    """Yield selected chunks through a bounded SQLite cursor."""
    statuses = ",".join("?" for _ in _SEARCHABLE_STATUSES)
    tombstone_clause = (
        " AND chunk_id NOT IN (SELECT chunk_id FROM corpus_tombstones)"
        if exclude_tombstones
        else ""
    )
    cursor = conn.execute(
        f"""
        SELECT chunk_id, document_id, content_text, content_hash, metadata_hash,
               available_at, ticker_associations, manifest_id, chunk_profile_version
        FROM {chunks_relation}
        WHERE manifest_id=? AND status IN ({statuses}){tombstone_clause}
        ORDER BY chunk_id
        """,
        (corpus_manifest_id, *_SEARCHABLE_STATUSES),
    )
    while True:
        page = cursor.fetchmany(_PAGE_SIZE)
        if not page:
            return
        yield from page


def _record_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    chunk_id, document_id, content_text, content_hash, metadata_hash = row[:5]
    available_at, ticker_associations, manifest_id, profile = row[5:9]
    if not isinstance(chunk_id, str) or not chunk_id:
        raise ValueError("missing identity fields for chunk")
    if not isinstance(document_id, str) or not document_id:
        raise ValueError(f"missing identity fields for chunk_id={chunk_id}")
    if not isinstance(available_at, str) or not available_at:
        raise ValueError(f"missing identity fields for chunk_id={chunk_id}")
    if ticker_associations is None or not manifest_id or not profile:
        raise ValueError(f"missing identity fields for chunk_id={chunk_id}")
    if not isinstance(content_hash, str) or len(content_hash) != 64:
        raise ValueError(f"invalid content_hash for {chunk_id}")
    if not isinstance(metadata_hash, str) or len(metadata_hash) != 64:
        raise ValueError(f"invalid metadata_hash for {chunk_id}")
    content = content_text or ""
    if content_hash != _content_hash(content):
        raise ValueError(f"content_hash mismatch for chunk_id={chunk_id}")
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "content_text": content,
        "content_hash": content_hash,
        "metadata_hash": metadata_hash,
        "available_at": available_at,
        "ticker_associations": ticker_associations,
        "corpus_manifest_id": manifest_id,
        "chunk_profile_version": profile,
    }


def _iter_valid_records(
    conn: sqlite3.Connection,
    chunks_relation: str,
    corpus_manifest_id: str,
    *,
    exclude_tombstones: bool = False,
) -> Iterator[dict[str, Any]]:
    previous_id = ""
    for row in _iter_chunk_rows(
        conn,
        chunks_relation,
        corpus_manifest_id,
        exclude_tombstones=exclude_tombstones,
    ):
        record = _record_from_row(row)
        chunk_id = record["chunk_id"]
        if previous_id and chunk_id <= previous_id:
            if chunk_id == previous_id:
                raise ValueError(f"duplicate chunk_id: {chunk_id}")
            raise ValueError("chunk_id not strictly sorted")
        previous_id = chunk_id
        yield record


def _json_scalar(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _stream_source_bundle_id(
    *,
    corpus_manifest_id: str,
    snapshot_id: str,
    probe_report_id: str,
    postbuild_readiness_id: str,
    record_hashes: Iterable[str],
) -> str:
    """Hash the schema 1.1.0 identity without retaining the hash sequence."""
    digest = hashlib.sha256()
    digest.update(b'{"corpus_manifest_id":')
    digest.update(_json_scalar(corpus_manifest_id))
    digest.update(b',"ordered_chunk_record_hashes":[')
    first = True
    for record_hash in record_hashes:
        if not first:
            digest.update(b",")
        digest.update(_json_scalar(record_hash))
        first = False
    digest.update(b'],"postbuild_readiness_id":')
    digest.update(_json_scalar(postbuild_readiness_id))
    digest.update(b',"probe_report_id":')
    digest.update(_json_scalar(probe_report_id))
    digest.update(b',"schema_version":"1.1.0","snapshot_id":')
    digest.update(_json_scalar(snapshot_id))
    digest.update(b"}")
    return digest.hexdigest()


def _scan_source(
    conn: sqlite3.Connection,
    *,
    chunks_relation: str,
    corpus_manifest_id: str,
    snapshot_id: str,
    probe_report_id: str,
    postbuild_readiness_id: str,
    exclude_tombstones: bool = False,
) -> tuple[int, bool, str]:
    count = 0
    has_filing = False

    def record_hashes() -> Iterator[str]:
        nonlocal count, has_filing
        for record in _iter_valid_records(
            conn,
            chunks_relation,
            corpus_manifest_id,
            exclude_tombstones=exclude_tombstones,
        ):
            count += 1
            has_filing = has_filing or record["chunk_profile_version"] == "filing_v3"
            yield _chunk_record_hash(record)

    bundle_id = _stream_source_bundle_id(
        corpus_manifest_id=corpus_manifest_id,
        snapshot_id=snapshot_id,
        probe_report_id=probe_report_id,
        postbuild_readiness_id=postbuild_readiness_id,
        record_hashes=record_hashes(),
    )
    return count, has_filing, bundle_id


def _write_source_rows(
    conn: sqlite3.Connection,
    *,
    chunks_relation: str,
    corpus_manifest_id: str,
    output,
    snapshot_id: str,
    probe_report_id: str,
    postbuild_readiness_id: str,
    exclude_tombstones: bool = False,
) -> tuple[int, bool, str]:
    count = 0
    has_filing = False

    def record_hashes() -> Iterator[str]:
        nonlocal count, has_filing
        for record in _iter_valid_records(
            conn,
            chunks_relation,
            corpus_manifest_id,
            exclude_tombstones=exclude_tombstones,
        ):
            output.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            output.write("\n")
            count += 1
            has_filing = has_filing or record["chunk_profile_version"] == "filing_v3"
            yield _chunk_record_hash(record)

    bundle_id = _stream_source_bundle_id(
        corpus_manifest_id=corpus_manifest_id,
        snapshot_id=snapshot_id,
        probe_report_id=probe_report_id,
        postbuild_readiness_id=postbuild_readiness_id,
        record_hashes=record_hashes(),
    )
    output.flush()
    os.fsync(output.fileno())
    return count, has_filing, bundle_id


def _verify_existing_bundle_integrity(
    conn: sqlite3.Connection,
    bundle_dir: Path,
    *,
    expected_source_bundle_id: str,
    chunks_relation: str,
    universe_manifest_id: str,
    corpus_manifest_id: str,
    snapshot_id: str,
    probe_report_id: str,
    postbuild_readiness_id: str,
    expected_count: int,
    exclude_tombstones: bool,
) -> None:
    """Verify an existing bundle in lockstep without corpus-sized memory."""
    checksums_path = bundle_dir / "checksums.sha256"
    checksums = _parse_checksums(checksums_path)
    entries = {
        path.name for path in bundle_dir.iterdir() if path.name != "checksums.sha256"
    }
    on_disk_files = {
        path.name
        for path in bundle_dir.iterdir()
        if path.is_file() and path.name != "checksums.sha256"
    }
    listed = set(checksums)
    if "checksums.sha256" in listed:
        raise ValueError("checksums.sha256 must not list itself")
    if (
        listed != _ALLOWED_ARTIFACTS
        or entries != _ALLOWED_ARTIFACTS
        or on_disk_files != _ALLOWED_ARTIFACTS
    ):
        raise ValueError("source bundle artifact set is not vector-free")
    for name, expected in checksums.items():
        if _file_sha256(bundle_dir / name) != expected:
            raise ValueError(f"checksum mismatch for {name}")

    manifest_path = bundle_dir / "source_bundle_manifest.json"
    if not manifest_path.is_file():
        raise ValueError("source_bundle_manifest.json missing")
    with manifest_path.open("r", encoding="utf-8") as handle:
        existing = json.load(handle)
    for key, expected in (
        ("schema_version", "1.1.0"),
        ("universe_manifest_id", universe_manifest_id),
        ("source_bundle_id", expected_source_bundle_id),
        ("corpus_manifest_id", corpus_manifest_id),
        ("snapshot_id", snapshot_id),
        ("probe_report_id", probe_report_id),
        ("postbuild_readiness_id", postbuild_readiness_id),
    ):
        if existing.get(key) != expected:
            raise ValueError(f"{key} mismatch in existing bundle")
    if int(existing.get("chunk_count", -1)) != expected_count:
        raise ValueError("chunk_count mismatch in existing bundle")

    chunks_path = bundle_dir / "chunks.jsonl"
    if not chunks_path.is_file():
        raise ValueError("chunks.jsonl missing")
    db_stream = _iter_valid_records(
        conn,
        chunks_relation,
        corpus_manifest_id,
        exclude_tombstones=exclude_tombstones,
    )
    previous_id = ""
    count = 0

    def record_hashes() -> Iterator[str]:
        nonlocal previous_id, count
        with chunks_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                if not raw_line.strip():
                    raise ValueError("blank line in chunks.jsonl")
                actual = json.loads(raw_line)
                chunk_id = actual.get("chunk_id") or ""
                if previous_id and chunk_id <= previous_id:
                    raise ValueError("chunks.jsonl is not strictly sorted")
                previous_id = chunk_id
                expected = next(db_stream, _MISSING)
                if expected is _MISSING:
                    raise ValueError("chunks.jsonl has extra rows")
                if actual != expected:
                    raise ValueError(f"chunk record drift at index {count}")
                count += 1
                yield _chunk_record_hash(actual)
        if next(db_stream, _MISSING) is not _MISSING:
            raise ValueError("chunks.jsonl is missing rows")

    recomputed_id = _stream_source_bundle_id(
        corpus_manifest_id=corpus_manifest_id,
        snapshot_id=snapshot_id,
        probe_report_id=probe_report_id,
        postbuild_readiness_id=postbuild_readiness_id,
        record_hashes=record_hashes(),
    )
    if count != expected_count:
        raise ValueError("chunks.jsonl line count mismatch")
    if recomputed_id != expected_source_bundle_id:
        raise ValueError("recomputed source_bundle_id mismatch")


def export_source_bundle(
    conn: sqlite3.Connection,
    *,
    universe_manifest_id: str,
    corpus_manifest_id: str,
    snapshot_id: str,
    probe_cutoff: str,
    output_dir: Path,
    probe_report_path: Path,
    postbuild_readiness_report_path: Path,
) -> str:
    """Export certified source bundle for frozen corpus_manifest_id."""
    from catalyst_data.pre_b6_probes import (
        load_and_verify_postbuild_readiness_report,
        load_and_verify_probe_report,
        verify_probe_report_against_db,
    )

    postbuild = load_and_verify_postbuild_readiness_report(
        conn,
        Path(postbuild_readiness_report_path),
        expected_universe_manifest_id=universe_manifest_id,
        expected_snapshot_id=snapshot_id,
        expected_corpus_manifest_id=corpus_manifest_id,
    )
    probe_report = load_and_verify_probe_report(
        Path(probe_report_path),
        expected_snapshot_id=snapshot_id,
        expected_corpus_manifest_id=corpus_manifest_id,
        expected_universe_manifest_id=universe_manifest_id,
        expected_postbuild_readiness_id=postbuild["postbuild_readiness_id"],
    )
    probe_report_id = str(probe_report["probe_report_id"])
    postbuild_readiness_id = str(postbuild["postbuild_readiness_id"])

    row = conn.execute(
        "SELECT manifest_json, is_current FROM corpus_manifest WHERE manifest_id=?",
        (corpus_manifest_id,),
    ).fetchone()
    if row is None:
        raise ValueError("corpus_manifest_id not found in corpus_manifest")
    if int(row[1] or 0) != 1:
        raise ValueError("corpus_manifest is_current must be 1 for production export")
    try:
        manifest = json.loads(row[0] or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("invalid corpus_manifest.manifest_json") from exc
    if manifest.get("certified_snapshot_identity") != snapshot_id:
        raise ValueError("snapshot_id does not match certified_snapshot_identity")

    lexical = conn.execute(
        "SELECT corpus_manifest_id, mode_served FROM lexical_index_state "
        "WHERE singleton_id=1"
    ).fetchone()
    if lexical is None:
        raise ValueError("lexical_index_state missing")
    if lexical != (corpus_manifest_id, "fts5"):
        raise ValueError("lexical_index_state is not bound to current fts5 corpus")

    chunks_relation = served_chunks_relation(conn)
    has_tombstones = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_tombstones'"
    ).fetchone()
    exclude_tombstones = (
        chunks_relation == "corpus_chunks" and has_tombstones is not None
    )
    selected_count, has_filing, source_bundle_id = _scan_source(
        conn,
        chunks_relation=chunks_relation,
        corpus_manifest_id=corpus_manifest_id,
        snapshot_id=snapshot_id,
        probe_report_id=probe_report_id,
        postbuild_readiness_id=postbuild_readiness_id,
        exclude_tombstones=exclude_tombstones,
    )
    if selected_count == 0:
        raise ValueError("production source bundle must be non-empty")
    if not has_filing:
        raise ValueError("production source bundle requires filing_v3 chunks")
    inventory_count = manifest.get("inventory_row_count")
    if inventory_count is not None and int(inventory_count) != selected_count:
        raise ValueError(
            f"selected row count {selected_count} != normalized inventory {inventory_count}"
        )

    verify_probe_report_against_db(
        conn,
        probe_report,
        expected_universe_manifest_id=universe_manifest_id,
        expected_snapshot_id=snapshot_id,
        expected_corpus_manifest_id=corpus_manifest_id,
        expected_probe_cutoff=probe_cutoff,
        postbuild_readiness_report_path=Path(postbuild_readiness_report_path),
    )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    final_dir = output_dir / f"source_{source_bundle_id}"
    if final_dir.exists():
        _verify_existing_bundle_integrity(
            conn,
            final_dir,
            expected_source_bundle_id=source_bundle_id,
            chunks_relation=chunks_relation,
            universe_manifest_id=universe_manifest_id,
            corpus_manifest_id=corpus_manifest_id,
            snapshot_id=snapshot_id,
            probe_report_id=probe_report_id,
            postbuild_readiness_id=postbuild_readiness_id,
            expected_count=selected_count,
            exclude_tombstones=exclude_tombstones,
        )
        return source_bundle_id

    with tempfile.TemporaryDirectory(dir=str(output_dir)) as tmp:
        bundle_dir = Path(tmp) / f"source_{source_bundle_id}"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        chunks_path = bundle_dir / "chunks.jsonl"
        with chunks_path.open("w", encoding="utf-8") as handle:
            written_count, written_filing, written_id = _write_source_rows(
                conn,
                chunks_relation=chunks_relation,
                corpus_manifest_id=corpus_manifest_id,
                output=handle,
                snapshot_id=snapshot_id,
                probe_report_id=probe_report_id,
                postbuild_readiness_id=postbuild_readiness_id,
                exclude_tombstones=exclude_tombstones,
            )
        if (
            written_count != selected_count
            or not written_filing
            or written_id != source_bundle_id
        ):
            raise ValueError("source rows changed during bounded export")
        manifest_body = {
            "schema_version": "1.1.0",
            "source_bundle_id": source_bundle_id,
            "corpus_manifest_id": corpus_manifest_id,
            "snapshot_id": snapshot_id,
            "universe_manifest_id": probe_report["universe_manifest_id"],
            "probe_report_id": probe_report_id,
            "postbuild_readiness_id": postbuild_readiness_id,
            "chunk_count": selected_count,
        }
        manifest_path = bundle_dir / "source_bundle_manifest.json"
        with manifest_path.open("w", encoding="utf-8") as handle:
            json.dump(manifest_body, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        artifact_names = sorted(
            path.name
            for path in bundle_dir.iterdir()
            if path.is_file() and path.name != "checksums.sha256"
        )
        checksums = {name: _file_sha256(bundle_dir / name) for name in artifact_names}
        checksum_path = bundle_dir / "checksums.sha256"
        with checksum_path.open("w", encoding="utf-8") as handle:
            handle.write("\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for name, expected in checksums.items():
            if _file_sha256(bundle_dir / name) != expected:
                raise RuntimeError(f"checksum verification failed for {name}")
        os.replace(str(bundle_dir), str(final_dir))
    return source_bundle_id
