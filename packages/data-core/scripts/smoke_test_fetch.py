#!/usr/bin/env python3
"""
V1.5 Real-World Smoke Test — Catalyst Data-Core

Fetches live data from ALL target sources for 5 tickers x 3 business days.
Stores results in scripts/smoke_test_assets.db with data_version=v1.1-smoke.
Exports debug artifacts (one sample per source) into scripts/debug_output/.
Outputs a summary audit report.

Usage:
    cd /Users/yiannischen/Desktop/Catalyst
    set -a; source packages/data-core/.env; set +a
    PYTHONPATH=packages/data-core .venv/bin/python packages/data-core/scripts/smoke_test_fetch.py

Required env vars: FMP_API_KEY, FRED_API_KEY, FINNHUB_API_KEY, SEC_USER_AGENT
GDELT: no key required.

Expected runtime: 10-18 minutes (due to GDELT 5.5s rate limit + SEC delays).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta, timezone

from data_core.artifacts import ArtifactWriter
from data_core.pipeline import run_clean
from data_core.models import CatalystDataRequest, DataAsset
from data_core.orchestrator import process_request
from data_core.sqlite_cache import init_db
from data_core.connector_types import FetchResult

from data_core.connectors.fmp import create_fmp_fetcher
from data_core.connectors.gdelt import create_gdelt_fetcher
from data_core.connectors.fred import create_fred_fetcher
from data_core.connectors.sec_edgar import create_sec_fetcher
from data_core.connectors.finnhub import create_finnhub_fetcher
from data_core.connectors.yfinance_fallback import create_yfinance_fetcher

import httpx

# === Configuration ===
TICKERS = ["NVDA", "AAPL", "JPM", "TSLA", "KO"]
SEC_TICKERS = ["NVDA", "AAPL"]
DATA_VERSION = "v1.1-smoke"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "smoke_test_assets.db")
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug_output")


def get_last_n_business_days(n: int = 3) -> list[str]:
    days = []
    d = datetime.now(timezone.utc).date()
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
    return days


def print_log(log: dict):
    cache_icon = "CACHE" if log.get("cache_hit") else "FETCH"
    status = log.get("http_status", "?")
    latency = log.get("latency_ms", 0)
    source = log.get("source", "?")
    retry = log.get("retry_count", 0)
    print(f"  [{cache_icon}] {source:25s} | status={status:>4} | {latency:>8.1f}ms | retries={retry}")


def export_artifacts(writer: ArtifactWriter, asset: DataAsset, source: str, date: str):
    """Write structured debug artifacts for a single asset."""
    try:
        raw = json.loads(asset.content_raw.decode("utf-8")) if asset.content_raw else {}
    except Exception:
        raw = {}
    writer.write_raw(source, asset.ticker, date, raw)

    clean_result = run_clean(raw, source)
    cleaned = clean_result.data if clean_result.ok else raw
    writer.write_cleaned(source, asset.ticker, date, cleaned)

    writer.write_final(source, asset.ticker, date, asset.content_clean)
    print(f"    -> Artifacts: {source}/{asset.ticker}/{date}/")


async def run_smoke_test():
    print("=" * 70)
    print("Catalyst Data-Core V1.5 — Real-World Smoke Test")
    print(f"data_version: {DATA_VERSION}")
    print("=" * 70)

    fmp_key = os.environ.get("FMP_API_KEY", "")
    fred_key = os.environ.get("FRED_API_KEY", "")
    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
    sec_ua = os.environ.get("SEC_USER_AGENT", "")

    missing = []
    if not fmp_key: missing.append("FMP_API_KEY")
    if not fred_key: missing.append("FRED_API_KEY")
    if not finnhub_key: missing.append("FINNHUB_API_KEY")
    if not sec_ua: missing.append("SEC_USER_AGENT")
    if missing:
        print(f"\nWARNING: Missing env vars: {', '.join(missing)}")
        print("  Connectors with missing keys will be skipped.\n")

    dates = get_last_n_business_days(3)
    print(f"Tickers: {TICKERS}")
    print(f"Dates: {dates}")
    print(f"DB: {DB_PATH}")
    print(f"Debug artifacts: {DEBUG_DIR}")
    print()

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    writer = ArtifactWriter(base_dir=DEBUG_DIR, run_id=run_id)
    print(f"Run ID: {run_id}")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    async with httpx.AsyncClient(timeout=45.0) as client:
        sem = asyncio.Semaphore(5)

        primary_fmp = create_fmp_fetcher(api_key=fmp_key, semaphore=sem, client=client) if fmp_key else None
        fallback_yf = create_yfinance_fetcher()
        gdelt_fetch = create_gdelt_fetcher(client=client)
        fred_fetch = create_fred_fetcher(api_key=fred_key, client=client) if fred_key else None
        sec_fetch = create_sec_fetcher(user_agent=sec_ua, client=client) if sec_ua else None
        finnhub_fetch = create_finnhub_fetcher(api_key=finnhub_key, client=client) if finnhub_key else None

        results_log: list[dict] = []
        all_logs: list[dict] = []
        artifact_count = 0

        def log_sink(log: dict):
            all_logs.append(log)
            print_log(log)

        def record_asset(asset: DataAsset, date: str, source: str):
            raw_len = len(asset.content_raw) if asset.content_raw else 0
            results_log.append({
                "ticker": asset.ticker,
                "date": date,
                "source": source,
                "content_clean_len": len(asset.content_clean),
                "content_raw_len": raw_len,
                "has_references": "## References" in asset.content_clean,
                "has_table": "|---|" in asset.content_clean,
                "http_status": asset.metadata.get("http_status", "?"),
            })

        # === Phase 1: Per-ticker FMP + Finnhub ===
        print("\n--- Phase 1: Per-Ticker Sources (FMP + Finnhub) ---")
        for ticker in TICKERS:
            for date in dates:
                sources = []
                if fmp_key:
                    sources.append("fmp_fundamentals")
                if finnhub_key:
                    sources.append("finnhub_news")
                if not sources:
                    continue

                print(f"\n[{ticker} / {date}] sources={sources}")

                async def dispatch_fetch(t, ep, d,
                                         _fmp=primary_fmp, _fh=finnhub_fetch):
                    if ep in ("income_statement", "balance_sheet", "cash_flow"):
                        if _fmp:
                            return await _fmp(t, ep, d)
                    if ep == "finnhub_news":
                        if _fh:
                            return await _fh(t, ep, d)
                    return FetchResult(status=0, error="No fetcher", source_label="none")

                request = CatalystDataRequest(ticker=ticker, date=date, sources=sources)
                assets = await process_request(
                    request=request, conn=conn,
                    primary_fetch_fn=dispatch_fetch,
                    fallback_fetch_fn=fallback_yf if fmp_key else None,
                    data_version=DATA_VERSION,
                    log_sink=log_sink,
                )
                if not assets:
                    for src in sources:
                        writer.write_error(src, ticker, date,
                                           stage="ingest", error="No assets returned")
                for a in assets:
                    record_asset(a, date, a.source_type)
                    export_artifacts(writer, a, a.source_type, date)
                    artifact_count += 1

                await asyncio.sleep(0.5)

        # === Phase 2: GDELT (2 tickers x 3 dates) ===
        print("\n--- Phase 2: GDELT News ---")
        for date in dates:
            for ticker in TICKERS[:2]:
                print(f"\n[GDELT / {ticker} / {date}]")
                request = CatalystDataRequest(ticker=ticker, date=date, sources=["gdelt_news"])
                assets = await process_request(
                    request=request, conn=conn,
                    primary_fetch_fn=gdelt_fetch,
                    data_version=DATA_VERSION,
                    log_sink=log_sink,
                )
                if not assets:
                    writer.write_error("gdelt_news", ticker, date,
                                       stage="ingest", error="No assets returned")
                for a in assets:
                    record_asset(a, date, "gdelt_news")
                    export_artifacts(writer, a, "gdelt_news", date)
                    artifact_count += 1

        # === Phase 3: FRED (once per date, ticker=MACRO) ===
        if fred_fetch:
            print("\n--- Phase 3: FRED Macro ---")
            for date in dates:
                print(f"\n[FRED / MACRO / {date}]")
                request = CatalystDataRequest(ticker="MACRO", date=date, sources=["fred_rates"])
                assets = await process_request(
                    request=request, conn=conn,
                    primary_fetch_fn=fred_fetch,
                    data_version=DATA_VERSION,
                    log_sink=log_sink,
                )
                if not assets:
                    writer.write_error("fred_rates", "MACRO", date,
                                       stage="ingest", error="No assets returned")
                for a in assets:
                    record_asset(a, date, "fred_rates")
                    export_artifacts(writer, a, "fred_rates", date)
                    artifact_count += 1

        # === Phase 4: SEC EDGAR (slow; only 2 tickers) ===
        if sec_fetch:
            print("\n--- Phase 4: SEC EDGAR (2 tickers) ---")
            for ticker in SEC_TICKERS:
                print(f"\n[SEC / {ticker}]")
                request = CatalystDataRequest(
                    ticker=ticker, date=dates[0], sources=["sec_filings"],
                )
                assets = await process_request(
                    request=request, conn=conn,
                    primary_fetch_fn=sec_fetch,
                    data_version=DATA_VERSION,
                    log_sink=log_sink,
                )
                if not assets:
                    writer.write_error("sec_filings", ticker, dates[0],
                                       stage="ingest", error="No assets returned")
                for a in assets:
                    record_asset(a, dates[0], "sec_filings")
                    export_artifacts(writer, a, "sec_filings", dates[0])
                    artifact_count += 1
                await asyncio.sleep(1.0)

    conn.close()

    # === AUDIT REPORT ===
    print("\n" + "=" * 70)
    print("AUDIT REPORT")
    print("=" * 70)

    source_stats: dict[str, dict] = {}
    for r in results_log:
        src = r["source"]
        if src not in source_stats:
            source_stats[src] = {
                "total": 0, "success": 0, "refs": 0, "tables": 0,
                "raw_bytes": 0, "clean_bytes": 0,
            }
        source_stats[src]["total"] += 1
        if r.get("http_status") in (200, "200", 207, "207"):
            source_stats[src]["success"] += 1
        if r.get("has_references"):
            source_stats[src]["refs"] += 1
        if r.get("has_table"):
            source_stats[src]["tables"] += 1
        source_stats[src]["raw_bytes"] += r.get("content_raw_len", 0)
        source_stats[src]["clean_bytes"] += r.get("content_clean_len", 0)

    print(f"\n{'Source':<20} {'Total':>6} {'OK':>4} {'Rate':>6} {'Refs':>5} {'Tables':>7} {'AvgClean':>10}")
    print("-" * 70)
    for src, s in sorted(source_stats.items()):
        rate = f"{s['success']/s['total']*100:.0f}%" if s["total"] > 0 else "N/A"
        avg_clean = s["clean_bytes"] // s["total"] if s["total"] > 0 else 0
        print(f"{src:<20} {s['total']:>6} {s['success']:>4} {rate:>6} {s['refs']:>5} {s['tables']:>7} {avg_clean:>10}")

    total_raw = sum(s["raw_bytes"] for s in source_stats.values())
    total_clean = sum(s["clean_bytes"] for s in source_stats.values())
    if total_raw > 0:
        ratio = total_clean / total_raw
        print(f"\nCompression: {total_raw:,} raw → {total_clean:,} clean ({ratio:.2%})")

    print(f"\nTotal fetch logs emitted: {len(all_logs)}")
    retries = sum(1 for l in all_logs if l.get("retry_count", 0) > 0)
    partial = sum(1 for l in all_logs if l.get("http_status") == 207)
    print(f"  Retries triggered: {retries}")
    print(f"  Partial success (207): {partial}")

    print(f"\nDB: {DB_PATH}")
    print(f"Total assets persisted: {len(results_log)}")

    print(f"\nDebug artifacts in: {DEBUG_DIR}/{run_id}/")
    print(f"  Total artifact sets written: {artifact_count}")
    print("  Structure: <source>/<ticker>/<date>/{01_raw, 02_cleaned, 03_final.md}")

    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
