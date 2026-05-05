from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_data.storage.sqlite import init_db, upsert_clean_asset, upsert_raw_asset
from scripts.freeze_frozen_db_v2 import build_provenance_manifest, seed_v2_database


def _insert_asset(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    ticker: str,
    reference_date: str,
    content_md: str,
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
    )


def test_build_provenance_manifest_marks_v2_as_clean_assets_superset(tmp_path: Path):
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"

    source_conn = sqlite3.connect(source_db)
    init_db(source_conn)
    _insert_asset(
        source_conn,
        asset_id="asset-a",
        ticker="NVDA",
        reference_date="2025-05-02",
        content_md="Original asset",
    )
    source_conn.close()

    seed_v2_database(source_db=source_db, target_db=target_db, replace=True)

    target_conn = sqlite3.connect(target_db)
    _insert_asset(
        target_conn,
        asset_id="asset-b",
        ticker="TSLA",
        reference_date="2025-01-02",
        content_md="Backfilled asset",
    )
    target_conn.close()

    manifest = build_provenance_manifest(
        source_db=source_db,
        target_db=target_db,
        start_date="2024-12-30",
        end_date="2025-05-01",
        coverage_artifact_path=None,
    )

    assert manifest["superset_checks"]["clean_assets"]["is_superset"] is True
    assert manifest["table_counts"]["source"]["clean_assets"] == 1
    assert manifest["table_counts"]["target"]["clean_assets"] == 2
