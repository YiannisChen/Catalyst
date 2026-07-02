"""Tests for FRED normalization + point-in-time — mock-only, zero network."""
from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from catalyst_data.pipeline.fred_normalize import (
    normalize_fred_observations, derive_t10y2y, rederive_fred_macro,
    _parse_value,
)
from catalyst_data.storage.sqlite import (
    init_db, ensure_macro_tables, compute_asset_id, upsert_raw_asset,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseValue:
    def test_numeric_value(self):
        assert _parse_value("4.83") == 4.83

    def test_null_for_dot(self):
        assert _parse_value(".") is None

    def test_null_for_none(self):
        assert _parse_value(None) is None

    def test_negative_value(self):
        assert _parse_value("-0.25") == -0.25


class TestNormalizeFredObservations:
    def test_observations_parsed_from_fixture(self):
        """Real output_type=4 fixture → correct row count and fields."""
        with open(FIXTURES / "fred_observations_DFF_output_type4.json") as f:
            data = json.load(f)

        rows = normalize_fred_observations(data, "raw_test_asset")
        # 20 observations, 1 "." → filtered: 19 rows with non-null values
        # Wait — normalization does NOT filter "." — it stores NULL
        # The "." is on 2024-11-11, so all 20 rows are returned
        assert len(rows) == 20

        # Find the "." row
        dot_row = [r for r in rows if r["observation_date"] == "2024-11-11"]
        assert len(dot_row) == 1
        assert dot_row[0]["value"] is None
        assert dot_row[0]["released_at"] == "2024-11-11"

        # Find a non-null row
        row = rows[0]
        assert row["series_id"] == "DFF"
        assert row["observation_date"] == "2024-11-01"
        assert row["value"] == 4.83
        assert row["raw_asset_id"] == "raw_test_asset"

    def test_released_at_from_per_observation_not_top_level(self):
        """PER-OBSERVATION realtime_start is used, NOT the top-level field."""
        with open(FIXTURES / "fred_observations_DFF_output_type4.json") as f:
            data = json.load(f)

        rows = normalize_fred_observations(data, "raw_test")
        # Top-level realtime_start is "2025-08-15" (useless fetch-date proxy)
        top_level = data["realtime_start"]  # "2025-08-15"

        # PER-observation realtime_start for 2024-11-07 is "2024-11-07"
        row_nov7 = [r for r in rows if r["observation_date"] == "2024-11-07"][0]
        assert row_nov7["released_at"] == "2024-11-07"

        # This MUST differ from the top-level field
        assert row_nov7["released_at"] != top_level, (
            "released_at must come from per-observation realtime_start, "
            "NOT from top-level fetch-date proxy"
        )

    def test_empty_observations(self):
        rows = normalize_fred_observations(
            {"observations": [], "id": "DFF"}, "raw_test"
        )
        assert rows == []

    def test_invalid_observations_type(self):
        rows = normalize_fred_observations(
            {"observations": "not a list", "id": "DFF"}, "raw_test"
        )
        assert rows == []


class TestPointInTime:
    def test_historical_as_of_query_returns_released_rows(self):
        """A query with as_of=2024-11-10 RETURNS rows released on/before that date."""
        with open(FIXTURES / "fred_observations_DFF_output_type4.json") as f:
            data = json.load(f)

        rows = normalize_fred_observations(data, "raw_test")

        # as_of = 2024-11-10: should include all rows with released_at <= 2024-11-10
        as_of = "2024-11-10"
        visible = [r for r in rows
                   if r["released_at"] and r["released_at"] <= as_of]
        visible_dates = sorted(set(r["observation_date"] for r in visible))

        # Observations up to 2024-11-08 were released on/before 2024-11-10
        assert "2024-11-01" in visible_dates
        assert "2024-11-07" in visible_dates
        assert "2024-11-08" in visible_dates
        # 2024-11-11 (released 2024-11-11) should NOT be visible
        assert "2024-11-11" not in visible_dates
        # 2024-11-12 (released 2024-11-12) should NOT be visible
        assert "2024-11-12" not in visible_dates

        assert len(visible) > 0, (
            "Historical as_of query must return data — "
            "if released_at were the fetch date, this would be EMPTY"
        )

    def test_excludes_observation_released_after_as_of(self):
        """An observation with released_at > as_of is excluded."""
        with open(FIXTURES / "fred_observations_DFF_output_type4.json") as f:
            data = json.load(f)

        rows = normalize_fred_observations(data, "raw_test")

        # as_of = 2024-11-10: 2024-11-12 (released 2024-11-12) excluded
        as_of = "2024-11-10"
        row_nov12 = [r for r in rows if r["observation_date"] == "2024-11-12"]
        assert len(row_nov12) == 1
        assert row_nov12[0]["released_at"] == "2024-11-12"

        # This would pass the query rule (simulate the WHERE clause)
        passes = row_nov12[0]["released_at"] <= as_of
        assert not passes, (
            "2024-11-12 observation released on 2024-11-12 "
            "must NOT be visible at as_of=2024-11-10"
        )

    def test_would_fail_if_released_at_were_fetch_date(self):
        """SANITY: if released_at were set to top-level (fetch-date proxy),
        the historical as_of query would return NOTHING."""
        with open(FIXTURES / "fred_observations_DFF_output_type4.json") as f:
            data = json.load(f)

        # Simulate the BAD approach: released_at = top-level field
        top_level = data["realtime_start"]  # "2025-08-15"
        rows = normalize_fred_observations(data, "raw_test")

        # Override with top-level (simulating the bug)
        for r in rows:
            r["released_at"] = top_level

        as_of = "2024-11-10"
        # All observations have released_at="2025-08-15" which is > 2024-11-10
        visible = [r for r in rows
                   if r["released_at"] and r["released_at"] <= as_of]
        assert len(visible) == 0, (
            "SANITY CHECK: If released_at were the fetch date (2025-08-15), "
            "ZERO observations would be visible at as_of=2024-11-10. "
            "This proves the output_type=4 approach is necessary."
        )


class TestT10Y2YDerivation:
    def test_t10y2y_derived(self):
        dgs10 = [
            {"observation_date": "2025-07-01", "value": 4.50, "released_at": "2025-07-01"},
            {"observation_date": "2025-07-02", "value": 4.40, "released_at": "2025-07-02"},
            {"observation_date": "2025-07-03", "value": None, "released_at": "2025-07-03"},
        ]
        dgs2 = [
            {"observation_date": "2025-07-01", "value": 4.00, "released_at": "2025-07-01"},
            {"observation_date": "2025-07-02", "value": 3.90, "released_at": "2025-07-02"},
            {"observation_date": "2025-07-03", "value": 3.85, "released_at": "2025-07-03"},
            {"observation_date": "2025-07-04", "value": 3.80, "released_at": "2025-07-04"},
        ]

        t10y2y = derive_t10y2y(dgs10, dgs2, "raw_test")

        # 2025-07-01: 4.50 − 4.00 = 0.5
        row1 = [r for r in t10y2y if r["observation_date"] == "2025-07-01"][0]
        assert row1["value"] == 0.5
        assert row1["series_id"] == "T10Y2Y"

        # 2025-07-02: 4.40 − 3.90 = 0.5
        row2 = [r for r in t10y2y if r["observation_date"] == "2025-07-02"][0]
        assert row2["value"] == 0.5

        # 2025-07-03: DGS10 is None → no T10Y2Y row
        row3 = [r for r in t10y2y if r["observation_date"] == "2025-07-03"]
        assert len(row3) == 0

        # 2025-07-04: DGS2 present but DGS10 missing → no T10Y2Y
        row4 = [r for r in t10y2y if r["observation_date"] == "2025-07-04"]
        assert len(row4) == 0

    def test_t10y2y_released_at_is_max(self):
        dgs10 = [
            {"observation_date": "2025-07-01", "value": 4.50, "released_at": "2025-07-01"},
        ]
        dgs2 = [
            {"observation_date": "2025-07-01", "value": 4.00, "released_at": "2025-07-02"},
        ]
        t10y2y = derive_t10y2y(dgs10, dgs2, "raw_test")
        assert t10y2y[0]["released_at"] == "2025-07-02"  # max of the two


class TestBronzeRederivability:
    def test_bronze_roundtrip(self, tmp_path):
        """Store FRED raw_asset → decompress → re-normalize → observations match."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_macro_tables(conn)

        with open(FIXTURES / "fred_observations_DFF_output_type4.json") as f:
            data = json.load(f)

        raw_bytes = json.dumps(data, ensure_ascii=True).encode("utf-8")
        asset_id = compute_asset_id("DFF", "2024-11-30", "fred_macro")

        upsert_raw_asset(
            conn,
            asset_id=asset_id,
            ticker="DFF",
            source_type="fred_macro",
            reference_date="2024-11-30",
            content_raw=raw_bytes,
            http_status=200,
            metadata={"series_id": "DFF", "observation_count": 20},
        )
        conn.close()

        # Re-derive
        counts = rederive_fred_macro(db_path)
        assert counts["observations_upserted"] == 20  # 19 non-null + 1 NULL

        # Verify observations in DB
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT observation_date, value, released_at FROM macro_observations "
            "WHERE series_id='DFF' ORDER BY observation_date"
        ).fetchall()
        assert len(rows) == 20

        # Check the "." → NULL row
        dot_row = [r for r in rows if r[0] == "2024-11-11"][0]
        assert dot_row[1] is None

        # Check a normal row
        row0 = rows[0]
        assert row0[0] == "2024-11-01"
        assert row0[1] == 4.83
        assert row0[2] == "2024-11-01"  # per-observation realtime_start

        conn.close()


class TestManifest:
    def test_curated_12_series(self):
        from catalyst_data.pipeline.fred_manifest import CURATED_SERIES, FETCHED_SERIES
        assert len(CURATED_SERIES) == 12
        assert len(FETCHED_SERIES) == 11  # T10Y2Y excluded
        assert "TEDRATE" not in {s.series_id for s in CURATED_SERIES}
        assert "T10Y2Y" in {s.series_id for s in CURATED_SERIES}

    def test_t10y2y_is_derived(self):
        from catalyst_data.pipeline.fred_manifest import series_by_id
        t10 = series_by_id("T10Y2Y")
        assert t10 is not None
        assert t10.derived is True

    def test_fetched_does_not_include_derived(self):
        from catalyst_data.pipeline.fred_manifest import FETCHED_SERIES
        assert "T10Y2Y" not in FETCHED_SERIES
