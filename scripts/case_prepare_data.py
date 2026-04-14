#!/usr/bin/env python3
"""Step A: prepare case-study SQLite data only (no index, no MCJ, no eval)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3

from event_case_study import (
    TARGET_EVENT_ID,
    PRIMARY_TICKER,
    DISTRACTOR_TICKERS,
    DATE_WINDOW_DAYS,
    SOURCES,
    load_golden_event,
    _date_range,
    build_corpus,
    DB_PATH,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare case-study SQLite corpus only.")
    parser.add_argument("--event-id", default=TARGET_EVENT_ID, help="Golden event id (default: g013)")
    args = parser.parse_args()

    golden = load_golden_event(args.event_id)
    trade_date = golden["trade_date"]

    print(f"Preparing corpus for {golden['id']} ({golden['ticker']} {trade_date})")

    missing = [k for k in ["POLYGON_API_KEY"] if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing API keys: {missing}")

    tickers_used = [PRIMARY_TICKER] + DISTRACTOR_TICKERS
    dates_used = _date_range(trade_date, DATE_WINDOW_DAYS)

    conn, corpus_summaries = await build_corpus(tickers_used, dates_used, SOURCES)

    ok_count = sum(1 for s in corpus_summaries if s.get("ok"))
    print(f"Corpus fetch done: {ok_count}/{len(corpus_summaries)} source requests OK")

    conn.row_factory = sqlite3.Row
    raw_n = conn.execute("SELECT COUNT(*) AS n FROM raw_assets").fetchone()["n"]
    clean_n = conn.execute("SELECT COUNT(*) AS n FROM clean_assets").fetchone()["n"]
    conn.row_factory = None
    conn.close()

    print(f"DB: {DB_PATH}")
    print(f"raw_assets rows: {raw_n}")
    print(f"clean_assets rows: {clean_n}")


if __name__ == "__main__":
    asyncio.run(main())
