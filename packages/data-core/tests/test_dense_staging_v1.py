"""M3-10: inactive dense staging from fixture vectors (never GPU)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION
from catalyst_data.retrieval.index_manifest import IndexManifest


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_dir(tmp_path: Path, *, count: int = 2) -> tuple[Path, list[str], np.ndarray]:
    chunk_ids = [f"chunk:{index:04d}" for index in range(count)]
    vectors = np.zeros((count, BGE_M3_DIMENSION), dtype=np.float32)
    for index in range(count):
        vectors[index, index] = 1.0
    root = tmp_path / "embedding-artifact"
    root.mkdir()
    np.save(root / "vectors.npy", vectors)
    (root / "chunk_ids.json").write_text(json.dumps(chunk_ids), encoding="utf-8")
    texts = [f"fixture {index}" for index in range(count)]
    lines = []
    for chunk_id, text in zip(chunk_ids, texts):
        record = {
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
        lines.append(json.dumps(record, sort_keys=True, separators=(",", ":")))
    (root / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    checksums = {
        "vectors.npy": _sha(root / "vectors.npy"),
        "chunk_ids.json": _sha(root / "chunk_ids.json"),
    }
    (root / "checksums.sha256").write_text(
        "\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())) + "\n",
        encoding="utf-8",
    )
    return root, chunk_ids, vectors


def _manifest(artifact: Path, *, vector_count: int, source_bundle_id: str = "1" * 64) -> IndexManifest:
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
        corpus_manifest_id="c" * 64,
        source_bundle_id=source_bundle_id,
        snapshot_id="6" * 64,
        probe_report_id="7" * 64,
        postbuild_readiness_id="8" * 64,
        artifact_hashes=hashes,
        code_revision="a" * 40,
        vector_count=vector_count,
        artifact_state="vectors_staged",
    )


def test_stage_dense_missing_api():
    from catalyst_data.index.v1_staging import (
        InactiveDenseCandidate,
        stage_dense,
        validate_dense_candidate,
    )

    assert callable(stage_dense)
    assert callable(validate_dense_candidate)
    assert InactiveDenseCandidate is not None


def test_stage_dense_writes_inactive_candidate_and_preserves_active(tmp_path):
    from catalyst_data.index.v1_staging import stage_dense, validate_dense_candidate

    artifact, _chunk_ids, _vectors = _artifact_dir(tmp_path)
    source_bundle_id = "1" * 64
    manifest = _manifest(artifact, vector_count=2, source_bundle_id=source_bundle_id)
    gold = tmp_path / "lancedb_gold" / source_bundle_id
    gold.mkdir(parents=True)
    active = gold / "active_generation.json"
    active.write_text(
        json.dumps({"schema_version": "active_generation_v1", "table_name": "live_table"}),
        encoding="utf-8",
    )
    before = active.read_bytes()
    manifest_dir = gold / "candidates" / manifest.index_manifest_id
    first = stage_dense(
        manifest_dir,
        embedding_artifact_dir=artifact,
        new_index_manifest=manifest,
        expected_chunk_count=2,
    )
    validate_dense_candidate(first, expected_chunk_count=2)
    payload = json.loads(first.candidate_generation_path.read_text())
    assert payload["schema_version"] == "candidate_generation_v1"
    assert payload["status"] == "inactive"
    assert payload["table_name"] != "live_table"
    assert payload["chunk_count"] == 2
    assert payload["embedding_model"] == BGE_M3_MODEL
    assert payload["embedding_revision"] == BGE_M3_REVISION
    assert payload["embedding_dimension"] == BGE_M3_DIMENSION
    assert active.read_bytes() == before
    second = stage_dense(
        manifest_dir,
        embedding_artifact_dir=artifact,
        new_index_manifest=manifest,
        expected_chunk_count=2,
    )
    assert second.table_name == first.table_name
    assert json.loads(second.candidate_generation_path.read_text()) == payload
    (manifest_dir / "candidate_generation.json").write_text(
        json.dumps({**payload, "table_name": "other_table"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        stage_dense(
            manifest_dir,
            embedding_artifact_dir=artifact,
            new_index_manifest=manifest,
            expected_chunk_count=2,
        )
