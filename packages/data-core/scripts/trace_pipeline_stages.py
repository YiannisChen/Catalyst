#!/usr/bin/env python3
"""Replay clean + transform from a Bronze row to inspect each stage.

Bronze ``content_raw`` in SQLite is **zlib-compressed JSON** — it is exactly the
``validated_data`` written by the orchestrator **after ingest** (successful
endpoints only). Per-endpoint HTTP responses are merged there; there is no
separate "fetch-only" snapshot in the DB.

**Not covered here:** ``align.py`` (``map_to_trade_date``, etc.) is **not**
called by ``process_request`` today; use it in a separate workflow if you need
news-to-trading-day alignment.

Usage (from ``packages/data-core``)::

    python -m scripts.trace_pipeline_stages \\
        --ticker AAPL --reference-date 2026-04-03 --source-type polygon_news

    python -m scripts.trace_pipeline_stages \\
        --ticker AAPL --reference-date 2026-04-03 --source-type fmp_fundamentals
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import zlib
from pathlib import Path
from typing import Any

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PACKAGE_ROOT))

from catalyst_data.pipeline.clean import run_clean  # noqa: E402
from catalyst_data.pipeline.transform import run_transform  # noqa: E402


def _load_bronze_json(conn: sqlite3.Connection, ticker: str, ref: str, source: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT content_raw FROM raw_assets
        WHERE ticker = ? AND reference_date = ? AND source_type = ?
        """,
        (ticker, ref, source),
    ).fetchone()
    if row is None:
        raise SystemExit(
            f"No raw_assets row for ticker={ticker!r} reference_date={ref!r} source_type={source!r}"
        )
    return json.loads(zlib.decompress(row[0]).decode("utf-8"))


def _truncate(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"\n... [{len(s) - max_chars} more chars truncated]"


def _news_article_count_ingest_shape(data: dict[str, Any]) -> int:
    """Match clean.py entry shape: outer dict may be single-key endpoint wrapper."""
    raw: Any = data
    if isinstance(raw, dict) and len(raw) == 1:
        only = next(iter(raw.values()))
        if isinstance(only, dict):
            raw = only
    articles = (
        raw.get("articles")
        or raw.get("results")
        or (raw if isinstance(raw, list) else [])
    )
    if isinstance(articles, dict):
        articles = [articles]
    if not isinstance(articles, list):
        return 0
    return sum(1 for a in articles if isinstance(a, dict))


def _article_one_line(a: dict[str, Any]) -> str:
    title = (a.get("title") or "")[:80]
    url = a.get("article_url") or a.get("url") or ""
    pub = a.get("published_utc") or ""
    return f"  title={title!r} published={pub!r} url={url[:60]!r}..."


def main() -> None:
    parser = argparse.ArgumentParser(description="Trace clean/transform from Bronze in SQLite")
    parser.add_argument("--db", type=Path, help="Path to dev_assets.db")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--reference-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--source-type", required=True)
    parser.add_argument("--max-json", type=int, default=6000, help="Max chars for JSON dumps")
    parser.add_argument("--max-md", type=int, default=8000, help="Max chars for Markdown preview")
    parser.add_argument(
        "--article-index",
        type=int,
        default=None,
        help="If set, print one article before/after clean (news only), by list index",
    )
    args = parser.parse_args()

    db_path = args.db
    if db_path is None:
        db_path = _PACKAGE_ROOT.parent.parent / "data" / "dev_assets.db"
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    bronze = _load_bronze_json(conn, args.ticker, args.reference_date, args.source_type)
    conn.close()

    n_pre: int | None = None

    print("=" * 72)
    print("STAGE 0 — Fetch (not stored separately)")
    print("-" * 72)
    print(
        "Per-endpoint HTTP payloads are merged by the orchestrator into one dict.\n"
        "What you have in Bronze is **after ingest** (200 OK endpoints only)."
    )

    print("\n" + "=" * 72)
    print("STAGE 1 — Ingest output (same as Bronze JSON after decompress)")
    print("-" * 72)
    print(_truncate(json.dumps(bronze, indent=2, default=str), args.max_json))

    if "news" in args.source_type:
        n_pre = _news_article_count_ingest_shape(bronze)
        print(f"\n>>> Article-like dict count in ingest-shaped payload: {n_pre}")

    print("\n" + "=" * 72)
    print("STAGE 2 — Clean (dedup for news; merge lists for fundamentals)")
    print("-" * 72)
    clean_res = run_clean(bronze, args.source_type)
    if not clean_res.ok:
        print(f"FAILED: {clean_res.error}")
        raise SystemExit(1)
    cleaned = clean_res.data
    print(f"ok=True latency_ms={clean_res.latency_ms:.3f}")
    if isinstance(cleaned, list):
        print(f">>> After clean: {len(cleaned)} items (list)")
        if "news" in args.source_type and n_pre is not None:
            print(f">>> Removed by title+2h-window dedup (approx): {max(0, n_pre - len(cleaned))}")
        if args.article_index is not None:
            idx = args.article_index
            # pre-list: re-extract for display (same as count helper path)
            raw_pre = bronze
            if isinstance(raw_pre, dict) and len(raw_pre) == 1:
                v = next(iter(raw_pre.values()))
                if isinstance(v, dict):
                    raw_pre = v
            arts = raw_pre.get("articles") or raw_pre.get("results") or []
            if isinstance(arts, dict):
                arts = [arts]
            pre_list = [a for a in arts if isinstance(a, dict)]
            if 0 <= idx < len(pre_list):
                print(f"\n--- Article index {idx} BEFORE clean ---")
                print(_article_one_line(pre_list[idx]))
            if 0 <= idx < len(cleaned):
                print(f"\n--- Article index {idx} AFTER clean (if still present) ---")
                print(_article_one_line(cleaned[idx]))
            else:
                print(f"\n(Index {idx} not in cleaned list; len={len(cleaned)})")
        print(_truncate(json.dumps(cleaned, indent=2, default=str), args.max_json))
    else:
        print(f">>> After clean: dict with keys {list(cleaned.keys()) if isinstance(cleaned, dict) else type(cleaned)}")
        print(_truncate(json.dumps(cleaned, indent=2, default=str), args.max_json))

    print("\n" + "=" * 72)
    print("STAGE 3 — Transform → Markdown (Silver-shaped)")
    print("-" * 72)
    tf_res = run_transform(cleaned, args.source_type, args.ticker)
    if not tf_res.ok:
        print(f"FAILED: {tf_res.error}")
        raise SystemExit(1)
    md = tf_res.data
    print(f"ok=True latency_ms={tf_res.latency_ms:.3f}")
    print(_truncate(md, args.max_md))

    print("\n" + "=" * 72)
    print("STAGE 4 — align.py (optional, not run by smoke/orchestrator)")
    print("-" * 72)
    print(
        "``catalyst_data.pipeline.align`` maps ``published_utc`` → trade dates.\n"
        "It does **not** run inside ``process_request``. Import and call "
        "``map_to_trade_date`` when you build a news_alignment workflow."
    )
    print("=" * 72)


if __name__ == "__main__":
    main()
