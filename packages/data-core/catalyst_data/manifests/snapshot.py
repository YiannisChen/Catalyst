from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import struct
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping

from .universe import SourceWindows, canonical_json_bytes, sha256_identity

SNAPSHOT_TABLE_INVENTORY: dict[str, dict[str, Any]] = {
    "raw_assets": {"key": ("asset_id",), "columns": None},
    "clean_assets": {"key": ("asset_id",), "columns": None},
    "ohlcv": {"key": ("symbol", "date"), "columns": None},
    "articles": {"key": ("article_id",), "columns": None},
    "article_tickers": {"key": ("article_id", "ticker"), "columns": None},
    "filings": {"key": ("filing_id",), "columns": None},
    "filing_documents": {"key": ("filing_id", "document_url"), "columns": None},
    "macro_observations": {"key": ("series_id", "observation_date"), "columns": None},
    "fundamental_statements": {"key": ("statement_id",), "columns": None},
    "normalized_provenance": {"key": ("entity_type", "entity_id", "entity_version", "raw_asset_id"), "columns": None},
    "source_checkpoints": {
        "key": ("run_id", "source_type", "ticker", "date"),
        "columns": (
            "run_id", "source_type", "ticker", "date", "status", "empty_reason",
            "logical_fetch_id", "request_count", "pages_received", "items_received",
            "is_complete", "raw_asset_id", "items_count", "http_status",
            "cell_id", "window_start", "window_end", "endpoint_name",
            "provider_profile_version",
        ),
    },
    "ingestion_runs": {
        "key": ("run_id",),
        "columns": (
            "run_id", "plan_hash", "expected_plan_hash", "parent_run_id",
            "ticker_list_json", "source_list_json", "status", "success_count",
            "fail_count", "cancel_requested",
        ),
    },
}


@dataclass(frozen=True)
class DataSnapshotManifest:
    schema_version: str
    snapshot_id: str
    universe_manifest_id: str
    plan_hash: str
    db_user_version: int
    table_inventory: dict[str, Any]
    table_hashes: dict[str, str]
    protected_source_sha256: str
    source_windows: dict[str, str]
    coverage_states: dict[str, Any]
    created_at: str
    report_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise ValueError(f"required snapshot table missing: {table}")
    return [(row[1], (row[2] or "").upper()) for row in rows]


def _typed_value(value: Any, declared_type: str) -> dict[str, str]:
    if value is None:
        return {"type": "null"}
    dtype = (declared_type or "").upper()
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    if "INT" in dtype:
        return {"type": "integer", "decimal": str(int(value))}
    if any(token in dtype for token in ("REAL", "FLOA", "DOUB")):
        real = float(value)
        if not math.isfinite(real):
            raise ValueError("non-finite REAL cannot enter snapshot identity")
        return {"type": "real", "ieee754_hex": struct.pack(">d", real).hex()}
    if isinstance(value, int):
        return {"type": "integer", "decimal": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite REAL cannot enter snapshot identity")
        return {"type": "real", "ieee754_hex": struct.pack(">d", value).hex()}
    return {"type": "text", "utf8": str(value)}


def _table_hash(conn: sqlite3.Connection, table: str, key_columns: tuple[str, ...], included: tuple[str, ...] | None) -> str:
    col_defs = _columns(conn, table)
    col_type = dict(col_defs)
    available = [name for name, _ in col_defs]
    columns = list(included) if included is not None else available
    columns = [col for col in columns if col in available]
    missing_keys = [col for col in key_columns if col not in available]
    if missing_keys:
        raise ValueError(f"{table} missing key columns {missing_keys}")
    header = {
        "table": table,
        "key_columns": list(key_columns),
        "columns": columns,
    }
    h = hashlib.sha256()
    h.update(canonical_json_bytes(header))
    h.update(b"\n")
    select_cols = ", ".join(f'"{col}"' for col in columns)
    order_by = ", ".join(f'"{col}"' for col in key_columns)
    for row in conn.execute(f"SELECT {select_cols} FROM {table} ORDER BY {order_by}"):
        row_obj = {
            col: _typed_value(row[idx], col_type.get(col, ""))
            for idx, col in enumerate(columns)
        }
        h.update(canonical_json_bytes(row_obj))
        h.update(b"\n")
    return h.hexdigest()


def build_data_snapshot_manifest(
    conn: sqlite3.Connection,
    *,
    universe_manifest_id: str,
    plan_hash: str,
    protected_source_sha256: str,
    source_windows: SourceWindows,
    created_at: datetime,
    coverage_states: Mapping[str, Any] | None = None,
    report_path: str | None = None,
) -> DataSnapshotManifest:
    db_user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if db_user_version != 11:
        raise ValueError(f"snapshot requires db user_version 11, got {db_user_version}")
    coverage = dict(coverage_states or {})
    if not coverage:
        raise ValueError("snapshot requires readiness coverage_states")
    overall = coverage.get("overall_readiness") or {}
    comparable = coverage.get("canonical_news_comparable_gate") or coverage.get("comparable_gate") or {}
    if overall.get("status") != "complete":
        raise ValueError("snapshot readiness overall_readiness incomplete")
    if comparable.get("status") != "complete":
        raise ValueError("snapshot readiness comparable gate incomplete")
    table_hashes: dict[str, str] = {}
    inventory: dict[str, Any] = {}
    for table, spec in SNAPSHOT_TABLE_INVENTORY.items():
        key = tuple(spec["key"])
        cols = spec["columns"]
        table_hashes[table] = _table_hash(conn, table, key, cols)
        inventory[table] = {
            "key": list(key),
            "columns": list(cols) if cols is not None else [name for name, _ in _columns(conn, table)],
        }
    identity = {
        "schema_version": "1.0.0",
        "universe_manifest_id": universe_manifest_id,
        "plan_hash": plan_hash,
        "db_user_version": db_user_version,
        "table_inventory": inventory,
        "table_hashes": table_hashes,
        "protected_source_sha256": protected_source_sha256,
        "source_windows": source_windows.to_identity(),
        "coverage_states": coverage,
    }
    snapshot_id = sha256_identity(identity)
    return DataSnapshotManifest(
        schema_version="1.0.0",
        snapshot_id=snapshot_id,
        universe_manifest_id=universe_manifest_id,
        plan_hash=plan_hash,
        db_user_version=db_user_version,
        table_inventory=inventory,
        table_hashes=table_hashes,
        protected_source_sha256=protected_source_sha256,
        source_windows=source_windows.to_identity(),
        coverage_states=coverage,
        created_at=created_at.isoformat(),
        report_path=report_path,
    )
