#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_PACKAGE_ROOT = Path(__file__).resolve().parent.parent

WINDOW_DAYS = 3
DIRECT_SOURCE_TYPES = (
    "polygon_news",
    "fmp_fundamentals",
    "fmp_news",
    "finnhub_company_news",
    "sec_filing",
)
MACRO_SOURCE_TYPES = ("fred_macro", "gdelt_news")
GEO_KEYWORDS = re.compile(r"(tariff|policy|fed|rate|inflation|export|ban|sanction|trade|war)", re.IGNORECASE)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _window_bounds(trade_date: str, window_days: int) -> tuple[str, str]:
    anchor = datetime.strptime(trade_date, "%Y-%m-%d")
    start = (anchor - timedelta(days=window_days)).strftime("%Y-%m-%d")
    end = (anchor + timedelta(days=window_days)).strftime("%Y-%m-%d")
    return start, end


def _db_reference_window(conn: sqlite3.Connection) -> dict[str, Any]:
    min_date, max_date = conn.execute(
        "SELECT MIN(reference_date), MAX(reference_date) FROM clean_assets"
    ).fetchone()
    total = conn.execute("SELECT COUNT(*) FROM clean_assets").fetchone()[0]
    return {
        "min_reference_date": min_date,
        "max_reference_date": max_date,
        "clean_assets": int(total),
    }


def build_coverage_audit(
    *,
    db_path: Path,
    golden_set_path: Path,
    window_days: int = WINDOW_DAYS,
) -> dict[str, Any]:
    db_path = db_path.resolve()
    golden_set_path = golden_set_path.resolve()
    cases = _load_cases(golden_set_path)

    conn = sqlite3.connect(str(db_path))
    try:
        report_cases: list[dict[str, Any]] = []
        for case in cases:
            start, end = _window_bounds(case["trade_date"], window_days)
            direct_placeholders = ",".join("?" for _ in DIRECT_SOURCE_TYPES)
            direct_params = (case["ticker"], start, end, *DIRECT_SOURCE_TYPES)
            direct_doc_count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM clean_assets
                WHERE ticker = ?
                  AND reference_date BETWEEN ? AND ?
                  AND source_type IN ({direct_placeholders})
                """,
                direct_params,
            ).fetchone()[0]

            total_ticker_docs = conn.execute(
                """
                SELECT COUNT(*)
                FROM clean_assets
                WHERE ticker = ?
                  AND reference_date BETWEEN ? AND ?
                """,
                (case["ticker"], start, end),
            ).fetchone()[0]

            macro_doc_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM clean_assets
                WHERE reference_date BETWEEN ? AND ?
                  AND source_type IN (?, ?)
                """,
                (start, end, *MACRO_SOURCE_TYPES),
            ).fetchone()[0]

            geo_keyword_news_count = 0
            source_types_present = set()
            for source_type, content_md in conn.execute(
                """
                SELECT source_type, content_md
                FROM clean_assets
                WHERE reference_date BETWEEN ? AND ?
                """,
                (start, end),
            ):
                source_types_present.add(source_type)
                if source_type == "polygon_news" and GEO_KEYWORDS.search(content_md or ""):
                    geo_keyword_news_count += 1

            report_cases.append(
                {
                    "id": case["id"],
                    "ticker": case["ticker"],
                    "trade_date": case["trade_date"],
                    "window_start": start,
                    "window_end": end,
                    "direct_doc_count": int(direct_doc_count),
                    "macro_doc_count": int(macro_doc_count + geo_keyword_news_count),
                    "geo_keyword_news_count": int(geo_keyword_news_count),
                    "ticker_window_doc_count": int(total_ticker_docs),
                    "covered_direct": bool(direct_doc_count > 0),
                    "covered_macro": bool((macro_doc_count + geo_keyword_news_count) > 0),
                    "source_types_present": sorted(source_types_present),
                }
            )

        return {
            "schema_version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "db_path": str(db_path),
            "db_sha256": sha256_file(db_path),
            "golden_set_path": str(golden_set_path),
            "golden_set_sha256": sha256_file(golden_set_path),
            "window_days": window_days,
            "db_reference_window": _db_reference_window(conn),
            "summary": {
                "total_cases": len(report_cases),
                "cases_with_direct_docs": sum(1 for row in report_cases if row["covered_direct"]),
                "cases_with_macro_docs": sum(1 for row in report_cases if row["covered_macro"]),
                "cases_with_any_ticker_docs": sum(1 for row in report_cases if row["ticker_window_doc_count"] > 0),
                "cases_missing_direct_docs": [row["id"] for row in report_cases if not row["covered_direct"]],
                "cases_missing_any_ticker_docs": [
                    row["id"] for row in report_cases if row["ticker_window_doc_count"] == 0
                ],
            },
            "cases": report_cases,
        }
    finally:
        conn.close()


def _default_output_path() -> Path:
    out_dir = _PACKAGE_ROOT.parent.parent / "data" / "eval_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return out_dir / f"v1_2_coverage_audit_{ts}.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit v1_2 golden-set coverage against a frozen DB.")
    parser.add_argument("--db", default="data/catalyst_eval_frozen_v2.db")
    parser.add_argument("--golden-set", default="packages/eval/golden_set/v1_2.jsonl")
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_coverage_audit(
        db_path=Path(args.db),
        golden_set_path=Path(args.golden_set),
        window_days=args.window_days,
    )
    out_path = Path(args.output) if args.output else _default_output_path()
    _write_json(out_path, report)
    print(json.dumps({"output_path": str(out_path.resolve()), "total_cases": report["summary"]["total_cases"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
