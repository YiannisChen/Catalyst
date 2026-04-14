#!/usr/bin/env python3
"""Step C: run MCJ + eval report using existing SQLite and LanceDB artifacts."""
from __future__ import annotations

import argparse
import sqlite3
import time

from event_case_study import (
    TARGET_EVENT_ID,
    DB_PATH,
    PRIMARY_TICKER,
    DISTRACTOR_TICKERS,
    DATE_WINDOW_DAYS,
    load_golden_event,
    _date_range,
    build_index,
    GeminiLLM,
    GEMINI_MODEL,
    run_mcj,
    write_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MCJ attribution and eval report (no fetch).")
    parser.add_argument("--event-id", default=TARGET_EVENT_ID, help="Golden event id (default: g013)")
    parser.add_argument("--reuse-index", action="store_true", help="Reuse existing LanceDB table if present")
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise RuntimeError(f"DB not found: {DB_PATH}. Run case_prepare_data.py first.")

    golden = load_golden_event(args.event_id)
    dates_used = _date_range(golden["trade_date"], DATE_WINDOW_DAYS)
    tickers_used = [PRIMARY_TICKER] + DISTRACTOR_TICKERS

    wall_t0 = time.monotonic()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        silver_rows = conn.execute(
            "SELECT asset_id, ticker, source_type, reference_date, content_md "
            "FROM clean_assets WHERE is_duplicate = 0 AND LENGTH(content_md) > 0"
        ).fetchall()
        silver_chunks = [dict(r) for r in silver_rows]

        if not silver_chunks:
            raise RuntimeError("No clean chunks found. Run case_prepare_data.py first.")

        t0 = time.monotonic()
        table, embedding_fn, _ = build_index(conn, reuse_index=args.reuse_index)
        index_ms = (time.monotonic() - t0) * 1000
        if table is None:
            raise RuntimeError("Index unavailable after build_index().")

        llm = GeminiLLM(model=GEMINI_MODEL, temperature=0.0)

        t0 = time.monotonic()
        state = run_mcj(table, embedding_fn, llm, golden)
        mcj_ms = (time.monotonic() - t0) * 1000

    finally:
        conn.row_factory = None
        conn.close()

    wall_total = (time.monotonic() - wall_t0) * 1000

    timings = {
        "Data collection": 0.0,
        "Index build": index_ms,
        "Agent pipeline (Miner → Critic → Judge)": mcj_ms,
        "Total wall time": wall_total,
    }

    write_report(
        golden=golden,
        dates_used=dates_used,
        tickers_used=tickers_used,
        corpus_summaries=[],
        silver_chunks=silver_chunks,
        state=state,
        timings=timings,
    )

    print(f"MCJ causes: {len(state.get('causes', []))}")
    print(f"Critic passed chunks: {len(state.get('graded_evidence', []))}")


if __name__ == "__main__":
    main()
