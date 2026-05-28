#!/usr/bin/env python3
"""Smoke test: fetch N days of data for one ticker, validate full pipeline.

Usage:
    python -m scripts.smoke_test --ticker AAPL --days 3

Loads API keys from .env, creates connectors for available providers,
runs process_request() for each recent trading day, and prints a summary
of stored Bronze/Silver assets.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup so `catalyst_data` is importable when run from the repo root
# ---------------------------------------------------------------------------
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PACKAGE_ROOT))

from catalyst_data.config import RATE_POLICIES, provider_api_key
from catalyst_data.connectors.base import FetchResult
from catalyst_data.orchestrator import process_request
from catalyst_data.rate_limiter import TokenBucketLimiter
from catalyst_data.storage.sqlite import init_db

logger = logging.getLogger("smoke_test")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_dotenv(env_path: Path) -> None:
    """Minimal .env loader -- avoids adding python-dotenv as a dependency."""
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_available_sources() -> list[str]:
    """Determine which logical sources have API keys configured."""
    sources: list[str] = []
    if provider_api_key("polygon"):
        sources.extend(["polygon_news", "polygon_ohlcv"])
    if provider_api_key("fmp"):
        sources.append("fmp_fundamentals")
    if provider_api_key("fred"):
        sources.append("fred_macro")
    if not sources:
        print("WARNING: No API keys found. Set POLYGON_API_KEY, FMP_API_KEY, or FRED_API_KEY.")
        print("Falling back to yfinance_fundamentals only.")
        sources.append("yfinance_fundamentals")
    return sources


def resolve_sources(
    requested_sources: str | None, available_sources: list[str]
) -> list[str]:
    """Resolve a comma-separated source selection against available sources."""
    if not requested_sources:
        return available_sources

    requested = [source.strip() for source in requested_sources.split(",") if source.strip()]
    known_sources = {
        "polygon_news",
        "polygon_ohlcv",
        "fmp_fundamentals",
        "fred_macro",
        "yfinance_fundamentals",
    }
    for source in requested:
        if source not in known_sources:
            raise ValueError(f"Unknown logical source: {source}")
        if source not in available_sources:
            raise ValueError(
                f"Requested source is not available in this environment: {source}"
            )
    return requested


def get_recent_trading_days(days: int) -> list[str]:
    """Return the last *days* weekdays (approximate trading days), oldest first."""
    result: list[str] = []
    d = datetime.now().date()
    while len(result) < days:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # Mon--Fri
            result.append(d.strftime("%Y-%m-%d"))
    return list(reversed(result))


# ---------------------------------------------------------------------------
# Fetch function factory
# ---------------------------------------------------------------------------


async def _build_fetch_fn():
    """Create a unified fetch function that routes to the right connector.

    Returns (fetch_fn, httpx_client) -- caller is responsible for closing the
    client when done.
    """
    import httpx

    policies = RATE_POLICIES.get("dev", {})
    limiters = {name: TokenBucketLimiter(policy) for name, policy in policies.items()}
    client = httpx.AsyncClient(timeout=30.0)

    # -- Polygon --
    _polygon_fetch = None
    polygon_key = provider_api_key("polygon")
    if polygon_key:
        from catalyst_data.connectors.polygon import create_polygon_fetcher

        _polygon_fetch = create_polygon_fetcher(
            api_key=polygon_key,
            limiter=limiters.get("polygon"),
            client=client,
        )

    # -- FMP --
    _fmp_fetch = None
    fmp_key = provider_api_key("fmp")
    if fmp_key:
        from catalyst_data.connectors.fmp import create_fmp_fetcher

        _fmp_fetch = create_fmp_fetcher(
            api_key=fmp_key,
            limiter=limiters.get("fmp"),
            client=client,
        )

    # -- FRED --
    _fred_fetch = None
    fred_key = provider_api_key("fred")
    if fred_key:
        from catalyst_data.connectors.fred import create_fred_fetcher

        _fred_fetch = create_fred_fetcher(
            api_key=fred_key,
            limiter=limiters.get("fred"),
            client=client,
        )

    # -- yfinance (no API key needed) --
    from catalyst_data.connectors.yfinance_fallback import create_yfinance_fetcher

    _yfinance_fetch = create_yfinance_fetcher(limiter=limiters.get("yfinance"))

    # Endpoint-to-connector routing table
    _POLYGON_ENDPOINTS = {"ohlcv", "news"}
    _FMP_ENDPOINTS = {"income_statement", "balance_sheet", "cash_flow"}
    _FRED_SERIES = {"DFF", "DGS10", "VIXCLS", "UNRATE", "CPIAUCSL"}
    _YFINANCE_ENDPOINTS = {"income_statement", "balance_sheet", "cash_flow"}

    async def fetch(ticker: str, endpoint: str, date: str) -> FetchResult:
        if endpoint in _POLYGON_ENDPOINTS and _polygon_fetch is not None:
            return await _polygon_fetch(ticker, endpoint, date)
        if endpoint in _FMP_ENDPOINTS and _fmp_fetch is not None:
            return await _fmp_fetch(ticker, endpoint, date)
        if endpoint in _FRED_SERIES and _fred_fetch is not None:
            return await _fred_fetch(ticker, endpoint, date)
        # yfinance fallback for fundamentals when no other key is available
        if endpoint in _YFINANCE_ENDPOINTS:
            return await _yfinance_fetch(ticker, endpoint, date)
        return FetchResult(
            status=0,
            error=f"No connector available for endpoint: {endpoint}",
            source_label=f"unknown:{endpoint}",
        )

    return fetch, client


# ---------------------------------------------------------------------------
# Artifact writer (optional debug output)
# ---------------------------------------------------------------------------


def _write_artifacts(artifact_dir: Path, results_by_date: dict) -> None:
    """Persist per-date pipeline summaries as JSON for debugging."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for date, summaries in results_by_date.items():
        path = artifact_dir / f"{date}.json"
        path.write_text(json.dumps(summaries, indent=2, default=str))
    print(f"Artifacts written to {artifact_dir}/")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    # Load .env from the data-core package root
    _load_dotenv(_PACKAGE_ROOT / ".env")

    # Setup database
    data_dir = _PACKAGE_ROOT.parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = data_dir / "dev_assets.db"
    conn = sqlite3.connect(str(db_path))
    init_db(conn)
    conn.close()

    available_sources = get_available_sources()
    sources = resolve_sources(args.sources, available_sources)
    dates = get_recent_trading_days(args.days)

    print(f"Smoke test: {args.ticker} | {len(dates)} days | sources: {sources}")
    print(f"Database:   {db_path}")
    print("-" * 60)

    fetch_fn, client = await _build_fetch_fn()
    results_by_date: dict[str, list[dict]] = {}
    total_ok = 0
    total_fail = 0

    try:
        for date in dates:
            print(f"\n[{date}] Fetching {args.ticker} ...")
            t0 = time.monotonic()
            summaries = await process_request(
                ticker=args.ticker,
                date=date,
                sources=sources,
                db_path=db_path,
                fetch_fn=fetch_fn,
            )
            elapsed = (time.monotonic() - t0) * 1000
            results_by_date[date] = summaries
            for s in summaries:
                status_tag = "OK" if s.get("ok") else "FAIL"
                if s.get("ok"):
                    total_ok += 1
                else:
                    total_fail += 1
                error_msg = f"  {s.get('error', '')}" if s.get("error") else ""
                print(f"  {s.get('source', '?'):25s} [{status_tag}]{error_msg}")
            print(f"  -- {elapsed:.0f} ms total for {date}")
    finally:
        await client.aclose()

    # Summary counts from the database
    count_conn = sqlite3.connect(str(db_path))
    raw_count = count_conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0]
    clean_count = count_conn.execute("SELECT COUNT(*) FROM clean_assets").fetchone()[0]
    count_conn.close()

    print(f"\n{'=' * 60}")
    print(f"Results:  {total_ok} OK / {total_fail} FAIL")
    print(f"Database: {raw_count} raw_assets, {clean_count} clean_assets stored")

    # Optional debug artifacts
    if args.artifacts:
        artifact_dir = _PACKAGE_ROOT.parent.parent / "data" / "smoke_artifacts"
        _write_artifacts(artifact_dir, results_by_date)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Catalyst data pipeline smoke test",
    )
    parser.add_argument("--ticker", default="AAPL", help="Stock ticker (default: AAPL)")
    parser.add_argument("--days", type=int, default=3, help="Number of trading days (default: 3)")
    parser.add_argument(
        "--sources",
        help="Comma-separated logical sources to run (default: all available sources)",
    )
    parser.add_argument(
        "--artifacts",
        action="store_true",
        help="Write debug artifacts to data/smoke_artifacts/",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
