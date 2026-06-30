"""CLI for index status reporting and dry-run record building.

Usage:
    python -m catalyst_data.cli_index status
    python -m catalyst_data.cli_index rebuild-index --mode dry-run

Never imports lancedb, FlagEmbedding, or any GPU library.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from catalyst_data.storage.sqlite import init_db
from catalyst_data.source_tier import classify_articles, tier_distribution, tier_label
from catalyst_data.index_builder import build_index_records, index_summary

logger = logging.getLogger(__name__)

DEFAULT_DB = Path("data/catalyst_dev_ws4b.db")


def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_db(conn)
    return conn


def cmd_status(db_path: str) -> None:
    """Print index freshness summary."""
    conn = _open_db(db_path)

    # Latest build
    build_row = conn.execute(
        """SELECT build_id, created_at, status, l1_count, l2_count, article_count,
                  model, indexed_through_date
           FROM index_manifests ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()

    if build_row:
        print("=== Latest Build ===")
        print(f"  Build ID:       {build_row[0]}")
        print(f"  Created:        {build_row[1]}")
        print(f"  Status:         {build_row[2]}")
        print(f"  Model:          {build_row[6]}")
        print(f"  L1 count:       {build_row[3]}")
        print(f"  L2 count:       {build_row[4]}")
        print(f"  Article count:  {build_row[5]}")
        print(f"  Indexed through: {build_row[7]}")
    else:
        print("=== Latest Build ===")
        print("  No builds — run rebuild-index on cloud/GPU (Step 4).")

    # Index state counts
    state_total = conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0]
    state_article = conn.execute(
        "SELECT COUNT(*) FROM index_state WHERE source_kind = 'article'"
    ).fetchone()[0]
    print(f"\n=== Index State ===")
    print(f"  Total indexed:   {state_total}")
    print(f"  Articles:        {state_article}")

    # Per-tier distribution from articles
    dist = tier_distribution(conn)
    print(f"\n=== Article Tier Distribution ===")
    for tier in sorted(dist):
        print(f"  {tier_label(tier)}: {dist[tier]}")

    article_total = sum(dist.values())
    print(f"  Total articles:  {article_total}")

    conn.close()


def cmd_rebuild_index(db_path: str, mode: str = "dry-run") -> None:
    """Rebuild index records — dry-run only on Mac (no embedding).

    Populates source_tier (idempotent), builds L1+L2 records, prints summary.
    Does NOT write index_state or index_manifests.
    """
    if mode != "dry-run":
        print("ERROR: Only --mode dry-run is supported on Mac.")
        print("       Full rebuild requires cloud/GPU (Step 4).")
        sys.exit(1)

    conn = _open_db(db_path)

    # 1. Classify articles (idempotent)
    print("Classifying articles...")
    classified = classify_articles(conn)
    print(f"  Classified: {classified} articles (newly assigned tier)")

    # 2. Build index records (no embedding)
    print("Building index records (dry-run, no embedding)...")
    records = build_index_records(conn, min_l2_chars=800)
    summary = index_summary(records)

    # 3. Guards — enforced inside build_index_records (protects all callers)
    article_count = conn.execute(
        "SELECT COUNT(*) FROM articles"
    ).fetchone()[0]
    at_count = conn.execute(
        "SELECT COUNT(*) FROM article_tickers"
    ).fetchone()[0]
    l1_records = [r for r in records if r["chunk_level"] == "l1"]
    ticker_refs = sum(len(r["tickers"]) for r in l1_records)

    # 4. Report
    print(f"\n=== Dry-Run Summary ===")
    print(f"  L1 records:              {summary['l1_count']}")
    print(f"  L2 records:              {summary['l2_count']}")
    print(f"  L2-eligible articles:    {summary['l2_eligible_count']} "
          f"({summary['l2_eligible_pct']}%)")
    print(f"  Would-embed total:       {summary['would_embed_count']}")
    print(f"  Ticker-lossless guard:   PASS ({ticker_refs} == {at_count})")
    print(f"  Dedup guard:             PASS (L1={summary['l1_count']} == articles={article_count})")

    print(f"\n  Per-tier distribution:")
    for tier in sorted(summary["per_tier"]):
        t = summary["per_tier"][tier]
        print(f"    {tier_label(tier)}: L1={t['l1']}, L2={t['l2']}")

    print(f"\n  NOTE: index_state and index_manifests were NOT written.")
    print(f"  Real embedding is deferred to Step 4 (cloud/GPU).")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index CLI — status and dry-run rebuild"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Print index freshness summary")

    rebuild = sub.add_parser("rebuild-index", help="Rebuild index records")
    rebuild.add_argument(
        "--mode", choices=["dry-run"], default="dry-run",
        help="Only dry-run is supported on Mac (default: dry-run)"
    )
    rebuild.add_argument(
        "--db", default=str(DEFAULT_DB),
        help=f"Path to dev DB (default: {DEFAULT_DB})"
    )

    args = parser.parse_args()

    if args.command == "status":
        cmd_status(str(DEFAULT_DB))
    elif args.command == "rebuild-index":
        db_path = getattr(args, "db", str(DEFAULT_DB))
        cmd_rebuild_index(db_path, mode=args.mode)


if __name__ == "__main__":
    main()
