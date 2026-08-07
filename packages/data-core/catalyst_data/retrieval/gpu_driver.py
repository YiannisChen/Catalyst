"""Streaming corpus source-bundle embedding driver.

The module is intentionally dependency-light. Real model loading is lazy and
only the CLI's non-mock path may request it; tests inject an embedder callable.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable

import numpy as np

from catalyst_data.config import (
    BGE_M3_DIMENSION,
    BGE_M3_MODEL,
    BGE_M3_REVISION,
)
from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION

from .gpu_contract import (
    _lancedb_table_identity_hash,
    embed_with_oom_backoff,
    verify_source_bundle,
)
from .index_manifest import IndexManifest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
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


def _iter_chunk_texts(bundle: Path):
    with (bundle / "chunks.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            yield record["chunk_id"], record["content_text"]


def _write_shard(path: Path, chunk_ids: list[str], vectors: np.ndarray) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        np.savez_compressed(handle, chunk_ids=np.asarray(chunk_ids), vectors=vectors.astype(np.float32, copy=False))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _chunk_order_checksum(chunk_ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(chunk_ids, separators=(",", ":")).encode()).hexdigest()


CHECKPOINT_SCHEMA_VERSION = "embedding_checkpoint_v1"


def _checkpoint_identity(
    verified: Any,
    *,
    mock: bool,
    shard_size: int,
    code_revision: str,
) -> dict[str, Any]:
    """Full checkpoint identity: data identities + code/model/tokenizer +
    artifact contract fields. Resume validates every field before embedding."""
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "source_bundle_id": verified.source_bundle_id,
        "snapshot_id": verified.snapshot_id,
        "corpus_manifest_id": verified.corpus_manifest_id,
        "probe_report_id": verified.probe_report_id,
        "postbuild_readiness_id": verified.postbuild_readiness_id,
        "code_revision": code_revision,
        "model_name": BGE_M3_MODEL,
        "model_revision": BGE_M3_REVISION,
        "tokenizer_revision": TOKENIZER_REVISION,
        "dimension": BGE_M3_DIMENSION,
        "normalization_mode": "l2",
        "dtype": "float32",
        "mock": mock,
        "shard_size": shard_size,
    }


def _validate_resume_checkpoint(
    checkpoint: dict[str, Any],
    verified: Any,
    *,
    mock: bool,
    shard_size: int,
    code_revision: str,
) -> None:
    if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("resume checkpoint schema mismatch")
    expected = _checkpoint_identity(
        verified, mock=mock, shard_size=shard_size, code_revision=code_revision,
    )
    for key, expected_value in expected.items():
        if checkpoint.get(key) != expected_value:
            raise ValueError(f"resume checkpoint {key} mismatch")


def build_embedding_artifacts(
    bundle_path: Path,
    output_dir: Path,
    *,
    embed_batch: Callable,
    batch_size: int = 32,
    shard_size: int = 1024,
    resume: bool = False,
    mock: bool = False,
    code_revision: str,
) -> dict:
    """Build vectors from a verified bundle without loading all source text."""
    bundle = Path(bundle_path)
    if mock and "source_bundles" in bundle.parts:
        raise ValueError("mock embedding is restricted to fixture-scale directories")
    verified = verify_source_bundle(bundle)
    output = Path(output_dir)
    if output.exists() and not resume and any(output.iterdir()):
        raise ValueError("non-resume embedding output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    shards = output / "shards"
    shards.mkdir(exist_ok=True)
    checkpoint_path = output / "checkpoint.json"
    completed = 0
    shard_checksums: dict[str, str] = {}
    if resume and not checkpoint_path.exists():
        # Fail closed: a resume must be backed by a checkpoint. Without one,
        # only an empty output directory (no artifacts) is treated as fresh.
        leftover = [
            path for path in output.iterdir()
            if path.name not in ("shards", "gpu_run_report.json")
        ] + (list(shards.iterdir()) if shards.exists() else [])
        if leftover:
            raise ValueError("resume requires an existing checkpoint.json")
    if resume and checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text())
        _validate_resume_checkpoint(
            checkpoint, verified, mock=mock, shard_size=shard_size,
            code_revision=code_revision,
        )
        completed = int(checkpoint.get("completed_rows", 0))
        if completed < 0 or completed > verified.chunk_count:
            raise ValueError("resume completed_rows out of range")
        expected_shards = (completed + shard_size - 1) // shard_size
        present = sorted(shards.glob("shard_*.npz"))
        if len(present) != expected_shards:
            raise ValueError("resume shard set is incomplete")
        checkpoint_checksums = checkpoint.get("shard_checksums")
        if not isinstance(checkpoint_checksums, dict):
            raise ValueError("resume shard checksums missing")
        expected_ids: list[str] = []
        for shard_index_expected, shard_path in enumerate(present):
            if shard_path.name != f"shard_{shard_index_expected:06d}.npz":
                raise ValueError("resume shard numbering mismatch")
            if checkpoint_checksums.get(shard_path.name) != _sha256_file(shard_path):
                raise ValueError(f"resume shard checksum mismatch: {shard_path.name}")
            try:
                with np.load(shard_path, allow_pickle=False) as shard:
                    shard_ids = [str(value) for value in shard["chunk_ids"].tolist()]
                    shard_vectors = np.asarray(shard["vectors"])
            except Exception as exc:
                raise ValueError(f"resume shard unreadable: {shard_path.name}") from exc
            if shard_vectors.dtype != np.float32 or shard_vectors.ndim != 2 or shard_vectors.shape[1] != BGE_M3_DIMENSION:
                raise ValueError(f"resume shard vector contract mismatch: {shard_path.name}")
            expected_ids.extend(shard_ids)
        prefix_ids = [
            chunk_id
            for index, (chunk_id, _text) in enumerate(_iter_chunk_texts(bundle))
            if index < completed
        ]
        if len(expected_ids) != completed or expected_ids != prefix_ids:
            raise ValueError("resume shard row count mismatch")
        if checkpoint.get("chunk_order_checksum") != _chunk_order_checksum(expected_ids):
            raise ValueError("resume shard order checksum mismatch")
        shard_checksums = dict(checkpoint_checksums)

    all_chunk_ids: list[str] = []
    pending_ids: list[str] = []
    pending_texts: list[str] = []
    shard_ids: list[str] = []
    shard_vectors: list[np.ndarray] = []
    shard_index = completed // shard_size
    row_index = 0
    for chunk_id, text in _iter_chunk_texts(bundle):
        if row_index < completed:
            all_chunk_ids.append(chunk_id)
            row_index += 1
            continue
        pending_ids.append(chunk_id)
        pending_texts.append(text)
        row_index += 1
        if len(pending_ids) >= min(batch_size, shard_size - len(shard_ids)) or row_index == verified.chunk_count:
            vectors = embed_with_oom_backoff(pending_texts, embed_batch, initial_batch_size=batch_size)
            if vectors.shape != (len(pending_ids), BGE_M3_DIMENSION):
                raise ValueError("embedder returned invalid shape")
            shard_ids.extend(pending_ids)
            shard_vectors.append(vectors)
            all_chunk_ids.extend(pending_ids)
            pending_ids, pending_texts = [], []
            if len(shard_ids) >= shard_size or row_index == verified.chunk_count:
                shard_path = shards / f"shard_{shard_index:06d}.npz"
                _write_shard(shard_path, shard_ids, np.vstack(shard_vectors))
                completed = row_index
                shard_checksums[shard_path.name] = _sha256_file(shard_path)
                _atomic_json(checkpoint_path, {
                    **_checkpoint_identity(
                        verified, mock=mock, shard_size=shard_size,
                        code_revision=code_revision,
                    ),
                    "completed_rows": completed,
                    "chunk_order_checksum": _chunk_order_checksum(all_chunk_ids),
                    "shard_checksums": shard_checksums,
                })
                shard_index += 1
                shard_ids, shard_vectors = [], []

    if len(all_chunk_ids) != verified.chunk_count:
        raise ValueError("driver output count mismatch")
    vectors_path_tmp = output / "vectors.npy.tmp.npy"
    vectors_path = output / "vectors.npy"
    matrix = np.lib.format.open_memmap(vectors_path_tmp, mode="w+", dtype="<f4", shape=(verified.chunk_count, BGE_M3_DIMENSION))
    offset = 0
    for shard_path in sorted(shards.glob("shard_*.npz")):
        with np.load(shard_path, allow_pickle=False) as shard:
            shard_vectors = np.asarray(shard["vectors"], dtype=np.float32)
            matrix[offset:offset + len(shard_vectors)] = shard_vectors
            offset += len(shard_vectors)
    matrix.flush()
    del matrix
    os.replace(vectors_path_tmp, vectors_path)
    chunk_ids_path = output / "chunk_ids.json"
    chunk_ids_text = json.dumps(all_chunk_ids, separators=(",", ":")) + "\n"
    _atomic_text(chunk_ids_path, chunk_ids_text)
    order_checksum = _chunk_order_checksum(all_chunk_ids)
    vector_checksum = _sha256_file(vectors_path)
    chunk_ids_checksum = _sha256_file(chunk_ids_path)
    # This is a staging identity only. The importer replaces it with the
    # content hash of the persisted LanceDB rows before serving dense search.
    lancedb_table_checksum = _lancedb_table_identity_hash(
        corpus_manifest_id=verified.corpus_manifest_id,
        source_bundle_id=verified.source_bundle_id,
        chunk_order_checksum=order_checksum,
        vectors_checksum=vector_checksum,
    )
    manifest = IndexManifest(
        model_name=BGE_M3_MODEL,
        model_revision=BGE_M3_REVISION,
        tokenizer_revision=TOKENIZER_REVISION,
        normalization_mode="l2",
        dtype="float32",
        dimension=BGE_M3_DIMENSION,
        corpus_manifest_id=verified.corpus_manifest_id,
        source_bundle_id=verified.source_bundle_id,
        snapshot_id=verified.snapshot_id,
        probe_report_id=verified.probe_report_id,
        postbuild_readiness_id=verified.postbuild_readiness_id,
        vector_count=verified.chunk_count,
        chunk_order_checksum=order_checksum,
        vectors_checksum=vector_checksum,
        artifact_state="vectors_staged",
        code_revision=code_revision,
        artifact_hashes={
            "vectors.npy": vector_checksum,
            "chunk_ids.json": chunk_ids_checksum,
            "lancedb_table": lancedb_table_checksum,
        },
    )
    raw_manifest = manifest.to_dict()
    if mock:
        raw_manifest["non_production"] = True
    _atomic_json(output / "index_manifest.json", raw_manifest)
    checksums = {
        name: _sha256_file(output / name)
        for name in ("vectors.npy", "chunk_ids.json", "index_manifest.json")
    }
    _atomic_text(
        output / "checksums.sha256",
        "\n".join(f"{digest}  {name}" for name, digest in sorted(checksums.items())) + "\n",
    )
    return raw_manifest


def _validate_preflight_vectors(vectors: object) -> np.ndarray:
    """Validate batch-one CUDA preflight dense output.

    FlagEmbedding with ``use_fp16=True`` commonly returns float16 dense vectors
    that are not yet L2-normalized. Production storage remains float32 + L2 via
    ``embed_with_oom_backoff``; preflight only proves encode works on CUDA with
    the correct shape and non-degenerate finite values.
    """
    matrix = np.asarray(vectors)
    if matrix.ndim != 2 or matrix.shape != (1, BGE_M3_DIMENSION):
        raise RuntimeError("CUDA preflight output must have shape (1, 1024)")
    if not np.issubdtype(matrix.dtype, np.floating):
        raise RuntimeError("CUDA preflight output must be floating-point")
    # Cast fp16/bf16/fp64 model outputs to the storage contract dtype.
    matrix = np.asarray(matrix, dtype=np.float32)
    if not np.isfinite(matrix).all():
        raise RuntimeError("CUDA preflight output must be finite")
    norms = np.linalg.norm(matrix, axis=1)
    if np.any(norms == 0):
        raise RuntimeError("CUDA preflight output must be non-zero")
    # Normalize for the returned smoke matrix; the live embed path also
    # L2-normalizes every production batch.
    return (matrix / norms[:, None]).astype(np.float32, copy=False)


def _load_real_cuda_embedder(
    *,
    model_revision: str = BGE_M3_REVISION,
    batch_size: int = 1,
    torch_module=None,
    snapshot_download_fn=None,
    model_class=None,
):
    """Load the pinned model only after a real CUDA batch-one preflight."""
    if model_revision != BGE_M3_REVISION:
        raise ValueError("model_revision must equal the pinned BGE-M3 revision")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if torch_module is None:
        import torch as torch_module
    if not torch_module.cuda.is_available():
        raise RuntimeError("CUDA is required; CPU fallback is disabled")
    if snapshot_download_fn is None:
        from huggingface_hub import snapshot_download
        snapshot_download_fn = snapshot_download
    if model_class is None:
        from FlagEmbedding import BGEM3FlagModel
        model_class = BGEM3FlagModel

    try:
        # Production model resolution is strictly offline: the pinned model
        # must already be present in the local HF cache. Downloading is only
        # authorized in the runbook bootstrap stage, never in the embedding
        # execution path.
        model_path = snapshot_download_fn(
            repo_id=BGE_M3_MODEL, revision=model_revision, local_files_only=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "BGE-M3 is not present in the offline HF cache; model download is "
            "only authorized in the runbook bootstrap stage"
        ) from exc
    model = model_class(model_path, use_fp16=True, devices="cuda")
    try:
        preflight = model.encode(
            ["catalyst B6-L CUDA preflight"], batch_size=1, max_length=8192,
        )
        _validate_preflight_vectors(preflight["dense_vecs"])
    except RuntimeError as exc:
        if "out of memory" in str(exc).lower():
            raise RuntimeError("CUDA OOM at batch=1 during preflight") from exc
        raise

    def embed(texts):
        encoded = model.encode(list(texts), batch_size=min(batch_size, len(texts)), max_length=8192)
        return np.asarray(encoded["dense_vecs"], dtype=np.float32)

    return embed


def load_real_cuda_embedder(*, model_revision: str = BGE_M3_REVISION, batch_size: int = 1):
    """Load the pinned model for an explicitly authorized GPU execution only."""
    return _load_real_cuda_embedder(model_revision=model_revision, batch_size=batch_size)


__all__ = ["build_embedding_artifacts", "load_real_cuda_embedder"]
