"""FRED normalization: JSON observations → macro_observations rows.

Normalizes raw FRED output_type=4 responses into macro_observations rows.
Handles "." missing-value markers → NULL, extracts per-observation
realtime_start for point-in-time integrity, and derives T10Y2Y spread.

Includes rederive_fred_macro() for Bronze→Silver re-derivability.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_data.pipeline.fred_manifest import CURATED_SERIES, series_by_id
from catalyst_data.storage.sqlite import upsert_macro_observation

logger = logging.getLogger(__name__)


def _parse_value(raw_value: str | None) -> float | None:
    """Convert FRED value string to float, treating '.' as NULL."""
    if raw_value is None or raw_value == ".":
        return None
    try:
        return float(raw_value)
    except (ValueError, TypeError):
        return None


def normalize_fred_observations(
    raw_data: dict[str, Any],
    raw_asset_id: str,
) -> list[dict[str, Any]]:
    """Convert a FRED output_type=4 response into macro_observations row dicts.

    Args:
        raw_data: Parsed FRED JSON response (output_type=4).
        raw_asset_id: Bronze asset ID for provenance.

    Returns:
        List of dicts with keys: series_id, observation_date, value, released_at, raw_asset_id.
    """
    observations = raw_data.get("observations", [])
    if not isinstance(observations, list):
        return []

    rows = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        obs_date = obs.get("date", "")
        raw_value = obs.get("value", ".")
        value = _parse_value(raw_value)
        # per-observation realtime_start = true first-release date
        released_at = obs.get("realtime_start") or None

        if not obs_date:
            continue

        rows.append({
            "series_id": raw_data.get("id", ""),
            "observation_date": obs_date,
            "value": value,
            "released_at": released_at,
            "raw_asset_id": raw_asset_id,
        })

    return rows


def derive_t10y2y(
    dgs10_rows: list[dict[str, Any]],
    dgs2_rows: list[dict[str, Any]],
    raw_asset_id: str,
) -> list[dict[str, Any]]:
    """Derive T10Y2Y spread from DGS10 and DGS2 normalized rows.

    For each date where both DGS10 and DGS2 have non-NULL values:
        T10Y2Y = DGS10.value − DGS2.value
        released_at = max(DGS10.released_at, DGS2.released_at)

    Returns list of T10Y2Y row dicts (series_id='T10Y2Y', derived).
    """
    dgs10_by_date = {}
    for r in dgs10_rows:
        if r["value"] is not None:
            dgs10_by_date[r["observation_date"]] = r

    t10y2y_rows = []
    for r in dgs2_rows:
        obs_date = r["observation_date"]
        if r["value"] is None:
            continue
        d10 = dgs10_by_date.get(obs_date)
        if d10 is None or d10["value"] is None:
            continue

        spread = d10["value"] - r["value"]

        # released_at = later of the two component release dates
        released_a = d10.get("released_at") or ""
        released_b = r.get("released_at") or ""
        released_at = (
            max(released_a, released_b) if released_a and released_b
            else released_a or released_b
        )

        t10y2y_rows.append({
            "series_id": "T10Y2Y",
            "observation_date": obs_date,
            "value": round(spread, 6),
            "released_at": released_at or None,
            "raw_asset_id": raw_asset_id,
        })

    return t10y2y_rows


def rederive_fred_macro(db_path: str | Path) -> dict[str, int]:
    """Re-derive all fred_macro observations from raw_assets (Bronze → Silver).

    Reads raw_assets WHERE source_type='fred_macro', decompresses, normalizes,
    upserts into macro_observations. Also derives T10Y2Y from DGS10 + DGS2.

    Returns dict with counts: raw_rows_processed, observations_upserted.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    raw_rows = conn.execute(
        "SELECT asset_id, content_raw FROM raw_assets WHERE source_type = 'fred_macro'"
    ).fetchall()

    observations_upserted = 0

    # Collect DGS10 and DGS2 rows for T10Y2Y derivation
    dgs10_rows: list[dict[str, Any]] = []
    dgs2_rows: list[dict[str, Any]] = []

    for raw_asset_id, compressed in raw_rows:
        try:
            payload = zlib.decompress(compressed)
        except zlib.error as exc:
            logger.warning("Failed to decompress %s: %s", raw_asset_id, exc)
            continue

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            logger.warning("Invalid JSON in %s: %s", raw_asset_id, exc)
            continue

        rows = normalize_fred_observations(data, raw_asset_id)
        for row in rows:
            upsert_macro_observation(
                conn,
                series_id=row["series_id"],
                observation_date=row["observation_date"],
                value=row["value"],
                released_at=row["released_at"],
                raw_asset_id=row["raw_asset_id"],
            )
            observations_upserted += 1

            # Collect for T10Y2Y derivation
            if row["series_id"] == "DGS10":
                dgs10_rows.append(row)
            elif row["series_id"] == "DGS2":
                dgs2_rows.append(row)

    # Derive T10Y2Y
    if dgs10_rows and dgs2_rows:
        t10y2y = derive_t10y2y(dgs10_rows, dgs2_rows, dgs10_rows[0].get("raw_asset_id", ""))
        for row in t10y2y:
            upsert_macro_observation(
                conn,
                series_id=row["series_id"],
                observation_date=row["observation_date"],
                value=row["value"],
                released_at=row["released_at"],
                raw_asset_id=row["raw_asset_id"],
            )
            observations_upserted += 1

    conn.close()

    logger.info(
        "FRED re-derive complete: %d raw rows → %d observations upserted",
        len(raw_rows), observations_upserted,
    )

    return {
        "raw_rows_processed": len(raw_rows),
        "observations_upserted": observations_upserted,
    }
