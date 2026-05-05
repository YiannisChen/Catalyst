from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

from catalyst_data.storage.sqlite import init_db, upsert_clean_asset, upsert_raw_asset
from scripts import build_embeddings_gpu


def _make_test_db(path: Path) -> Path:
    def insert_asset(
        conn: sqlite3.Connection,
        *,
        asset_id: str,
        ticker: str,
        reference_date: str,
        content_md: str,
        is_duplicate: int = 0,
    ) -> None:
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
            is_duplicate=is_duplicate,
        )

    conn = sqlite3.connect(path)
    init_db(conn)
    insert_asset(
        conn,
        asset_id="asset-c",
        ticker="NVDA",
        reference_date="2026-01-03",
        content_md="Gamma row",
    )
    insert_asset(
        conn,
        asset_id="asset-a",
        ticker="AAPL",
        reference_date="2026-01-01",
        content_md="Alpha row",
    )
    insert_asset(
        conn,
        asset_id="asset-b",
        ticker="MSFT",
        reference_date="2026-01-02",
        content_md="Beta row",
        is_duplicate=1,
    )
    insert_asset(
        conn,
        asset_id="asset-d",
        ticker="META",
        reference_date="2026-01-04",
        content_md="",
    )
    conn.close()
    return path


def test_fetch_embedding_rows_filters_and_orders_deterministically(tmp_path):
    db_path = _make_test_db(tmp_path / "sample.db")

    rows = build_embeddings_gpu.fetch_embedding_rows(db_path)

    assert [row.asset_id for row in rows] == ["asset-a", "asset-c"]
    assert [row.ticker for row in rows] == ["AAPL", "NVDA"]
    assert [row.source_type for row in rows] == ["polygon_news", "polygon_news"]


def test_build_embedding_artifacts_writes_manifest_and_index(tmp_path):
    db_path = _make_test_db(tmp_path / "sample.db")
    output_dir = tmp_path / "embeddings"

    result = build_embeddings_gpu.build_embedding_artifacts(
        db_path=db_path,
        output_dir=output_dir,
        model_name="BAAI/bge-m3",
        model_revision="mock-revision",
        embedding_dim=8,
        batch_size=2,
        use_mock_embeddings=True,
    )

    matrix = np.load(result.numpy_path)
    assert matrix.shape == (2, 8)
    assert matrix.dtype == np.float32

    index_payload = json.loads(result.index_path.read_text())
    assert [row["asset_id"] for row in index_payload["rows"]] == ["asset-a", "asset-c"]
    assert [row["row_index"] for row in index_payload["rows"]] == [0, 1]

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["db_path"] == str(db_path)
    assert manifest["embedding_model_revision"] == "mock-revision"
    assert manifest["embedding_model_name"] == "BAAI/bge-m3"
    assert manifest["embedding_object_scope"] == "l1_clean_assets_rows"
    assert manifest["embedding_dim"] == 8
    assert manifest["dtype"] == "float32"
    assert manifest["n_vectors"] == 2
    assert manifest["numpy_sha256"] == build_embeddings_gpu.sha256_file(result.numpy_path)
    assert manifest["index_sha256"] == build_embeddings_gpu.sha256_file(result.index_path)
    assert manifest["row_order"] == ["asset_id"]
    assert manifest["query_sql"] == build_embeddings_gpu.EMBEDDING_ROWS_SQL


def test_main_mock_embed_generates_expected_artifacts(tmp_path):
    db_path = _make_test_db(tmp_path / "sample.db")
    output_dir = tmp_path / "embeddings"

    exit_code = build_embeddings_gpu.main(
        [
            "--db",
            str(db_path),
            "--output-dir",
            str(output_dir),
            "--mock-embed",
            "--model-revision",
            "mock-cli-revision",
            "--embedding-dim",
            "16",
            "--batch-size",
            "2",
        ]
    )

    assert exit_code == 0
    assert (output_dir / "bge_m3_eval_frozen_v2.npy").exists()
    assert (output_dir / "asset_id_index.json").exists()
    assert (output_dir / "manifest.json").exists()


def test_main_dry_run_reports_deterministic_contract(tmp_path, capsys):
    db_path = _make_test_db(tmp_path / "sample.db")

    exit_code = build_embeddings_gpu.main(["--db", str(db_path), "--dry-run"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["db_path"] == str(db_path.resolve())
    assert payload["n_vectors"] == 2
    assert payload["row_order"] == ["asset_id"]
    assert payload["query_sql"] == build_embeddings_gpu.EMBEDDING_ROWS_SQL
    assert payload["sample_asset_ids"] == ["asset-a", "asset-c"]
