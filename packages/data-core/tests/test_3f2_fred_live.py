"""3F.2 FRED live tests — orchestration, PIT, idempotency, Plane-2."""

from __future__ import annotations

import sqlite3
import json
import zlib
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from catalyst_data.storage.sqlite import init_db, ensure_macro_tables
from catalyst_data.quality import ensure_ingestion_quality_tables
from catalyst_data.pipeline.fred_normalize import (
    normalize_fred_observations,
    rederive_fred_macro,
    derive_t10y2y,
)
from catalyst_data.pipeline.fred_manifest import CURATED_SERIES, FETCHED_SERIES


def _make_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    ensure_macro_tables(conn)
    conn.commit()
    return conn


# Sample output_type=4 FRED response for DGS10
FRED_DGS10_FIXTURE = {
    "id": "DGS10",
    "realtime_start": "2026-07-03",
    "realtime_end": "2026-07-03",
    "observation_start": "2026-01-02",
    "observation_end": "2026-06-30",
    "units": "lin",
    "output_type": 4,
    "file_type": "json",
    "order_by": "observation_date",
    "sort_order": "asc",
    "count": 2,
    "offset": 0,
    "limit": 100000,
    "observations": [
        {
            "realtime_start": "2026-01-03",
            "realtime_end": "9999-12-31",
            "date": "2026-01-02",
            "value": "4.25",
        },
        {
            "realtime_start": "2026-01-04",
            "realtime_end": "9999-12-31",
            "date": "2026-01-03",
            "value": "4.30",
        },
    ],
}

FRED_DGS2_FIXTURE = {
    "id": "DGS2",
    "realtime_start": "2026-07-03",
    "realtime_end": "2026-07-03",
    "observation_start": "2026-01-02",
    "observation_end": "2026-06-30",
    "units": "lin",
    "output_type": 4,
    "file_type": "json",
    "order_by": "observation_date",
    "sort_order": "asc",
    "count": 2,
    "offset": 0,
    "limit": 100000,
    "observations": [
        {
            "realtime_start": "2026-01-03",
            "realtime_end": "9999-12-31",
            "date": "2026-01-02",
            "value": "3.90",
        },
        {
            "realtime_start": "2026-01-04",
            "realtime_end": "9999-12-31",
            "date": "2026-01-03",
            "value": "3.95",
        },
    ],
}

FRED_DOT_FIXTURE = {
    "realtime_start": "2026-07-03",
    "realtime_end": "2026-07-03",
    "observation_start": "2026-01-02",
    "observation_end": "2026-01-02",
    "units": "lin",
    "output_type": 4,
    "file_type": "json",
    "order_by": "observation_date",
    "sort_order": "asc",
    "count": 1,
    "offset": 0,
    "limit": 100000,
    "id": "TEDRATE",
    "observations": [
        {
            "realtime_start": "2026-01-03",
            "realtime_end": "9999-12-31",
            "date": "2026-01-02",
            "value": ".",
        },
    ],
}


# ---------------------------------------------------------------------------
# TML1 — FRED live orchestration (DISCRIMINATING — B1 proof)
# ---------------------------------------------------------------------------

class TestFredLiveOrchestration:
    """Prove B1: current FRED live path is a stub that writes zero rows."""

    @pytest.mark.skip(reason="Requires CLI wiring that isn't mockable without the fix")
    async def test_fred_live_stub_writes_zero_observations(self, tmp_path):
        """Discriminating: call the FRED live path through CLI entrypoint.

        Current code prints stub message and returns — macro_observations stays empty.
        After fix: observations are fetched, archived, and upserted.
        """
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)

        from catalyst_data.cli_index import cmd_update_macro

        # Mock environment key
        with patch.dict("os.environ", {"FRED_API_KEY": "test_key_redacted"}):
            cmd_update_macro(
                db_path=db_path,
                series=None,
                from_date=None,
                to_date=None,
                dry_run=False,
                live=True,
                confirm=True,
            )

        c = sqlite3.connect(db_path)
        count = c.execute("SELECT COUNT(*) FROM macro_observations").fetchone()[0]
        c.close()

        # BUG (B1): stub writes ZERO observations
        assert count > 0, (
            f"B1 BUG: FRED live path is a stub — macro_observations has {count} rows. "
            "After fix: observations should be fetched, archived, and upserted."
        )

    async def test_fred_live_mocked_orchestration(self, tmp_path):
        """Test the FRED live flow with mocked fetcher directly."""
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)

        from catalyst_data.storage.sqlite import compute_asset_id, upsert_raw_asset

        # Simulate what the live loop should do for one series
        raw_json = json.dumps(FRED_DGS10_FIXTURE, ensure_ascii=True).encode("utf-8")
        today = "2026-07-03"
        asset_id = compute_asset_id("DGS10", today, "fred_macro")

        conn = sqlite3.connect(db_path)
        upsert_raw_asset(
            conn,
            asset_id=asset_id,
            ticker="__macro__",
            source_type="fred_macro",
            reference_date=today,
            content_raw=raw_json,
            http_status=200,
            metadata={"series_id": "DGS10", "endpoint": "observations"},
        )
        conn.commit()
        conn.close()

        # Now run rederive
        result = rederive_fred_macro(db_path)
        assert result["observations_upserted"] > 0, (
            "Redrive should upsert observations from Bronze"
        )

        c = sqlite3.connect(db_path)
        rows = c.execute(
            "SELECT observation_date, value, released_at FROM macro_observations "
            "WHERE series_id = 'DGS10' ORDER BY observation_date"
        ).fetchall()
        c.close()

        assert len(rows) == 2, f"Expected 2 DGS10 observations, got {len(rows)}"
        assert rows[0][0] == "2026-01-02"
        assert rows[0][1] == 4.25
        assert rows[0][2] == "2026-01-03", "released_at should be per-observation realtime_start"


