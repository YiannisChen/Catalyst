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
# materialize-tiers
# ---------------------------------------------------------------------------

def cmd_materialize_tiers(db_path: str, force: bool = False) -> None:
    """Materialize source tiers for articles.

    Without --force: only articles WHERE source_tier IS NULL (idempotent).
    With --force: re-evaluates ALL articles, reports delta.
    """
    conn = _open_db(db_path)

    if force:
        from catalyst_data.source_tier import materialize_all_tiers, unknown_publisher_audit
        changed, unknowns = materialize_all_tiers(conn)
        print(f"Materialized tiers (--force): {changed} rows changed")
    else:
        from catalyst_data.source_tier import classify_articles, unknown_publisher_audit
        count = classify_articles(conn)
        print(f"Classified: {count} articles (newly assigned tier)")

    dist = tier_distribution(conn)
    print(f"\nTier Distribution:")
    for tier in sorted(dist):
        print(f"  {tier_label(tier)}: {dist[tier]}")
    print(f"  Total articles:  {sum(dist.values())}")

    audit = unknown_publisher_audit(conn)
    if audit["count"] > 0:
        print(f"\nUnknown publishers (assigned T4): {audit['count']}")
        for name in audit["unknown_publishers"]:
            print(f"  - {name}")

    conn.close()

# ---------------------------------------------------------------------------
# rebuild-index
# ---------------------------------------------------------------------------

