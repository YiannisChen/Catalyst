"""Pre-eval preflight for the frozen Catalyst P0 corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCHEMA_VERSION = "1.0"
WINDOW_DAYS = 3
GEO_CORPUS_TIER_P0 = 2
MACRO_THRESHOLD = 30
DIRECT_THRESHOLD = 5
GEO_TIER2_THRESHOLD = 20
DIRECT_SOURCE_TYPES = ("polygon_news", "fmp_fundamentals", "fmp_news", "finnhub_company_news", "sec_filing")
GEO_KEYWORDS = re.compile(r"(tariff|policy|fed|rate|inflation|export|ban|sanction|trade|war)", re.IGNORECASE)
DEFAULT_GOLDEN_SET = Path("packages/eval/golden_set/v1_2_p0_set.jsonl")
DEFAULT_LANCEDB_DIR = Path("data/lancedb_gold/eval_frozen")
W15_LANCEDB_SENTINEL = "DEFERRED_P1"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run T-13a preflight checks against a frozen DB candidate.")
    parser.add_argument("--db", required=True, help="Path to the candidate frozen SQLite DB.")
    parser.add_argument(
        "--golden-set",
        default=str(DEFAULT_GOLDEN_SET),
        help="Path to the frozen 10-case JSONL set. Defaults to packages/eval/golden_set/v1_2_p0_set.jsonl.",
    )
    parser.add_argument(
        "--lancedb-dir",
        default=str(DEFAULT_LANCEDB_DIR),
        help="Path to the deferred/active LanceDB freeze directory.",
    )
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_golden_set(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _answerable_cases(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not bool(row.get("should_refuse"))]


def _windows_for_cases(rows: list[dict]) -> list[tuple[str, str]]:
    windows: list[tuple[str, str]] = []
    for row in rows:
        trade_date = datetime.strptime(row["trade_date"], "%Y-%m-%d")
        start = (trade_date - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
        end = (trade_date + timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
        windows.append((start, end))
    return windows


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _macro_doc_count(conn: sqlite3.Connection, windows: list[tuple[str, str]]) -> int:
    seen: set[str] = set()
    for start, end in windows:
        for (asset_id,) in conn.execute(
            "SELECT asset_id FROM clean_assets WHERE source_type = 'fred_macro' AND reference_date BETWEEN ? AND ?",
            (start, end),
        ):
            seen.add(asset_id)
    return len(seen)


def _direct_evidence_per_ticker_min(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, dict[str, int]]:
    per_ticker_windows: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        trade_date = datetime.strptime(row["trade_date"], "%Y-%m-%d")
        start = (trade_date - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
        end = (trade_date + timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
        per_ticker_windows.setdefault(row["ticker"], []).append((start, end))

    per_ticker_counts: dict[str, int] = {}
    for ticker, ticker_windows in per_ticker_windows.items():
        seen: set[str] = set()
        for start, end in ticker_windows:
            placeholders = ",".join("?" for _ in DIRECT_SOURCE_TYPES)
            params = (ticker, start, end, *DIRECT_SOURCE_TYPES)
            query = f"""
                SELECT asset_id
                FROM clean_assets
                WHERE ticker = ?
                  AND reference_date BETWEEN ? AND ?
                  AND source_type IN ({placeholders})
            """
            for (asset_id,) in conn.execute(query, params):
                seen.add(asset_id)
        per_ticker_counts[ticker] = len(seen)

    return min(per_ticker_counts.values()), per_ticker_counts


def _geo_tier2_doc_count(conn: sqlite3.Connection, windows: list[tuple[str, str]]) -> int:
    seen: set[str] = set()
    for start, end in windows:
        for asset_id, content_md in conn.execute(
            "SELECT asset_id, content_md FROM clean_assets WHERE source_type = 'polygon_news' AND reference_date BETWEEN ? AND ?",
            (start, end),
        ):
            if GEO_KEYWORDS.search(content_md or ""):
                seen.add(asset_id)
    return len(seen)


def _lancedb_freeze_status(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "deferred_p1", W15_LANCEDB_SENTINEL
    if any(path.rglob("*")):
        files = [p for p in path.rglob("*") if p.is_file()]
        if files:
            return "available", W15_LANCEDB_SENTINEL
    return "deferred_p1", W15_LANCEDB_SENTINEL


def _write_artifact(payload: dict) -> Path:
    out_dir = Path("data/eval_reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"preflight_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return out_path


def main() -> int:
    args = _parse_args()
    db_path = Path(args.db)
    golden_set_path = Path(args.golden_set)
    lancedb_dir = Path(args.lancedb_dir)

    rows = _load_golden_set(golden_set_path)
    answerable = _answerable_cases(rows)
    windows = _windows_for_cases(answerable)

    conn = sqlite3.connect(db_path)
    row_counts = {
        "raw_assets": _table_count(conn, "raw_assets"),
        "clean_assets": _table_count(conn, "clean_assets"),
    }
    macro_count = _macro_doc_count(conn, windows)
    direct_min, per_ticker_counts = _direct_evidence_per_ticker_min(conn, answerable)
    geo_tier2_count = _geo_tier2_doc_count(conn, windows)
    conn.close()

    lancedb_status, lancedb_dir_sha256 = _lancedb_freeze_status(lancedb_dir)
    passed = (
        macro_count >= MACRO_THRESHOLD
        and direct_min >= DIRECT_THRESHOLD
        and geo_tier2_count >= GEO_TIER2_THRESHOLD
    )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "db_sha256": _sha256_file(db_path),
        "lancedb_status": lancedb_status,
        "lancedb_dir_sha256": lancedb_dir_sha256,
        "geo_corpus_tier": GEO_CORPUS_TIER_P0,
        "row_counts": row_counts,
        "answerable_case_count": len(answerable),
        "frozen_golden_set": str(golden_set_path),
        "checks": {
            "macro_doc_count": {
                "threshold": MACRO_THRESHOLD,
                "value": macro_count,
                "pass": macro_count >= MACRO_THRESHOLD,
            },
            "direct_evidence_per_ticker_min": {
                "threshold": DIRECT_THRESHOLD,
                "value": direct_min,
                "per_ticker": per_ticker_counts,
                "pass": direct_min >= DIRECT_THRESHOLD,
            },
            "geo_tier2_doc_count": {
                "threshold": GEO_TIER2_THRESHOLD,
                "value": geo_tier2_count,
                "pass": geo_tier2_count >= GEO_TIER2_THRESHOLD,
            },
        },
        "pass": passed,
    }

    out_path = _write_artifact(payload)
    print(json.dumps({"out_path": str(out_path), "pass": passed}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