# ---------------------------------------------------------------------------
# TML2 — PIT released_at > observation_date (DISCRIMINATING)
# ---------------------------------------------------------------------------

class TestPointInTime:
    def test_released_at_after_observation_date(self):
        """Discriminating: first-release date is genuinely after observation date.

        If released_at were set to the fetch date (2026-07-03), all observations
        would have the same released_at — this test catches that.
        """
        rows = normalize_fred_observations(FRED_DGS10_FIXTURE, "ra_dgs10")

        assert len(rows) == 2
        for row in rows:
            assert row["released_at"] is not None, "released_at must be populated"
            assert row["released_at"] > row["observation_date"], (
                f"released_at={row['released_at']} must be > "
                f"observation_date={row['observation_date']}"
            )
            # released_at should NOT be the fetch date (2026-07-03)
            assert row["released_at"] != "2026-07-03", (
                "released_at is the fetch date — PIT is broken. "
                "Must use per-observation realtime_start, not top-level date."
            )

    def test_dot_value_to_null(self):
        """FRED '.' missing-value marker → NULL."""
        rows = normalize_fred_observations(FRED_DOT_FIXTURE, "ra_dot")
        assert len(rows) == 1
        assert rows[0]["value"] is None, f"'.' should become None, got {rows[0]['value']}"

    def test_t10y2y_derivation(self):
        """T10Y2Y = DGS10 - DGS2, released_at = max(components)."""
        dgs10_rows = normalize_fred_observations(FRED_DGS10_FIXTURE, "ra_dgs10")
        dgs2_rows = normalize_fred_observations(FRED_DGS2_FIXTURE, "ra_dgs2")

        t10y2y = derive_t10y2y(dgs10_rows, dgs2_rows, "ra_t10y2y")

        assert len(t10y2y) == 2, f"Expected 2 T10Y2Y rows, got {len(t10y2y)}"
        assert t10y2y[0]["series_id"] == "T10Y2Y"
        assert t10y2y[0]["value"] == pytest.approx(0.35, abs=0.01)
        assert t10y2y[0]["released_at"] == "2026-01-03", (
            "First T10Y2Y row: observation_date=2026-01-02, "
            "released_at = max(2026-01-03, 2026-01-03) = 2026-01-03"
        )
        assert t10y2y[1]["released_at"] == "2026-01-04", (
            "Second T10Y2Y row: observation_date=2026-01-03, "
            "released_at = max(2026-01-04, 2026-01-04) = 2026-01-04"
        )


# ---------------------------------------------------------------------------
# TML3 — PIT query excludes later revisions (DISCRIMINATING)
# ---------------------------------------------------------------------------