def cmd_rebuild_index(db_path: str, mode: str = "dry-run") -> None:
    """Rebuild index records — dry-run only on Mac (no embedding).

    STRICTLY read-only: PRAGMA query_only=ON for entire connection lifetime.
    Tier materialization is NEVER done here — use materialize-tiers.
    """
    if mode != "dry-run":
        print("ERROR: Only --mode dry-run is supported on Mac.")
        print("       Full rebuild requires cloud/GPU (Step 4).")
        sys.exit(1)

    conn = _open_db(db_path)
    conn.execute("PRAGMA query_only = ON")

    # Check for unmaterialized tiers (warn only, no write)
    null_tier_count = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE source_tier IS NULL"
    ).fetchone()[0]
    if null_tier_count > 0:
        print(f"WARNING: {null_tier_count} articles have NULL source_tier.")
        print("         Run 'materialize-tiers' first for accurate per-tier stats.")
        print()

    print("Building index records (dry-run, no embedding)...")
    records = build_index_records(conn, min_l2_chars=800)
    summary = index_summary(records)

    article_count = conn.execute(
        "SELECT COUNT(*) FROM articles"
    ).fetchone()[0]
    at_count = conn.execute(
        "SELECT COUNT(*) FROM article_tickers"
    ).fetchone()[0]

    # Article-scoped guards (not filing-inclusive)
    article_l1 = summary["article_l1_count"]
    ticker_refs = sum(
        len(r["tickers"]) for r in records
        if r["chunk_level"] == "l1" and r.get("source_kind", "article") == "article"
    )
    ticker_ok = ticker_refs == at_count
    dedup_ok = article_l1 == article_count

    print(f"\n=== Dry-Run Summary ===")
    print(f"  L1 records:              {summary['l1_count']} (article={summary['article_l1_count']}, filing={summary['filing_l1_count']})")
    print(f"  L2 records:              {summary['l2_count']}")
    print(f"  L2-eligible items:       {summary['l2_eligible_count']} "
          f"({summary['l2_eligible_pct']}%)")
    print(f"  Would-embed total:       {summary['would_embed_count']}")
    ticker_label = "PASS" if ticker_ok else "FAIL"
    dedup_label = "PASS" if dedup_ok else "FAIL"
    print(f"  Article ticker guard:    {ticker_label} (records={ticker_refs} == DB={at_count})")
    print(f"  Article dedup guard:     {dedup_label} (L1={summary['article_l1_count']} == articles={article_count})")

    # Print per-tier distribution from the polymorphic summary
    per_tier = summary.get("per_tier", {})
    if per_tier:
        print(f"\n  Per-tier distribution:")
        for sk in sorted(per_tier):
            for tier in sorted(per_tier[sk]):
                t = per_tier[sk][tier]
                print(f"    {sk}/{tier_label(tier)}: L1={t['l1']}, L2={t['l2']}")

    # Distribution from DB (read-only)
    dist = tier_distribution(conn)
    print(f"\n  DB Tier Distribution:")
    for tier in sorted(dist):
        print(f"    {tier_label(tier)}: {dist[tier]}")
    print(f"  Total articles:  {sum(dist.values())}")

    print(f"\n  NOTE: index_state and index_manifests were NOT written.")
    print(f"  Real embedding is deferred to Step 4 (cloud/GPU).")

    if not ticker_ok or not dedup_ok:
        print(f"\nERROR: Guard failure — ticker_lossless={ticker_ok}, dedup={dedup_ok}")
        conn.close()
        sys.exit(1)

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
    live: bool = False,
    confirm: bool = False,
    resume_from: str | None = None,
    json_output: bool = False,
) -> None:
    """Run the update pipeline (dry-run default; --live --confirm for real)."""
    ticker_list = (
        [t.strip() for t in tickers.split(",") if t.strip()]
        if tickers else None
    )
    source_list = (
        [s.strip() for s in sources.split(",") if s.strip()]
        if sources else None
    )

    fetch_fn = None

    # --live path: gated runner
    if live:
        from catalyst_data.live_runner import run_live_guard, build_polygon_fetcher, build_finnhub_fetcher, build_sec_fetcher

        run_live_guard(db_path, required_keys=None, confirm=confirm)

        # Determine required keys based on --sources
        req_keys = []
        resolved_sources = source_list or ["polygon_news"]
        if "polygon_news" in resolved_sources:
            req_keys.append("POLYGON_API_KEY")
        if "finnhub_company_news" in resolved_sources:
            req_keys.append("FINNHUB_API_KEY")
        if "sec_filings" in resolved_sources:
            req_keys.append("SEC_USER_AGENT")

        if req_keys:
            from catalyst_data.live_runner import _check_env_key
            for k in req_keys:
                _check_env_key(k)

        # Build connectors
        fetch_fn = {}
        if "polygon_news" in resolved_sources:
            fn, _ = build_polygon_fetcher()
            fetch_fn["polygon_news"] = fn
        if "finnhub_company_news" in resolved_sources:
            fn, _ = build_finnhub_fetcher()
            fetch_fn["finnhub_company_news"] = fn
        if "sec_filings" in resolved_sources:
            ns, _ = build_sec_fetcher()
            fetch_fn["sec_filings"] = ns

        print(f"\n=== Live Run: update-news ===")
        print(f"  Sources: {resolved_sources}")
        print(f"  Tickers: {ticker_list or '(all 10)'}")

    elif not dry_run:
        print("ERROR: Only --dry-run is supported on Mac (Step 2).")
        print("       Use --live --confirm for real network calls.")
        sys.exit(1)

    _ensure_db(db_path)
    from catalyst_data.run_report import RunConfig
    from catalyst_data.update_pipeline import run_update
    cfg = RunConfig(
        db_path=db_path,
        tickers=ticker_list,
        sources=source_list,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        dry_run=not live,
        resume_from=resume_from,
        fetch_fn=fetch_fn,
    )
    report = run_update(cfg)

    if json_output:
        import json
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        if report.mode == "dry-run":
            print(f"\n=== Update-News Dry-Run ===")
        else:
            print(f"\n=== Update-News Report ===")
        print(f"  Run ID:         {report.run_id}")
        print(f"  Mode:           {report.mode}")
        print(f"  Status:         {report.config.get('dry_run') and 'dry-run' or 'live'}")
        print(f"  Report path:    {report.report_path}")
        for provider, stats in sorted(report.providers.items()):
            print(f"  {provider}: {stats}")
        if report.mode == "dry-run":
            print(f"\n  ZERO network calls made.  ZERO DB writes.")

    # Keep CLI process semantics outside service layer.
    total_failed = sum(v.get("cells_failed", 0) for v in report.providers.values())
    total_success = sum(v.get("cells_success", 0) + v.get("cells_success_empty", 0)
                        for v in report.providers.values())
    if total_failed and not total_success:
        sys.exit(1)


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(db_path: str, json_output: bool = False) -> None:
    """Run the full operator contract gate (all four audit dimensions).

    Exits 0 if all gates pass, non-zero otherwise.
    """
    from catalyst_data.doctor import doctor as run_doctor

    result = run_doctor(db_path, json_output=json_output)
    sys.exit(result["exit_code"])

