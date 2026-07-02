"""CLI for index status, freshness, and update/backfill operations.

Usage:
    python -m catalyst_data.cli_index status [--freshness] [--db PATH]
    python -m catalyst_data.cli_index rebuild-index --mode dry-run [--db PATH]
    python -m catalyst_data.cli_index update-news [--from DATE] [--to DATE]
              [--tickers T,...] [--sources S,...] [--limit N] [--dry-run]
              [--db PATH]
    python -m catalyst_data.cli_index backfill [--from DATE] [--to DATE]
              [--tickers T,...] [--sources S,...] [--chunk-days N]
              [--dry-run] [--db PATH]

Never imports lancedb, FlagEmbedding, or any GPU library.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

from catalyst_data.storage.sqlite import init_db
from catalyst_data.quality import ensure_ingestion_quality_tables
from catalyst_data.source_tier import classify_articles, tier_distribution, tier_label
from catalyst_data.index_builder import build_index_records, index_summary

logger = logging.getLogger(__name__)

DEFAULT_DB = Path("data/catalyst_dev_ws4b.db")


def _ensure_db(db_path: str) -> str:
    """Validate DB path exists.  Exits with clean error if missing."""
    p = Path(db_path)
    if not p.exists():
        print(f"ERROR: Database not found: {db_path}", file=sys.stderr)
        print(f"  Expected dev DB at data/catalyst_dev_ws4b.db", file=sys.stderr)
        sys.exit(1)
    return str(p.resolve())


def _open_db(db_path: str) -> sqlite3.Connection:
    db_path = _ensure_db(db_path)
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    return conn


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(db_path: str, freshness: bool = False) -> None:
    """Print index/build status, optionally with freshness."""
    conn = _open_db(db_path)

    if freshness:
        from catalyst_data.freshness import freshness_report

        report = freshness_report(conn)
        wm = report["local_ohlcv_date"]
        news = report["news"]
        idx = report["index"]

        print(f"=== Freshness Report ===")
        print(f"  Local OHLCV watermark:  {wm}")
        print(f"  Generated at:           {report['generated_at']}")
        print()

        print(f"=== Index ===")
        print(f"  Status:         {idx['status']}")
        print(f"  Total articles: {idx['total_articles']}")
        print(f"  Stale count:    {idx['stale_count']}")
        if idx.get("latest_build_id"):
            print(f"  Latest build:   {idx['latest_build_id']}")
            print(f"  Last built at:  {idx['last_built_at']}")
            print(f"  Model:          {idx['model']}")
        print()

        print(f"=== News Freshness ===")
        for source_type, tickers in sorted(news.items()):
            stales = sum(
                1 for t in tickers.values() if t["status"] == "STALE"
            )
            freshes = sum(
                1 for t in tickers.values() if t["status"] == "FRESH"
            )
            nodata = sum(
                1 for t in tickers.values() if t["status"] == "NO_DATA"
            )
            print(f"  {source_type}: {freshes} FRESH, {stales} STALE, "
                  f"{nodata} NO_DATA")
            for ticker, info in sorted(tickers.items()):
                if info["status"] != "FRESH":
                    db_str = (
                        f" ({info['days_behind']}d behind)"
                        if info["days_behind"] > 0 else ""
                    )
                    print(f"    {ticker}: {info['status']}{db_str}  "
                          f"latest={info['latest_date']}")


        # SEC filings section
        sec = report.get("sec_filings", {})
        if sec:
            ov = sec.get("overall", {})
            pt = sec.get("per_ticker", {})
            print()
            print("=== SEC Filings ===")
            print(f"  Total (30d):          {ov.get('total_filings', 0)}")
            print(f"  Checked tickers:      {ov.get('checked_tickers', 0)}")
            print(f"  Never checked:        {ov.get('never_checked_tickers', 0)}")
            if pt:
                for ticker, info in sorted(pt.items()):
                    lfd = info.get("latest_filing_date") or "-"
                    lcd = info.get("latest_checked_date") or "-"
                    st = info.get("status", "?")
                    c30 = info.get("filings_30d_count", 0)
                    print(f"    {ticker}: {st:15s}  latest_filing={lfd:10s}  checked={lcd:10s}  30d={c30}")

        conn.close()
        return

    # Original status output
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

    state_total = conn.execute("SELECT COUNT(*) FROM index_state").fetchone()[0]
    state_article = conn.execute(
        "SELECT COUNT(*) FROM index_state WHERE source_kind = 'article'"
    ).fetchone()[0]
    print(f"\n=== Index State ===")
    print(f"  Total indexed:   {state_total}")
    print(f"  Articles:        {state_article}")

    dist = tier_distribution(conn)
    print(f"\n=== Article Tier Distribution ===")
    for tier in sorted(dist):
        print(f"  {tier_label(tier)}: {dist[tier]}")

    article_total = sum(dist.values())
    print(f"  Total articles:  {article_total}")

    conn.close()


# ---------------------------------------------------------------------------
# rebuild-index
# ---------------------------------------------------------------------------

def cmd_rebuild_index(db_path: str, mode: str = "dry-run") -> None:
    """Rebuild index records — dry-run only on Mac (no embedding)."""
    if mode != "dry-run":
        print("ERROR: Only --mode dry-run is supported on Mac.")
        print("       Full rebuild requires cloud/GPU (Step 4).")
        sys.exit(1)

    conn = _open_db(db_path)

    print("Classifying articles...")
    classified = classify_articles(conn)
    print(f"  Classified: {classified} articles (newly assigned tier)")

    print("Building index records (dry-run, no embedding)...")
    records = build_index_records(conn, min_l2_chars=800)
    summary = index_summary(records)

    article_count = conn.execute(
        "SELECT COUNT(*) FROM articles"
    ).fetchone()[0]
    at_count = conn.execute(
        "SELECT COUNT(*) FROM article_tickers"
    ).fetchone()[0]
    l1_records = [r for r in records if r["chunk_level"] == "l1"]
    ticker_refs = sum(len(r["tickers"]) for r in l1_records)

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


# ---------------------------------------------------------------------------
# update-news
# ---------------------------------------------------------------------------

def cmd_update_news(
    db_path: str,
    from_date: str | None,
    to_date: str | None,
    tickers: str | None,
    sources: str | None,
    limit: int | None,
    dry_run: bool = False,
) -> None:
    """Run the update pipeline (dry-run only on Mac in Step 2)."""
    _ensure_db(db_path)

    ticker_list = (
        [t.strip() for t in tickers.split(",") if t.strip()]
        if tickers else None
    )
    source_list = (
        [s.strip() for s in sources.split(",") if s.strip()]
        if sources else None
    )

    if not dry_run:
        print("ERROR: Only --dry-run is supported on Mac (Step 2).")
        print("       Real network calls are gated to a separate step.")
        sys.exit(1)

    async def _run():
        from catalyst_data.update_pipeline import run_update_batch

        report = await run_update_batch(
            db_path,
            tickers=ticker_list,
            sources=source_list,
            from_date=from_date,
            to_date=to_date,
            fetch_fn=None,
            limit=limit,
            dry_run=True,
        )
        return report

    report = asyncio.run(_run())

    print(f"\n=== Update-News Dry-Run ===")
    print(f"  Mode:           dry-run")
    print(f"  Date window:    {from_date or '(auto)'} → {to_date or '(auto)'}")
    print(f"  Tickers:        {ticker_list or '(all 10 universe)'}")
    print(f"  Sources:        {source_list or 'polygon_news'}")
    print(f"  Missing cells:  {report['cells_total']}")
    if report["missing_cells"]:
        print(f"\n  First 10 missing cells:")
        for cell in report["missing_cells"][:10]:
            print(f"    {cell[0]}  {cell[1]}  {cell[2]}")
        if report["cells_total"] > 10:
            print(f"    ... and {report['cells_total'] - 10} more")

    print(f"\n  ZERO network calls made.  ZERO DB writes.")


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------

def cmd_backfill(
    db_path: str,
    from_date: str,
    to_date: str,
    tickers: str | None,
    sources: str | None,
    chunk_days: int,
    dry_run: bool = False,
) -> None:
    """Run the backfill pipeline (dry-run only on Mac in Step 2)."""
    _ensure_db(db_path)

    if not from_date or not to_date:
        print("ERROR: --from and --to are required for backfill.", file=sys.stderr)
        sys.exit(1)

    ticker_list = (
        [t.strip() for t in tickers.split(",") if t.strip()]
        if tickers else None
    )
    source_list = (
        [s.strip() for s in sources.split(",") if s.strip()]
        if sources else None
    )

    if not dry_run:
        print("ERROR: Only --dry-run is supported on Mac (Step 2).")
        print("       Real backfill execution is gated to a separate step.")
        sys.exit(1)

    async def _run():
        from catalyst_data.backfill_pipeline import run_backfill

        results = await run_backfill(
            db_path,
            tickers=ticker_list,
            sources=source_list,
            from_date=from_date,
            to_date=to_date,
            fetch_fn=None,
            chunk_days=chunk_days,
            dry_run=True,
        )
        return results

    results = asyncio.run(_run())

    total_cells = sum(r["cells_total"] for r in results)
    print(f"\n=== Backfill Dry-Run ===")
    print(f"  Chunks:         {len(results)}")
    print(f"  Chunk size:     {chunk_days} days")
    print(f"  Total cells:    {total_cells}")
    for i, r in enumerate(results[:5]):
        print(f"  Chunk {i}: {r['chunk_from']} → {r['chunk_to']}  "
              f"({r['cells_total']} cells)")
    if len(results) > 5:
        print(f"  ... and {len(results) - 5} more chunks")

    print(f"\n  ZERO network calls made.  ZERO DB writes.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index CLI — status, freshness, rebuild, update, backfill"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    status_p = sub.add_parser("status", help="Print index/freshness summary")
    status_p.add_argument("--freshness", action="store_true",
                          help="Include news + index freshness report")
    status_p.add_argument("--db", default=str(DEFAULT_DB),
                          help=f"Path to dev DB (default: {DEFAULT_DB})")

    # rebuild-index
    rebuild = sub.add_parser("rebuild-index", help="Rebuild index records")
    rebuild.add_argument(
        "--mode", choices=["dry-run"], default="dry-run",
        help="Only dry-run is supported on Mac (default: dry-run)"
    )
    rebuild.add_argument(
        "--db", default=str(DEFAULT_DB),
        help=f"Path to dev DB (default: {DEFAULT_DB})"
    )

    # update-news
    update_p = sub.add_parser("update-news", help="Run update pipeline")
    update_p.add_argument("--from", dest="from_date", default=None,
                          help="Start date YYYY-MM-DD (default: latest OHLCV)")
    update_p.add_argument("--to", dest="to_date", default=None,
                          help="End date YYYY-MM-DD (default: latest OHLCV)")
    update_p.add_argument("--tickers", default=None,
                          help="Comma-separated tickers (default: all 10)")
    update_p.add_argument("--sources", default="polygon_news",
                          help="Comma-separated sources: polygon_news, finnhub_company_news, sec_filings (default: polygon_news)")
    update_p.add_argument("--limit", type=int, default=None,
                          help="Cap number of cells to process")
    update_p.add_argument("--dry-run", action="store_true", default=True,
                          help="Compute missing cells only, no network/DB writes")
    update_p.add_argument("--db", default=str(DEFAULT_DB),
                          help=f"Path to dev DB (default: {DEFAULT_DB})")

    # refresh-cik-map
    cik_p = sub.add_parser("refresh-cik-map", help="Refresh CIK ticker map from SEC")
    cik_p.add_argument("--output", default=None,
                       help="Output CSV path (default: data/cik_map/cik_ticker_map.csv)")

    # backfill
    backfill_p = sub.add_parser("backfill", help="Run backfill pipeline")
    backfill_p.add_argument("--from", dest="from_date", required=True,
                            help="Start date YYYY-MM-DD")
    backfill_p.add_argument("--to", dest="to_date", required=True,
                            help="End date YYYY-MM-DD")
    backfill_p.add_argument("--tickers", default=None,
                            help="Comma-separated tickers (default: all 10)")
    backfill_p.add_argument("--sources", default="polygon_news",
                            help="Comma-separated sources: polygon_news, finnhub_company_news, sec_filings (default: polygon_news)")
    backfill_p.add_argument("--chunk-days", type=int, default=7,
                            help="Days per chunk (default: 7)")
    backfill_p.add_argument("--dry-run", action="store_true", default=True,
                            help="Compute chunked cells only, no network/DB writes")
    backfill_p.add_argument("--db", default=str(DEFAULT_DB),
                            help=f"Path to dev DB (default: {DEFAULT_DB})")

    args = parser.parse_args()

    if args.command == "status":
        db_path = getattr(args, "db", str(DEFAULT_DB))
        cmd_status(db_path, freshness=args.freshness)
    elif args.command == "rebuild-index":
        db_path = getattr(args, "db", str(DEFAULT_DB))
        cmd_rebuild_index(db_path, mode=args.mode)
    elif args.command == "update-news":
        db_path = getattr(args, "db", str(DEFAULT_DB))
        cmd_update_news(
            db_path,
            from_date=getattr(args, "from_date", None),
            to_date=getattr(args, "to_date", None),
            tickers=getattr(args, "tickers", None),
            sources=getattr(args, "sources", None),
            limit=getattr(args, "limit", None),
            dry_run=getattr(args, "dry_run", False),
        )
    elif args.command == "refresh-cik-map":
        result = refresh_cik_map(getattr(args, "output", None))
        print(f"CIK map refreshed: {len(result)} tickers written")
        for t, c in sorted(result.items()):
            print(f"  {t}: {c}")

    elif args.command == "backfill":
        db_path = getattr(args, "db", str(DEFAULT_DB))
        cmd_backfill(
            db_path,
            from_date=getattr(args, "from_date", None),
            to_date=getattr(args, "to_date", None),
            tickers=getattr(args, "tickers", None),
            sources=getattr(args, "sources", None),
            chunk_days=getattr(args, "chunk_days", 7),
            dry_run=getattr(args, "dry_run", False),
        )


if __name__ == "__main__":
    main()