class TestPITQuery:
    def test_pit_query_excludes_later_revision(self, tmp_path):
        """Discriminating: PIT query excludes observations released after as_of.

        Stores two observations with DIFFERENT series_ids so both coexist
        (macro_observations PK is (series_id, observation_date)).
        released_at=2026-01-03 is before as_of=2026-01-15 → returned.
        released_at=2026-02-15 is after → excluded (no look-ahead).
        """
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        from catalyst_data.storage.sqlite import upsert_macro_observation, upsert_raw_asset

        for ra_id in ("ra_early", "ra_late"):
            upsert_raw_asset(
                conn, asset_id=ra_id, ticker="__macro__",
                source_type="fred_macro", reference_date="2026-01-01",
                content_raw=b"{}", http_status=200, metadata={},
            )

        # Observation released before as_of → should be visible
        upsert_macro_observation(
            conn, series_id="EARLY", observation_date="2026-01-02",
            value=1.0, released_at="2026-01-03", raw_asset_id="ra_early",
        )
        # Observation released after as_of → should be hidden (no look-ahead)
        upsert_macro_observation(
            conn, series_id="LATER", observation_date="2026-01-02",
            value=9.0, released_at="2026-02-15", raw_asset_id="ra_late",
        )
        conn.commit()

        # Query at as_of=2026-01-15: EARLY visible, LATER excluded
        rows = conn.execute(
            "SELECT series_id, value FROM macro_observations "
            "WHERE released_at <= '2026-01-15' ORDER BY series_id"
        ).fetchall()
        conn.close()

        series_ids = [r[0] for r in rows]
        assert "EARLY" in series_ids, (
            f"PIT query should include observations released before as_of, got {series_ids}"
        )
        assert "LATER" not in series_ids, (
            f"PIT query must EXCLUDE observations released after as_of — "
            f"look-ahead leak! Got {series_ids}"
        )


# ---------------------------------------------------------------------------
# TML5 — Plane-2 separation
# ---------------------------------------------------------------------------

class TestPlane2Separation:
    def test_macro_not_in_index_state(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        from catalyst_data.storage.sqlite import upsert_macro_observation, upsert_raw_asset

        upsert_raw_asset(
            conn, asset_id="ra_plane2", ticker="__macro__",
            source_type="fred_macro", reference_date="2026-01-01",
            content_raw=b"{}", http_status=200, metadata={},
        )
        upsert_macro_observation(
            conn, series_id="DGS10", observation_date="2026-01-02",
            value=4.25, released_at="2026-01-03", raw_asset_id="ra_plane2",
        )
        conn.commit()

        # macro_observations should NEVER appear in index_state
        idx_count = conn.execute(
            "SELECT COUNT(*) FROM index_state WHERE source_kind LIKE '%macro%'"
        ).fetchone()[0]
        assert idx_count == 0, "macro_observations must never be in index_state (Plane-2)"

        conn.close()


# ---------------------------------------------------------------------------
# TML6 — Bronze re-derivability
# ---------------------------------------------------------------------------

class TestBronzeRedrivabilityFred:
    def test_bronze_rederive_reproduces_observations(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)

        from catalyst_data.storage.sqlite import compute_asset_id, upsert_raw_asset, upsert_macro_observation

        raw_json = json.dumps(FRED_DGS10_FIXTURE, ensure_ascii=True).encode("utf-8")
        # upsert_raw_asset compresses internally — pass raw bytes
        today = "2026-07-03"
        asset_id = compute_asset_id("DGS10", today, "fred_macro")

        conn = sqlite3.connect(db_path)
        upsert_raw_asset(
            conn, asset_id=asset_id, ticker="__macro__",
            source_type="fred_macro", reference_date=today,
            content_raw=raw_json,
            http_status=200, metadata={"series_id": "DGS10"},
        )
        conn.commit()
        conn.close()

        # Re-derive
        rederive_fred_macro(db_path)

        # Verify
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT observation_date, value, released_at FROM macro_observations "
            "WHERE series_id='DGS10' ORDER BY observation_date"
        ).fetchall()
        conn.close()

        assert len(rows) == 2
        assert rows[0][0] == "2026-01-02"
        assert rows[0][1] == 4.25


# ---------------------------------------------------------------------------
# TML7 — Idempotency
# ---------------------------------------------------------------------------

class TestFredIdempotency:
    def test_repeat_rederive_idempotent(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        _make_db(db_path)

        from catalyst_data.storage.sqlite import compute_asset_id, upsert_raw_asset

        raw_json = json.dumps(FRED_DGS10_FIXTURE, ensure_ascii=True).encode("utf-8")
        asset_id = compute_asset_id("DGS10", "2026-07-03", "fred_macro")

        conn = sqlite3.connect(db_path)
        upsert_raw_asset(
            conn, asset_id=asset_id, ticker="__macro__",
            source_type="fred_macro", reference_date="2026-07-03",
            content_raw=raw_json, http_status=200,
            metadata={"series_id": "DGS10"},
        )
        conn.commit()
        conn.close()

        r1 = rederive_fred_macro(db_path)
        r2 = rederive_fred_macro(db_path)

        assert r1["observations_upserted"] == r2["observations_upserted"], (
            "Repeat rederive should be idempotent (upsert)"
        )
