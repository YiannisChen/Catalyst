from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone

from data_core.models import DataAsset


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS data_assets (
    asset_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    reference_date_utc TEXT NOT NULL,
    reference_date_et TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    data_version TEXT NOT NULL,
    is_final INTEGER NOT NULL,
    content_raw BLOB,
    content_clean TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
"""


def compute_asset_id(ticker: str, date: str, source_type: str, data_version: str) -> str:
    raw = f"{ticker}|{date}|{source_type}|{data_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def init_db(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA journal_mode=WAL;").fetchone()
    # In-memory DBs return "memory"; file-backed DBs must return "wal".
    if result and result[0] not in ("wal", "memory"):
        raise RuntimeError(
            f"Failed to enable WAL journal mode, got: {result[0]}"
        )
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()


def upsert_asset(conn: sqlite3.Connection, asset: DataAsset) -> None:
    raw_payload = zlib.compress(asset.content_raw) if asset.content_raw is not None else None
    metadata_json = json.dumps(asset.metadata, ensure_ascii=True, sort_keys=True)

    conn.execute(
        """
        INSERT INTO data_assets (
            asset_id, ticker, source_type, reference_date_utc, reference_date_et,
            last_updated, data_version, is_final, content_raw, content_clean, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset_id) DO UPDATE SET
            ticker=excluded.ticker,
            source_type=excluded.source_type,
            reference_date_utc=excluded.reference_date_utc,
            reference_date_et=excluded.reference_date_et,
            last_updated=excluded.last_updated,
            data_version=excluded.data_version,
            is_final=excluded.is_final,
            content_raw=excluded.content_raw,
            content_clean=excluded.content_clean,
            metadata_json=excluded.metadata_json
        """,
        (
            asset.asset_id,
            asset.ticker,
            asset.source_type,
            asset.reference_date_utc.isoformat(),
            asset.reference_date_et,
            asset.last_updated.isoformat(),
            asset.data_version,
            1 if asset.is_final else 0,
            raw_payload,
            asset.content_clean,
            metadata_json,
        ),
    )
    conn.commit()


def _is_ttl_expired(source_type: str, is_final: bool, last_updated: datetime, now_utc: datetime) -> bool:
    # Infinite TTL for gdelt_news.
    if source_type == "gdelt_news":
        return False

    # For fundamentals: non-final data expires after 24 hours.
    if source_type == "fmp_fundamentals" and not is_final:
        return now_utc - last_updated > timedelta(hours=24)

    # All other V1 paths default to infinite TTL.
    return False


def get_cached_asset(
    conn: sqlite3.Connection,
    ticker: str,
    date: str,
    source_type: str,
    data_version: str,
    force_refresh: bool,
    now_utc: datetime | None = None,
) -> DataAsset | None:
    if force_refresh:
        return None

    asset_id = compute_asset_id(ticker, date, source_type, data_version)
    row = conn.execute(
        """
        SELECT
            asset_id, ticker, source_type, reference_date_utc, reference_date_et,
            last_updated, data_version, is_final, content_raw, content_clean, metadata_json
        FROM data_assets
        WHERE asset_id = ?
        """,
        (asset_id,),
    ).fetchone()

    if row is None:
        return None

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    last_updated = datetime.fromisoformat(row[5])
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=timezone.utc)

    is_final = bool(row[7])
    if _is_ttl_expired(source_type=row[2], is_final=is_final, last_updated=last_updated, now_utc=now_utc):
        return None

    raw_payload = row[8]
    if raw_payload is not None:
        try:
            content_raw = zlib.decompress(raw_payload)
        except zlib.error:
            return None
    else:
        content_raw = None
    metadata = json.loads(row[10]) if row[10] else {}

    return DataAsset(
        asset_id=row[0],
        ticker=row[1],
        source_type=row[2],
        reference_date_utc=datetime.fromisoformat(row[3]),
        reference_date_et=row[4],
        last_updated=last_updated,
        data_version=row[6],
        is_final=is_final,
        content_raw=content_raw,
        content_clean=row[9],
        metadata=metadata,
    )
