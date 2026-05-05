from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from catalyst_data.storage.sqlite import init_db, upsert_clean_asset, upsert_raw_asset
from scripts.audit_v1_2_coverage import build_coverage_audit


def _insert_asset(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    ticker: str,
    source_type: str,
    reference_date: str,
    content_md: str,
) -> None:
    upsert_raw_asset(
        conn,
        asset_id=asset_id,
        ticker=ticker,
        source_type=source_type,
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
        source_type=source_type,
        reference_date=reference_date,
        content_md=content_md,
    )


def test_build_coverage_audit_reports_direct_and_macro_counts(tmp_path: Path):
    db_path = tmp_path / "coverage.db"
    conn = sqlite3.connect(db_path)
    init_db(conn)
    _insert_asset(
        conn,
        asset_id="direct-1",
        ticker="NVDA",
        source_type="polygon_news",
        reference_date="2025-01-27",
        content_md="Nvidia led a semiconductor selloff.",
    )
    _insert_asset(
        conn,
        asset_id="macro-1",
        ticker="MACRO",
        source_type="fred_macro",
        reference_date="2025-01-26",
        content_md="Fed policy update.",
    )
    conn.close()

    golden_path = tmp_path / "v1_2.jsonl"
    golden_path.write_text(
        json.dumps(
            {
                "id": "g009",
                "ticker": "NVDA",
                "trade_date": "2025-01-27",
                "price_move_pct": -16.97,
                "causes": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_coverage_audit(db_path=db_path, golden_set_path=golden_path, window_days=3)

    assert report["summary"]["total_cases"] == 1
    assert report["summary"]["cases_with_direct_docs"] == 1
    assert report["summary"]["cases_with_macro_docs"] == 1
    assert report["cases"][0]["direct_doc_count"] == 1
    assert report["cases"][0]["macro_doc_count"] == 1
