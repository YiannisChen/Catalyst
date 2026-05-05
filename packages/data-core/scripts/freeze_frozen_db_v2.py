#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PACKAGE_ROOT))


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seed_v2_database(*, source_db: Path, target_db: Path, replace: bool = False) -> None:
    source_db = source_db.resolve()
    target_db = target_db.resolve()
    if not source_db.exists():
        raise FileNotFoundError(f"Source DB does not exist: {source_db}")
    if target_db.exists():
        if not replace:
            return
        target_db.unlink()
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(str(source_db))
    target_conn = sqlite3.connect(str(target_db))
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _reference_window(conn: sqlite3.Connection) -> dict[str, Any]:
    min_date, max_date = conn.execute(
        "SELECT MIN(reference_date), MAX(reference_date) FROM clean_assets"
    ).fetchone()
    return {"min_reference_date": min_date, "max_reference_date": max_date}


def _asset_id_set(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[0] for row in conn.execute(f"SELECT asset_id FROM {table}")}


def _ohlcv_key_set(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    if not _table_exists(conn, "ohlcv"):
        return set()
    return {(row[0], row[1]) for row in conn.execute("SELECT symbol, date FROM ohlcv")}


def _load_latest_ingestion_notes(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if not _table_exists(conn, "ingestion_runs"):
        return None
    row = conn.execute(
        """
        SELECT notes
        FROM ingestion_runs
        WHERE notes IS NOT NULL AND notes != ''
        ORDER BY started_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return {"raw_notes": row[0]}


def build_provenance_manifest(
    *,
    source_db: Path,
    target_db: Path,
    start_date: str,
    end_date: str,
    coverage_artifact_path: Path | None,
) -> dict[str, Any]:
    source_db = source_db.resolve()
    target_db = target_db.resolve()
    source_conn = sqlite3.connect(str(source_db))
    target_conn = sqlite3.connect(str(target_db))
    try:
        table_names = ["raw_assets", "clean_assets", "ohlcv", "source_checkpoints", "ingestion_runs"]
        table_counts = {
            "source": {table: _table_count(source_conn, table) for table in table_names},
            "target": {table: _table_count(target_conn, table) for table in table_names},
        }

        source_clean_assets = _asset_id_set(source_conn, "clean_assets")
        target_clean_assets = _asset_id_set(target_conn, "clean_assets")
        source_raw_assets = _asset_id_set(source_conn, "raw_assets")
        target_raw_assets = _asset_id_set(target_conn, "raw_assets")
        source_ohlcv = _ohlcv_key_set(source_conn)
        target_ohlcv = _ohlcv_key_set(target_conn)
        latest_notes = _load_latest_ingestion_notes(target_conn)

        return {
            "schema_version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_db": str(source_db),
            "source_db_sha256": sha256_file(source_db),
            "target_db": str(target_db),
            "target_db_sha256": sha256_file(target_db),
            "backfill_window": {"start": start_date, "end": end_date},
            "table_counts": table_counts,
            "reference_windows": {
                "source": _reference_window(source_conn),
                "target": _reference_window(target_conn),
            },
            "superset_checks": {
                "clean_assets": {
                    "is_superset": source_clean_assets.issubset(target_clean_assets),
                    "missing_asset_ids": sorted(source_clean_assets - target_clean_assets)[:20],
                },
                "raw_assets": {
                    "is_superset": source_raw_assets.issubset(target_raw_assets),
                    "missing_asset_ids": sorted(source_raw_assets - target_raw_assets)[:20],
                },
                "ohlcv": {
                    "is_superset": source_ohlcv.issubset(target_ohlcv),
                    "missing_keys": [
                        {"symbol": symbol, "date": date}
                        for symbol, date in sorted(source_ohlcv - target_ohlcv)[:20]
                    ],
                },
            },
            "coverage_artifact_path": str(coverage_artifact_path.resolve()) if coverage_artifact_path else None,
            "provider_assumptions": (latest_notes or {}).get("sources"),
            "rate_limit_assumptions": (latest_notes or {}).get("rate_policies"),
            "key_assumptions": (latest_notes or {}).get("api_key_ids"),
            "run_notes": latest_notes,
        }
    finally:
        target_conn.close()
        source_conn.close()


def _default_report_path() -> Path:
    report_dir = _PACKAGE_ROOT.parent.parent / "data" / "eval_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return report_dir / f"frozen_db_v2_manifest_{ts}.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed and verify catalyst_eval_frozen_v2.db provenance.")
    parser.add_argument("--source-db", default="data/catalyst_eval_frozen.db")
    parser.add_argument("--target-db", default="data/catalyst_eval_frozen_v2.db")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--coverage-artifact", default=None)
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_db = Path(args.source_db)
    target_db = Path(args.target_db)
    seed_v2_database(source_db=source_db, target_db=target_db, replace=args.replace)
    manifest = build_provenance_manifest(
        source_db=source_db,
        target_db=target_db,
        start_date=args.start_date,
        end_date=args.end_date,
        coverage_artifact_path=Path(args.coverage_artifact) if args.coverage_artifact else None,
    )
    report_path = Path(args.report_path) if args.report_path else _default_report_path()
    _write_json(report_path, manifest)
    print(json.dumps({"report_path": str(report_path.resolve()), "target_db": str(target_db.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
