from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from catalyst_data.storage.sqlite import init_db, upsert_clean_asset, upsert_raw_asset
from scripts import build_embeddings_gpu, build_index_from_artifacts


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_test_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    init_db(conn)

    for asset_id, ticker, reference_date, content_md in (
        ("asset-b", "MSFT", "2026-01-02", "Beta row"),
        ("asset-a", "AAPL", "2026-01-01", "Alpha row"),
    ):
        upsert_raw_asset(
            conn,
            asset_id=asset_id,
            ticker=ticker,
            source_type="polygon_news",
            reference_date=reference_date,
            data_version="v1",
            content_raw=content_md.encode("utf-8"),
            http_status=200,
            metadata={},
        )
        upsert_clean_asset(
            conn,
            asset_id=asset_id,
            ticker=ticker,
            source_type="polygon_news",
            reference_date=reference_date,
            content_md=content_md,
            is_duplicate=0,
        )
    conn.close()
    return path


def _write_artifacts(base: Path, db_path: Path) -> tuple[Path, Path, Path]:
    matrix = np.asarray([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
    npy_path = base / "bge_m3_eval_frozen_v2.npy"
    np.save(npy_path, matrix, allow_pickle=False)

    index_payload = {
        "db_path": str(db_path.resolve()),
        "source_table": "clean_assets",
        "query_sql": build_embeddings_gpu.EMBEDDING_ROWS_SQL,
        "row_order": ["asset_id"],
        "rows": [
            {
                "row_index": 1,
                "asset_id": "asset-b",
                "ticker": "MSFT",
                "source_type": "polygon_news",
                "reference_date": "2026-01-02",
            },
            {
                "row_index": 0,
                "asset_id": "asset-a",
                "ticker": "AAPL",
                "source_type": "polygon_news",
                "reference_date": "2026-01-01",
            },
        ],
    }
    index_path = base / "asset_id_index.json"
    index_path.write_text(json.dumps(index_payload, indent=2, sort_keys=True) + "\n")

    manifest_payload = {
        "schema_version": 1,
        "db_path": str(db_path.resolve()),
        "db_sha256": _sha256_file(db_path),
        "source_table": "clean_assets",
        "query_sql": build_embeddings_gpu.EMBEDDING_ROWS_SQL,
        "row_order": ["asset_id"],
        "embedding_object_scope": "l1_clean_assets_rows",
        "embedding_model_name": "BAAI/bge-m3",
        "embedding_model_revision": "test-revision",
        "embedding_dim": 2,
        "dtype": "float32",
        "n_vectors": 2,
        "numpy_sha256": _sha256_file(npy_path),
        "index_sha256": _sha256_file(index_path),
    }
    manifest_path = base / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n")

    return npy_path, index_path, manifest_path


def test_build_records_from_artifacts_aligns_row_index_with_asset_ids(tmp_path: Path):
    db_path = _make_test_db(tmp_path / "sample.db")
    npy_path, index_path, manifest_path = _write_artifacts(tmp_path, db_path)

    records = build_index_from_artifacts.build_records_from_artifacts(
        db_path=db_path,
        numpy_path=npy_path,
        index_path=index_path,
        manifest_path=manifest_path,
    )

    assert [record["asset_id"] for record in records] == ["asset-a", "asset-b"]
    assert records[0]["vector"] == pytest.approx([1.0, 2.0])
    assert records[1]["vector"] == pytest.approx([3.0, 4.0])
    assert records[0]["content_md"] == "Alpha row"
    assert records[1]["content_md"] == "Beta row"


def test_build_records_from_artifacts_rejects_index_asset_id_mismatch(tmp_path: Path):
    db_path = _make_test_db(tmp_path / "sample.db")
    npy_path, index_path, manifest_path = _write_artifacts(tmp_path, db_path)

    index_payload = json.loads(index_path.read_text())
    index_payload["rows"][0]["asset_id"] = "asset-missing"
    index_path.write_text(json.dumps(index_payload, indent=2, sort_keys=True) + "\n")
    manifest_payload = json.loads(manifest_path.read_text())
    manifest_payload["index_sha256"] = _sha256_file(index_path)
    manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n")

    with pytest.raises(ValueError, match="asset_id order mismatch"):
        build_index_from_artifacts.build_records_from_artifacts(
            db_path=db_path,
            numpy_path=npy_path,
            index_path=index_path,
            manifest_path=manifest_path,
        )


def test_sha256_directory_is_deterministic(tmp_path: Path):
    root = tmp_path / "hashdir"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")

    first = build_index_from_artifacts.sha256_directory(root)
    second = build_index_from_artifacts.sha256_directory(root)

    assert first == second
