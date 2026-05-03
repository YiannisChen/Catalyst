"""Shared fixture data for retrieval-policy tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_data.storage.sqlite import (
    compute_asset_id,
    init_db,
    upsert_clean_asset,
    upsert_raw_asset,
)


FIXTURE_ROWS = [
    {
        "ticker": "AAPL",
        "source_type": "polygon_news",
        "reference_date": "2026-01-15",
        "content_md": "Apple shares fell after export restrictions tightened.",
    },
    {
        "ticker": "AAPL",
        "source_type": "fmp_fundamentals",
        "reference_date": "2026-01-14",
        "content_md": "Apple guidance was revised lower.",
    },
    {
        "ticker": "AAPL",
        "source_type": "polygon_news",
        "reference_date": "2026-01-30",
        "content_md": "Out-of-window AAPL article that should not be returned.",
    },
    {
        "ticker": "MSFT",
        "source_type": "macro_news",
        "reference_date": "2026-01-15",
        "content_md": "Fed commentary pressured large-cap tech.",
    },
    {
        "ticker": "SPY",
        "source_type": "market_news",
        "reference_date": "2026-01-13",
        "content_md": "Broad market selloff weighed on equities.",
    },
    {
        "ticker": "NVDA",
        "source_type": "geopolitical_news",
        "reference_date": "2026-01-15",
        "content_md": "Tariff escalation hit semiconductor sentiment.",
    },
]


def build_retrieval_fixture(tmp_path: Path) -> Path:
    db_path = tmp_path / "retrieval_fixture.db"
    conn = sqlite3.connect(db_path)
    init_db(conn)

    for row in FIXTURE_ROWS:
        asset_id = compute_asset_id(row["ticker"], row["reference_date"], row["source_type"])
        upsert_raw_asset(
            conn,
            asset_id=asset_id,
            ticker=row["ticker"],
            source_type=row["source_type"],
            reference_date=row["reference_date"],
            content_raw=row["content_md"].encode("utf-8"),
            http_status=200,
            metadata={},
        )
        upsert_clean_asset(
            conn,
            asset_id=asset_id,
            ticker=row["ticker"],
            source_type=row["source_type"],
            reference_date=row["reference_date"],
            content_md=row["content_md"],
        )

    conn.close()
    return db_path
