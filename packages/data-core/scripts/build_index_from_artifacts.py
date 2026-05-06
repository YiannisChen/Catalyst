"""Build LanceDB Gold index from precomputed T01 embedding artifacts.

Consumes frozen L1 artifacts:
- data/embeddings/bge_m3_eval_frozen_v2.npy
- data/embeddings/asset_id_index.json
- data/embeddings/manifest.json
- data/catalyst_eval_frozen_v2.db

Writes LanceDB store:
- data/lancedb_gold/eval_frozen/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from catalyst_data.storage.lancedb_store import _TABLE_NAME

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "catalyst_eval_frozen_v2.db"
DEFAULT_NUMPY_PATH = REPO_ROOT / "data" / "embeddings" / "bge_m3_eval_frozen_v2.npy"
DEFAULT_INDEX_PATH = REPO_ROOT / "data" / "embeddings" / "asset_id_index.json"
DEFAULT_MANIFEST_PATH = REPO_ROOT / "data" / "embeddings" / "manifest.json"
DEFAULT_LANCEDB_DIR = REPO_ROOT / "data" / "lancedb_gold" / "eval_frozen"
EXPECTED_SCOPE = "l1_clean_assets_rows"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_directory(path: str | Path) -> str:
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"Directory does not exist: {root}")

    files = sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(root).as_posix())
    digest = hashlib.sha256()
    for file_path in files:
        rel = file_path.relative_to(root).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(file_path)))
        digest.update(b"\0")
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_manifest(
    manifest: dict[str, Any],
    *,
    db_path: Path,
    numpy_path: Path,
    index_path: Path,
    matrix: np.ndarray,
    index_rows: list[dict[str, Any]],
) -> None:
    if manifest.get("embedding_object_scope") != EXPECTED_SCOPE:
        raise ValueError(
            f"Unexpected embedding_object_scope={manifest.get('embedding_object_scope')!r}; "
            f"expected {EXPECTED_SCOPE!r}"
        )
    if manifest.get("dtype") != "float32":
        raise ValueError(f"Unexpected dtype={manifest.get('dtype')!r}; expected 'float32'")
    if matrix.dtype != np.float32:
        raise ValueError(f"Embedding matrix must be float32, got {matrix.dtype}")
    if manifest.get("n_vectors") != int(matrix.shape[0]):
        raise ValueError(
            f"Manifest n_vectors={manifest.get('n_vectors')} does not match matrix rows={matrix.shape[0]}"
        )
    if manifest.get("embedding_dim") != int(matrix.shape[1]):
        raise ValueError(
            f"Manifest embedding_dim={manifest.get('embedding_dim')} does not match matrix dim={matrix.shape[1]}"
        )
    if len(index_rows) != int(matrix.shape[0]):
        raise ValueError(f"Index rows={len(index_rows)} does not match matrix rows={matrix.shape[0]}")
    if manifest.get("db_sha256") != sha256_file(db_path):
        raise ValueError("db_sha256 mismatch between manifest and db file")
    if manifest.get("numpy_sha256") != sha256_file(numpy_path):
        raise ValueError("numpy_sha256 mismatch between manifest and numpy artifact")
    if manifest.get("index_sha256") != sha256_file(index_path):
        raise ValueError("index_sha256 mismatch between manifest and index artifact")


def _fetch_ordered_l1_rows(db_path: Path, query_sql: str) -> list[tuple[str, str, str, str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(query_sql).fetchall()
    finally:
        conn.close()
    return [(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4])) for r in rows]


def build_records_from_artifacts(
    *,
    db_path: str | Path,
    numpy_path: str | Path,
    index_path: str | Path,
    manifest_path: str | Path,
) -> list[dict[str, Any]]:
    db_path = Path(db_path).resolve()
    numpy_path = Path(numpy_path).resolve()
    index_path = Path(index_path).resolve()
    manifest_path = Path(manifest_path).resolve()

    matrix = np.load(numpy_path, allow_pickle=False)
    if matrix.ndim != 2:
        raise ValueError(f"Expected 2D embedding matrix, got shape={matrix.shape!r}")
    if not np.isfinite(matrix).all():
        raise ValueError("Embedding matrix contains NaN/inf values")

    index_payload = _load_json(index_path)
    manifest = _load_json(manifest_path)
    index_rows = list(index_payload.get("rows", []))

    _validate_manifest(
        manifest,
        db_path=db_path,
        numpy_path=numpy_path,
        index_path=index_path,
        matrix=matrix,
        index_rows=index_rows,
    )

    by_row_index: dict[int, dict[str, Any]] = {}
    for row in index_rows:
        row_index = row.get("row_index")
        if not isinstance(row_index, int):
            raise ValueError("Every index row must include integer row_index")
        if row_index in by_row_index:
            raise ValueError(f"Duplicate row_index in index artifact: {row_index}")
        by_row_index[row_index] = row

    expected_indices = set(range(matrix.shape[0]))
    if set(by_row_index) != expected_indices:
        raise ValueError("row_index values must be a contiguous 0..n-1 sequence")

    ordered_index_rows = [by_row_index[i] for i in range(matrix.shape[0])]

    query_sql = str(manifest.get("query_sql") or index_payload.get("query_sql") or "").strip()
    if not query_sql:
        raise ValueError("query_sql missing from manifest/index artifacts")
    db_rows = _fetch_ordered_l1_rows(db_path, query_sql)
    if len(db_rows) != len(ordered_index_rows):
        raise ValueError(
            f"DB query returned {len(db_rows)} rows, index has {len(ordered_index_rows)} rows"
        )

    db_asset_ids = [row[0] for row in db_rows]
    index_asset_ids = [str(row.get("asset_id")) for row in ordered_index_rows]
    if db_asset_ids != index_asset_ids:
        raise ValueError("asset_id order mismatch between DB query and index artifact")

    records: list[dict[str, Any]] = []
    for row_index, db_row in enumerate(db_rows):
        asset_id, ticker, source_type, reference_date, content_md = db_row
        vector = matrix[row_index].tolist()
        records.append(
            {
                "asset_id": asset_id,
                "ticker": ticker,
                "source_type": source_type,
                "reference_date": reference_date,
                "content_md": content_md,
                "vector": vector,
            }
        )
    return records


def _count_null_vectors(records: Sequence[dict[str, Any]]) -> int:
    null_count = 0
    for record in records:
        vector = record.get("vector")
        if not isinstance(vector, list) or not vector:
            null_count += 1
            continue
        if any(value is None for value in vector):
            null_count += 1
    return null_count


def build_lancedb_index(
    *,
    records: list[dict[str, Any]],
    lancedb_dir: str | Path,
) -> int:
    try:
        import lancedb  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "lancedb is required for artifact index build. Install with: pip install 'catalyst-data[vector]'"
        ) from exc

    target_dir = Path(lancedb_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(target_dir))

    if hasattr(db, "list_tables"):
        listed = db.list_tables()
        table_names = set(listed.tables if hasattr(listed, "tables") else listed)
    else:
        table_names = set(db.table_names())
    if _TABLE_NAME in table_names:
        db.drop_table(_TABLE_NAME)
    table = db.create_table(_TABLE_NAME, data=records)
    table.create_fts_index("content_md", replace=True)
    return int(table.count_rows())


def _smoke_fts_query(lancedb_dir: Path, probe_text: str) -> int:
    import lancedb  # type: ignore

    db = lancedb.connect(str(lancedb_dir))
    table = db.open_table(_TABLE_NAME)
    hits = table.search(probe_text, query_type="fts").limit(3).to_list()
    return len(hits)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build LanceDB Gold index from frozen embedding artifacts (P1-T02)."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help=f"SQLite DB path (default: {DEFAULT_DB_PATH})")
    parser.add_argument(
        "--numpy-path",
        default=str(DEFAULT_NUMPY_PATH),
        help=f"Embedding matrix .npy path (default: {DEFAULT_NUMPY_PATH})",
    )
    parser.add_argument(
        "--index-path",
        default=str(DEFAULT_INDEX_PATH),
        help=f"Asset index JSON path (default: {DEFAULT_INDEX_PATH})",
    )
    parser.add_argument(
        "--manifest-path",
        default=str(DEFAULT_MANIFEST_PATH),
        help=f"Manifest JSON path (default: {DEFAULT_MANIFEST_PATH})",
    )
    parser.add_argument(
        "--lancedb-dir",
        default=str(DEFAULT_LANCEDB_DIR),
        help=f"Output LanceDB directory (default: {DEFAULT_LANCEDB_DIR})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        records = build_records_from_artifacts(
            db_path=args.db,
            numpy_path=args.numpy_path,
            index_path=args.index_path,
            manifest_path=args.manifest_path,
        )
        null_vectors = _count_null_vectors(records)
        if null_vectors != 0:
            raise ValueError(f"Found {null_vectors} null vectors before LanceDB write")

        written_rows = build_lancedb_index(records=records, lancedb_dir=args.lancedb_dir)
        if written_rows != len(records):
            raise ValueError(f"LanceDB row count mismatch: wrote={written_rows}, expected={len(records)}")

        probe_text = " ".join(str(records[0]["content_md"]).split()[:8])
        smoke_hits = _smoke_fts_query(Path(args.lancedb_dir).resolve(), probe_text)
        if smoke_hits <= 0:
            raise ValueError("FTS smoke query returned 0 hits")

        payload = {
            "lancedb_dir": str(Path(args.lancedb_dir).resolve()),
            "table_name": _TABLE_NAME,
            "row_count": written_rows,
            "null_vector_count": null_vectors,
            "smoke_hits": smoke_hits,
            "lancedb_dir_sha256": sha256_directory(args.lancedb_dir),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