# ---------------------------------------------------------------------------
# reconcile-schema
# ---------------------------------------------------------------------------

def cmd_reconcile_schema(db_path: str, dry_run: bool = True) -> None:
    """Reconcile dev DB schema with code-derived DDL.

    --dry-run: builds a fresh temp DB from init_db, diffs sqlite_master.
    --apply:   creates missing tables/columns additively.
    """
    import os as _os
    resolved = _os.path.realpath(db_path)

    # Refuse frozen DB
    frozen_real = str(
        (Path(__file__).resolve().parent.parent.parent.parent
         / "data" / "catalyst_eval_frozen_v2.db")
    )
    if resolved == _os.path.realpath(frozen_real):
        print("ERROR: Refusing to modify frozen eval DB.", file=sys.stderr)
        print(f"  Frozen DB: {frozen_real}", file=sys.stderr)
        sys.exit(1)

    # Build code-derived schema from fresh temp DB
    temp_conn = sqlite3.connect(":memory:")
    try:
        init_db(temp_conn)
        temp_conn.commit()

        # Extract schema from temp DB
        temp_tables = {
            r[0]: r[1]
            for r in temp_conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        temp_indexes = {
            r[0]
            for r in temp_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        temp_cols: dict[str, set[str]] = {}
        for table_name in temp_tables:
            cols = temp_conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            temp_cols[table_name] = {c[1] for c in cols}
    finally:
        temp_conn.close()

    # Open dev DB
    if not _os.path.exists(resolved):
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(resolved)
    try:
        dev_tables = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        dev_indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        dev_cols: dict[str, set[str]] = {}
        for table_name in dev_tables:
            try:
                cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                dev_cols[table_name] = {c[1] for c in cols}
            except sqlite3.OperationalError:
                dev_cols[table_name] = set()

        # Diff
        missing_tables = set(temp_tables) - set(dev_tables)
        missing_indexes = temp_indexes - dev_indexes
        missing_columns: dict[str, set[str]] = {}
        for table_name in temp_cols:
            if table_name in dev_cols:
                mc = temp_cols[table_name] - dev_cols[table_name]
                if mc:
                    missing_columns[table_name] = mc

        total_missing = len(missing_tables) + len(missing_indexes) + sum(
            len(v) for v in missing_columns.values()
        )

        if total_missing == 0:
            print("Schema is current — 0 missing objects.")
            if not dry_run:
                print("Already reconciled, no changes needed.")
            conn.close()
            return

        print(f"Schema diff: {total_missing} missing objects")
        for t in sorted(missing_tables):
            print(f"  MISSING TABLE:  {t}")
        for t, cols in sorted(missing_columns.items()):
            for c in sorted(cols):
                print(f"  MISSING COLUMN: {t}.{c}")
        for idx in sorted(missing_indexes):
            print(f"  MISSING INDEX:  {idx}")

        if dry_run:
            print("\nDry-run complete — no writes. Use --apply --db <PATH> to reconcile.")
            conn.close()
            return

        # --apply: add missing objects
        print("\nApplying additive migration...")
        from catalyst_data.storage.sqlite import ensure_macro_tables, _migrate_article_tickers_dedup

        # Create missing tables via ensure functions
        if "macro_observations" in missing_tables:
            ensure_macro_tables(conn)
            print("  Created macro_observations table + indexes")

        # Add missing columns
        if "article_tickers" in missing_columns:
            if "dedup_group_id" in missing_columns["article_tickers"]:
                _migrate_article_tickers_dedup(conn)
                print("  Added article_tickers.dedup_group_id column")

        # Re-diff to confirm
        new_dev_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        still_missing_tables = set(temp_tables) - new_dev_tables
        if still_missing_tables:
            print(f"WARNING: Still missing tables: {still_missing_tables}")

        print("Reconciliation complete.")
    finally:
        conn.close()

def cmd_update_macro(
    db_path: str,
    series: str | None,
    from_date: str | None,
    to_date: str | None,
    dry_run: bool = False,
    live: bool = False,
    confirm: bool = False,
) -> None:
    """Run FRED macro update — fetch, archive, normalize."""
    _ensure_db(db_path)

    series_list = None
    if series:
        series_list = [s.strip() for s in series.split(",") if s.strip()]

    # --live path: gated runner
    if live:
        from catalyst_data.live_runner import run_live_guard, build_fred_fetcher

        run_live_guard(db_path, required_keys=["FRED_API_KEY"], confirm=confirm)
        fetch_fn, _ = build_fred_fetcher()

        from catalyst_data.pipeline.fred_manifest import FETCHED_SERIES, CURATED_SERIES
        from catalyst_data.storage.sqlite import compute_asset_id, upsert_raw_asset
        from catalyst_data.pipeline.fred_normalize import rederive_fred_macro
        from datetime import datetime, timezone
        import asyncio, json

        resolved = series_list or FETCHED_SERIES
        derived = [s.series_id for s in CURATED_SERIES if s.derived]

        print(f"\n=== Live Run: update-macro ===")
        print(f"  Fetch series:   {resolved}")
        print(f"  Derived series: {derived}")

        async def _run_fred_live():
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            conn = sqlite3.connect(db_path)
            init_db(conn)
            ensure_ingestion_quality_tables(conn)

            fetched = 0
            failed = 0
            for series_id in resolved:
                try:
                    result = await fetch_fn(
                        series_id, "observations", None,
                        output_type=4,
                        realtime_start="1776-07-04",
                        realtime_end="9999-12-31",
                    )
                except Exception as exc:
                    logger.warning("FRED fetch failed for %s: %s", series_id, exc)
                    failed += 1
                    continue

                if result.status != 200:
                    logger.warning(
                        "FRED fetch for %s returned HTTP %s",
                        series_id, result.status,
                    )
                    failed += 1
                    continue

                raw_data = result.data if isinstance(result.data, dict) else {}
                if not raw_data.get("observations"):
                    logger.info("FRED %s: no observations", series_id)
                    fetched += 1
                    continue

                raw_bytes = json.dumps(raw_data, ensure_ascii=True).encode("utf-8")
                asset_id = compute_asset_id(series_id, today, "fred_macro")
                upsert_raw_asset(
                    conn,
                    asset_id=asset_id,
                    ticker="__macro__",
                    source_type="fred_macro",
                    reference_date=today,
                    content_raw=raw_bytes,
                    http_status=result.status,
                    metadata={
                        "series_id": series_id,
                        "endpoint": "observations",
                        "output_type": 4,
                    },
                )
                fetched += 1

            conn.commit()
            conn.close()

            # Re-derive all fred_macro raw_assets → macro_observations
            rederive_counts = rederive_fred_macro(db_path)
            return fetched, failed, rederive_counts

        fetched, failed, rederive_counts = asyncio.run(_run_fred_live())

        print(f"\n  Series fetched:  {fetched}")
        print(f"  Series failed:   {failed}")
        print(f"  Observations:    {rederive_counts.get('observations_upserted', 0)}")
        if derived:
            print(f"  Derived:         {', '.join(derived)}")
        return

    if not dry_run:
        print("ERROR: Only --dry-run is supported on Mac (Step 3E).")
        print("       Use --live --confirm for real network calls.")
        sys.exit(1)

    from catalyst_data.pipeline.fred_manifest import FETCHED_SERIES, CURATED_SERIES

    resolved = series_list or FETCHED_SERIES
    derived = [s.series_id for s in CURATED_SERIES if s.derived]

    print(f"\n=== Update-Macro Dry-Run ===")
    print(f"  Mode:           dry-run")
    print(f"  Date window:    {from_date or '(auto: 30d from today)'} → {to_date or '(today)'}")
    print(f"  Fetch series:   {resolved}")
    print(f"  Derived series: {derived}")
    print(f"  Total requests: {len(resolved)} (one per series)")
    print(f"\n  ZERO network calls made.  ZERO DB writes.")

def cmd_backfill(
    db_path: str,
    from_date: str,
    to_date: str,
    tickers: str | None,
    sources: str | None,
    chunk_days: int,
    dry_run: bool = False,
    live: bool = False,
    confirm: bool = False,
) -> None:
    """Run the backfill pipeline (dry-run default; --live --confirm for real)."""
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

    # --live path: gated runner
    if live:
        from catalyst_data.live_runner import run_live_guard, build_polygon_fetcher, build_finnhub_fetcher

        run_live_guard(db_path, required_keys=None, confirm=confirm)

        resolved_sources = source_list or ["polygon_news"]
        req_keys = []
        if "polygon_news" in resolved_sources:
            req_keys.append("POLYGON_API_KEY")
        if "finnhub_company_news" in resolved_sources:
            req_keys.append("FINNHUB_API_KEY")
        if req_keys:
            from catalyst_data.live_runner import _check_env_key
            for k in req_keys:
                _check_env_key(k)

        fetch_fn = {}
        if "polygon_news" in resolved_sources:
            fn, _ = build_polygon_fetcher()
            fetch_fn["polygon_news"] = fn
        if "finnhub_company_news" in resolved_sources:
            fn, _ = build_finnhub_fetcher()
            fetch_fn["finnhub_company_news"] = fn

        print(f"\n=== Live Run: backfill ===")
        print(f"  Sources: {resolved_sources}")
        print(f"  Tickers: {ticker_list or '(all 10)'}")
        print(f"  Window:  {from_date} -> {to_date}")

        _ensure_db(db_path)

        async def _run_live_backfill():
            from catalyst_data.backfill_pipeline import run_backfill
            return await run_backfill(
                db_path, tickers=ticker_list, sources=source_list,
                from_date=from_date, to_date=to_date, fetch_fn=fetch_fn,
                chunk_days=chunk_days, dry_run=False,
            )

        results = asyncio.run(_run_live_backfill())
        total = sum(r.get("cells_total", 0) for r in results)
        print(f"\n  Chunks: {len(results)}, Total cells: {total}")
        return

    if not dry_run:
        print("ERROR: Only --dry-run is supported on Mac (Step 2).")
        print("       Use --live --confirm for real network calls.")
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

    # materialize-tiers
    mat_p = sub.add_parser("materialize-tiers", help="Classify and populate source_tier")
    mat_p.add_argument("--force", action="store_true", default=False,
                       help="Re-evaluate ALL articles, not just NULL source_tier")
    mat_p.add_argument("--db", default=str(DEFAULT_DB),
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
    update_p.add_argument("--live", action="store_true", default=False,
                          help="Use real connectors + network (requires --confirm)")
    update_p.add_argument("--confirm", action="store_true", default=False,
                          help="Confirm live network execution")
    update_p.add_argument("--resume", dest="resume_from", default=None,
                          help="Resume failed/skipped cells from RUN_ID")
    update_p.add_argument("--json", dest="json_output", action="store_true", default=False,
                          help="Print RunReport JSON")
    update_p.add_argument("--db", default=str(DEFAULT_DB),
                          help=f"Path to dev DB (default: {DEFAULT_DB})")


    # doctor
    doctor_p = sub.add_parser("doctor", help="Run all operator-contract audit gates")
    doctor_p.add_argument("--json", dest="doctor_json", action="store_true", default=False,
                          help="Output JSON to stdout")
    doctor_p.add_argument("--db", dest="doctor_db", default=str(DEFAULT_DB),
                          help=f"Path to dev DB (default: {DEFAULT_DB})")

    # reconcile-schema
    rec_p = sub.add_parser("reconcile-schema", help="Reconcile dev DB schema with code-derived DDL")
    rec_p.add_argument("--dry-run", dest="reconcile_dry_run", action="store_true", default=True,
                       help="Diff only, zero writes (default)")
    rec_p.add_argument("--apply", dest="reconcile_apply", action="store_true", default=False,
                       help="Apply additive migration (requires --db)")
    rec_p.add_argument("--db", dest="reconcile_db", default=None,
                       help="Path to DB to reconcile")
    # refresh-cik-map
    cik_p = sub.add_parser("refresh-cik-map", help="Refresh CIK ticker map from SEC")
    cik_p.add_argument("--output", default=None,
                       help="Output CSV path (default: data/cik_map/cik_ticker_map.csv)")

    # backfill

    # update-macro
    macro_p = sub.add_parser("update-macro", help="Fetch FRED macro observations")
    macro_p.add_argument("--series", default=None,
                         help="Comma-separated series IDs (default: all 12 curated)")
    macro_p.add_argument("--from", dest="from_date", default=None,
                         help="Observation start YYYY-MM-DD (default: 30d ago)")
    macro_p.add_argument("--to", dest="to_date", default=None,
                         help="Observation end YYYY-MM-DD (default: today)")
    macro_p.add_argument("--dry-run", action="store_true", default=True,
                         help="Compute-only, zero network, zero DB writes")
    macro_p.add_argument("--live", action="store_true", default=False,
                         help="Use real connectors + network (requires --confirm)")
    macro_p.add_argument("--confirm", action="store_true", default=False,
                         help="Confirm live network execution")
    macro_p.add_argument("--db", default=str(DEFAULT_DB),
                         help=f"Path to dev DB (default: {DEFAULT_DB})")

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
    backfill_p.add_argument("--live", action="store_true", default=False,
                            help="Use real connectors + network (requires --confirm)")
    backfill_p.add_argument("--confirm", action="store_true", default=False,
                            help="Confirm live network execution")
    backfill_p.add_argument("--db", default=str(DEFAULT_DB),
                            help=f"Path to dev DB (default: {DEFAULT_DB})")

    args = parser.parse_args()

    if args.command == "status":
        db_path = getattr(args, "db", str(DEFAULT_DB))
        cmd_status(db_path, freshness=args.freshness)
    elif args.command == "materialize-tiers":
        db_path = getattr(args, "db", str(DEFAULT_DB))
        cmd_materialize_tiers(db_path, force=getattr(args, "force", False))
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
            live=getattr(args, "live", False),
            confirm=getattr(args, "confirm", False),
            resume_from=getattr(args, "resume_from", None),
            json_output=getattr(args, "json_output", False),
        )
    elif args.command == "doctor":
        db_arg = getattr(args, "doctor_db", str(DEFAULT_DB))
        cmd_doctor(db_arg, json_output=getattr(args, "doctor_json", False))

    elif args.command == "refresh-cik-map":
        result = refresh_cik_map(getattr(args, "output", None))
        print(f"CIK map refreshed: {len(result)} tickers written")
        for t, c in sorted(result.items()):
            print(f"  {t}: {c}")

    elif args.command == "reconcile-schema":
        db_arg = getattr(args, "reconcile_db", None)
        if getattr(args, "reconcile_apply", False):
            if not db_arg:
                print("ERROR: --apply requires --db <PATH>", file=sys.stderr)
                sys.exit(1)
            cmd_reconcile_schema(db_arg, dry_run=False)
        else:
            resolved_db = db_arg or str(DEFAULT_DB)
            cmd_reconcile_schema(resolved_db, dry_run=True)

    elif args.command == "update-macro":
        db_path = getattr(args, "db", str(DEFAULT_DB))
        cmd_update_macro(
            db_path,
            series=getattr(args, "series", None),
            from_date=getattr(args, "from_date", None),
            to_date=getattr(args, "to_date", None),
            dry_run=getattr(args, "dry_run", False),
            live=getattr(args, "live", False),
            confirm=getattr(args, "confirm", False),
        )
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
            live=getattr(args, "live", False),
            confirm=getattr(args, "confirm", False),
        )


if __name__ == "__main__":
    main()
