"""Inactive dense generation staging (M3-10). Never activates pointers."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.retrieval.gpu_contract import _atomic_json, production_lancedb_schema
from catalyst_data.retrieval.index_manifest import IndexManifest


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
    records = _iter_chunk_records(chunks_path) if chunks_path.is_file() else {}
    if chunks_path.is_file() and set(records) != set(chunk_ids):
        raise ValueError("chunk_id/content identity mismatch")

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
    existing = candidate.candidate_generation_path
    if existing.is_file():
        payload = json.loads(existing.read_text(encoding="utf-8"))
        if payload != _candidate_payload(candidate):
            raise ValueError("existing candidate_generation.json identity mismatch")
        validate_dense_candidate(candidate, expected_chunk_count=expected_chunk_count)
        return candidate

    import lancedb

    db = lancedb.connect(str(manifest_dir))
    table = db.create_table(table_name, data=[], schema=production_lancedb_schema())
    rows = []
    for index, chunk_id in enumerate(chunk_ids):
        record = records.get(chunk_id, {})
        text = str(record.get("content_text") or "")
        content_hash = record.get("content_hash") or hashlib.sha256(text.encode()).hexdigest()
        metadata_hash = record.get("metadata_hash") or ("b" * 64)
        rows.append(
            {
                "chunk_id": chunk_id,
                "document_id": record.get("document_id") or chunk_id,
                "content_text": text,
                "content_hash": content_hash,
                "metadata_hash": metadata_hash,
                "available_at": record.get("available_at") or "1970-01-01T00:00:00Z",
                "ticker_associations": ["AAPL"],
                "source_class": record.get("source_class") or "reported_news",
                "chunk_profile_version": record.get("chunk_profile_version") or "news_v2",
                "status": "active",
                "eligibility": "eligible",
                "dedup_cluster_id": None,
                "cluster_first_available_at": None,
                "representative_document_id": None,
                "corpus_manifest_id": new_index_manifest.corpus_manifest_id,
                "source_bundle_id": new_index_manifest.source_bundle_id,
                "snapshot_id": new_index_manifest.snapshot_id,
                "probe_report_id": new_index_manifest.probe_report_id,
                "postbuild_readiness_id": new_index_manifest.postbuild_readiness_id,
                "index_manifest_id": new_index_manifest.index_manifest_id,
                "vector": vectors[index].tolist(),
            }
        )
    if rows:
        table.add(rows)
    _atomic_json(candidate.candidate_generation_path, _candidate_payload(candidate))
    return candidate
