"""Build reproducible embedding artifacts for frozen-eval L1 clean_assets rows.

This script is the P1-T01 scaffold for the cloud GPU batch defined by ADR-008.
It reads deterministic L1 rows from `data/catalyst_eval_frozen_v2.db` and writes:

- `data/embeddings/bge_m3_eval_frozen_v2.npy`
- `data/embeddings/asset_id_index.json`
- `data/embeddings/manifest.json`

Per the resolved OQ#3 decision, the canonical T01 embedding object is:

- every qualifying row in `clean_assets`
- one 1024-dim vector per L1 row
- no L2 sentence vectors in this batch

Two execution modes are supported:

1. Real embedding mode (for HUMAN cloud GPU runs):
   - pins a specific Hugging Face revision via `--model-revision`
   - downloads that exact snapshot
   - embeds every qualifying row with `BAAI/bge-m3`

2. Mock embedding mode (for local scaffold validation):
   - no model download
   - generates deterministic float32 vectors from row content hashes
   - exercises the artifact contract and validation path cheaply

Examples:
    python packages/data-core/scripts/build_embeddings_gpu.py --dry-run
    python packages/data-core/scripts/build_embeddings_gpu.py --mock-embed --model-revision mock-v1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "catalyst_eval_frozen_v2.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "embeddings"
DEFAULT_MODEL_NAME = "BAAI/bge-m3"
DEFAULT_EMBEDDING_DIM = 1024
DEFAULT_BATCH_SIZE = 32
DEFAULT_NUMPY_NAME = "bge_m3_eval_frozen_v2.npy"
DEFAULT_INDEX_NAME = "asset_id_index.json"
DEFAULT_MANIFEST_NAME = "manifest.json"
DEFAULT_MOCK_REVISION = "mock-deterministic-v1"
EMBEDDING_OBJECT_SCOPE = "l1_clean_assets_rows"
SOURCE_TABLE = "clean_assets"

EMBEDDING_ROWS_SQL = """
SELECT asset_id, ticker, source_type, reference_date, content_md
FROM clean_assets
WHERE is_duplicate = 0 AND LENGTH(content_md) > 0
ORDER BY asset_id ASC
""".strip()


@dataclass(frozen=True)
class EmbeddingRow:
    asset_id: str
    ticker: str
    source_type: str
    reference_date: str
    content_md: str


@dataclass(frozen=True)
class BuildArtifacts:
    numpy_path: Path
    index_path: Path
    manifest_path: Path
    n_vectors: int
    embedding_dim: int


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fetch_embedding_rows(db_path: str | Path) -> list[EmbeddingRow]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(EMBEDDING_ROWS_SQL).fetchall()
    finally:
        conn.close()

    return [EmbeddingRow(*row) for row in rows]


def _mock_embedding_vector(seed_text: str, embedding_dim: int) -> np.ndarray:
    seed_bytes = seed_text.encode("utf-8")
    target_bytes = embedding_dim * 4
    buffer = bytearray()
    counter = 0
    while len(buffer) < target_bytes:
        counter_bytes = counter.to_bytes(4, byteorder="big", signed=False)
        buffer.extend(hashlib.sha256(seed_bytes + counter_bytes).digest())
        counter += 1

    raw = np.frombuffer(bytes(buffer[:target_bytes]), dtype="<u4").astype(np.float32)
    return raw / np.float32(np.iinfo(np.uint32).max)


def _mock_embed_rows(rows: Sequence[EmbeddingRow], embedding_dim: int) -> np.ndarray:
    return np.vstack(
        [
            _mock_embedding_vector(
                f"{row.asset_id}\n{row.reference_date}\n{row.content_md}",
                embedding_dim,
            )
            for row in rows
        ]
    ).astype(np.float32, copy=False)


def _load_bge_m3_model(model_name: str, model_revision: str, use_fp16: bool):
    try:
        from FlagEmbedding import BGEM3FlagModel  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "FlagEmbedding is required for real embedding runs. "
            "Install with: pip install 'catalyst-data[vector]'"
        ) from exc

    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "huggingface_hub is required to pin and download an exact model revision."
        ) from exc

    model_path = snapshot_download(repo_id=model_name, revision=model_revision)
    return BGEM3FlagModel(model_path, use_fp16=use_fp16)


def _embed_rows_with_model(
    rows: Sequence[EmbeddingRow],
    *,
    model_name: str,
    model_revision: str,
    batch_size: int,
    use_fp16: bool,
) -> np.ndarray:
    model = _load_bge_m3_model(model_name, model_revision, use_fp16)
    encoded = model.encode(
        [row.content_md for row in rows],
        batch_size=batch_size,
        max_length=8192,
    )
    matrix = np.asarray(encoded["dense_vecs"], dtype=np.float32)
    return matrix


def validate_embedding_matrix(
    matrix: np.ndarray,
    rows: Sequence[EmbeddingRow],
    *,
    embedding_dim: int,
) -> None:
    if matrix.ndim != 2:
        raise ValueError(f"Expected a 2D embedding matrix, got shape={matrix.shape!r}")
    if matrix.shape[0] != len(rows):
        raise ValueError(
            f"Row-count mismatch: matrix has {matrix.shape[0]} rows but query returned {len(rows)}"
        )
    if matrix.shape[1] != embedding_dim:
        raise ValueError(
            f"Dim mismatch: matrix has dim {matrix.shape[1]} but expected {embedding_dim}"
        )
    if matrix.dtype != np.float32:
        raise ValueError(f"Expected float32 embeddings, got dtype={matrix.dtype}")
    if not np.isfinite(matrix).all():
        raise ValueError("Embedding matrix contains NaN or inf values")


def _build_index_payload(rows: Sequence[EmbeddingRow], db_path: Path) -> dict:
    return {
        "db_path": str(db_path.resolve()),
        "source_table": SOURCE_TABLE,
        "query_sql": EMBEDDING_ROWS_SQL,
        "row_order": ["asset_id"],
        "rows": [
            {
                "row_index": index,
                "asset_id": row.asset_id,
                "ticker": row.ticker,
                "source_type": row.source_type,
                "reference_date": row.reference_date,
            }
            for index, row in enumerate(rows)
        ],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_embedding_artifacts(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    model_name: str = DEFAULT_MODEL_NAME,
    model_revision: str,
    embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    batch_size: int = DEFAULT_BATCH_SIZE,
    use_mock_embeddings: bool = False,
    use_fp16: bool = True,
    operator: str | None = None,
) -> BuildArtifacts:
    db_path = Path(db_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = fetch_embedding_rows(db_path)
    if not rows:
        raise ValueError(
            f"No qualifying clean_assets rows found in {db_path}. "
            "Expected non-duplicate rows with non-empty content_md."
        )

    if use_mock_embeddings:
        matrix = _mock_embed_rows(rows, embedding_dim=embedding_dim)
        generator_mode = "mock"
    else:
        matrix = _embed_rows_with_model(
            rows,
            model_name=model_name,
            model_revision=model_revision,
            batch_size=batch_size,
            use_fp16=use_fp16,
        )
        generator_mode = "flagembedding_bgem3"

    matrix = np.asarray(matrix, dtype=np.float32)
    validate_embedding_matrix(matrix, rows, embedding_dim=embedding_dim)

    numpy_path = output_dir / DEFAULT_NUMPY_NAME
    index_path = output_dir / DEFAULT_INDEX_NAME
    manifest_path = output_dir / DEFAULT_MANIFEST_NAME

    np.save(numpy_path, matrix, allow_pickle=False)
    _write_json(index_path, _build_index_payload(rows, db_path))

    manifest = {
        "schema_version": 1,
        "db_path": str(db_path),
        "db_sha256": sha256_file(db_path),
        "source_table": SOURCE_TABLE,
        "query_sql": EMBEDDING_ROWS_SQL,
        "row_order": ["asset_id"],
        "embedding_object_scope": EMBEDDING_OBJECT_SCOPE,
        "embedding_model_name": model_name,
        "embedding_model_revision": model_revision,
        "embedding_dim": embedding_dim,
        "dtype": str(matrix.dtype),
        "n_vectors": int(matrix.shape[0]),
        "batch_size": batch_size,
        "generator_mode": generator_mode,
        "index_columns": ["row_index", "asset_id", "ticker", "source_type", "reference_date"],
        "numpy_sha256": sha256_file(numpy_path),
        "index_sha256": sha256_file(index_path),
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "operator": operator,
        "script_path": str(Path(__file__).resolve()),
    }
    _write_json(manifest_path, manifest)

    return BuildArtifacts(
        numpy_path=numpy_path,
        index_path=index_path,
        manifest_path=manifest_path,
        n_vectors=int(matrix.shape[0]),
        embedding_dim=int(matrix.shape[1]),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build reproducible bge-m3 embedding artifacts for Catalyst frozen eval.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        metavar="PATH",
        help=f"SQLite DB to read (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        metavar="DIR",
        help=f"Artifact directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL_NAME,
        metavar="MODEL_ID",
        help=f"Hugging Face model id (default: {DEFAULT_MODEL_NAME}).",
    )
    parser.add_argument(
        "--model-revision",
        default=None,
        metavar="REVISION",
        help="Exact model revision pin (HF commit hash / tag). Required unless --dry-run is set.",
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=DEFAULT_EMBEDDING_DIM,
        metavar="INT",
        help=f"Expected embedding dimension (default: {DEFAULT_EMBEDDING_DIM}).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        metavar="INT",
        help=f"Encoding batch size (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--operator",
        default=None,
        metavar="NAME",
        help="Optional human/operator handle recorded in manifest.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect deterministic row selection and exit without generating artifacts.",
    )
    parser.add_argument(
        "--mock-embed",
        action="store_true",
        help="Generate deterministic mock float32 vectors instead of downloading/running bge-m3.",
    )
    parser.add_argument(
        "--no-fp16",
        action="store_true",
        help="Disable fp16 when loading BGEM3FlagModel in real embedding mode.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    rows = fetch_embedding_rows(db_path)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "db_path": str(db_path),
                    "db_sha256": sha256_file(db_path),
                    "source_table": SOURCE_TABLE,
                    "query_sql": EMBEDDING_ROWS_SQL,
                    "row_order": ["asset_id"],
                    "embedding_object_scope": EMBEDDING_OBJECT_SCOPE,
                    "n_vectors": len(rows),
                    "sample_asset_ids": [row.asset_id for row in rows[:3]],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    model_revision = args.model_revision
    if args.mock_embed and model_revision is None:
        model_revision = DEFAULT_MOCK_REVISION
    if model_revision is None:
        parser.error("--model-revision is required for artifact builds")

    try:
        result = build_embedding_artifacts(
            db_path=db_path,
            output_dir=args.output_dir,
            model_name=args.model,
            model_revision=model_revision,
            embedding_dim=args.embedding_dim,
            batch_size=args.batch_size,
            use_mock_embeddings=args.mock_embed,
            use_fp16=not args.no_fp16,
            operator=args.operator,
        )
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    summary = {
        "numpy_path": str(result.numpy_path),
        "index_path": str(result.index_path),
        "manifest_path": str(result.manifest_path),
        "n_vectors": result.n_vectors,
        "embedding_dim": result.embedding_dim,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
