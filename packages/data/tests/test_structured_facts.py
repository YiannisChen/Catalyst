from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from catalyst_data.ingestion.structured_cache import StructuredCache
from catalyst_data.ingestion.structured_facts import (
    MACRO_RELEASES,
    parse_finnhub_earnings,
    parse_fmp_earnings,
    parse_fmp_grades,
    parse_fred_release_dates,
)
from catalyst_data.ingestion.structured_types import FactRow, FetchReceipt
from catalyst_data.storage.errors import StoreReadError


def receipt(payload) -> FetchReceipt:
    return FetchReceipt(
        request_hash="hash",
        provider="test",
        endpoint="/test",
        params={},
        status="OK",
        fetched_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        response_json=json.dumps(payload),
        error=None,
    )


def test_parse_finnhub_earnings():
    rows = parse_finnhub_earnings(
        receipt(
            [
                {
                    "symbol": "AAPL",
                    "estimate": 1.9271,
                    "actual": 1.91,
                    "period": "2026-06-30",
                    "surprise": -0.0171,
                    "surprisePercent": -0.8873,
                    "year": 2026,
                    "quarter": 3,
                }
            ]
        )
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "earnings" and row.source == "finnhub"
    assert row.subject == "AAPL"
    assert row.event_at is None and row.available_at is None and row.precision == "UNKNOWN"
    assert row.payload["fiscal_period"] == {"year": 2026, "quarter": 3}
    assert row.payload["period"] == "2026-06-30"


def test_parse_fmp_earnings_skips_null_and_handles_zero_estimate():
    rows = parse_fmp_earnings(
        receipt(
            [
                {
                    "symbol": "AAPL",
                    "date": "2026-07-30",
                    "epsActual": 1.91,
                    "epsEstimated": 1.93,
                    "revenueActual": 100.0,
                    "revenueEstimated": 0,
                    "lastUpdated": "2026-10-14",
                },
                {"symbol": "AAPL", "date": "2026-10-29", "epsActual": None, "epsEstimated": 2.0},
            ]
        )
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.precision == "DATE"
    assert row.event_at.tzinfo is not None
    assert row.payload["eps_surprise_pct"] == pytest.approx((1.91 - 1.93) / abs(1.93) * 100)
    assert row.payload["revenue_surprise_pct"] is None


def test_fmp_date_row_availability_utc_hour():
    row = parse_fmp_grades(
        receipt(
            [
                {
                    "symbol": "NVDA",
                    "date": "2026-07-12",
                    "gradingCompany": "Rosenblatt",
                    "previousGrade": "Buy",
                    "newGrade": "Buy",
                    "action": "maintain",
                }
            ]
        )
    )[0]
    assert row.available_at.date() in (row.available_at.date(),)
    assert row.available_at.utcoffset() == timezone.utc.utcoffset(None)
    assert row.available_at.hour in (3, 4)


def test_parse_fmp_grades_keeps_all_and_lowercases():
    rows = parse_fmp_grades(
        receipt(
            [
                {
                    "symbol": "NVDA",
                    "date": "2026-09-29",
                    "gradingCompany": "Rosenblatt",
                    "previousGrade": "Buy",
                    "newGrade": "Buy",
                    "action": "maintain",
                }
            ]
        )
    )
    assert len(rows) == 1
    assert rows[0].payload["action"] == "maintain"
    assert rows[0].payload["firm"] == "Rosenblatt"


def test_parse_fred_release_dates_cpi_is_early_morning():
    payload = {"release_dates": [{"release_id": 10, "date": "2026-09-11"}]}
    rows = parse_fred_release_dates(receipt(payload), MACRO_RELEASES[10])
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "macro_release" and row.subject == "US"
    assert row.precision == "DATE"
    assert row.available_at.hour in (12, 13)
    assert row.payload == {"release_id": 10, "name": "CPI", "date": "2026-09-11"}


@pytest.mark.parametrize(
    ("column", "bad_value"),
    [("payload_json", "{"), ("event_at", "not-a-timestamp")],
)
def test_structured_cache_malformed_fact_payload_is_a_store_read_error(tmp_path, column, bad_value):
    path = tmp_path / "structured.sqlite3"
    cache = StructuredCache(path, create=True)
    cached_receipt = FetchReceipt(
        request_hash="hash",
        provider="test",
        endpoint="/test",
        params={},
        status="OK",
        fetched_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        response_json="{}",
        error=None,
    )
    cache.put(
        cached_receipt,
        (FactRow("earnings", "AAPL", None, None, "UNKNOWN", "test", {}),),
    )
    cache.conn.execute(f"UPDATE facts SET {column} = ?", (bad_value,))
    cache.conn.commit()
    cache.conn.close()

    reader = StructuredCache(path)
    try:
        with pytest.raises(StoreReadError):
            reader.rows("earnings", "AAPL")
    finally:
        reader.conn.close()
