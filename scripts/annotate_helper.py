#!/usr/bin/env python3
"""Generate a candidate pool of Golden Set v1.2 skeleton cases from yfinance data.

Downloads 2025 OHLCV for 10 tickers, filters |daily_return| > 3%,
computes Abnormal Return via OLS (60-day estimation window, SPY proxy),
and outputs skeleton GoldenEvent JSONL + AR summary CSV.

The output is a candidate pool — not a fixed set. Use --max-events to
keep only the top N events by |AR| magnitude for manual annotation.

Usage:
    python scripts/annotate_helper.py                  # all candidates
    python scripts/annotate_helper.py --max-events 50  # top 50 by |AR|
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
import yfinance as yf

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "eval"))

TICKERS = ["NVDA", "AAPL", "TSLA", "META", "MSFT", "GOOGL", "AMZN", "UNH", "JPM", "AMD"]
MARKET_PROXY = "SPY"
YEAR = "2025"
START_DATE = "2024-07-01"  # need ~120 trading days before 2025-01-02 for estimation window
END_DATE = "2025-12-31"
RETURN_THRESHOLD = 0.03  # |daily_return| > 3%
ESTIMATION_WINDOW = 60   # trading days for OLS
EXISTING_IDS = {f"g{i:03d}" for i in range(1, 6)}  # g001-g005 from v1.jsonl

OUT_JSONL = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "v1_2_skeleton.jsonl"
OUT_CSV = PROJECT_ROOT / "packages" / "eval" / "golden_set" / "v1_2_ar_summary.csv"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_abnormal_return(
    stock_returns: np.ndarray,
    market_returns: np.ndarray,
    event_stock_return: float,
    event_market_return: float,
) -> float | None:
    """OLS regression on estimation window, then compute AR for event day.

    AR = R_stock - (alpha + beta * R_market)
    """
    if len(stock_returns) < 60 or len(market_returns) < 60:
        return None

    # OLS: R_stock = alpha + beta * R_market
    X = np.column_stack([np.ones(len(market_returns)), market_returns])
    y = stock_returns

    # Normal equation: (X'X)^-1 X'y
    try:
        coeffs = np.linalg.lstsq(X, y, rcond=None)[0]
    except np.linalg.LinAlgError:
        return None

    alpha, beta = coeffs[0], coeffs[1]
    expected_return = alpha + beta * event_market_return
    return event_stock_return - expected_return


def weight_tier(abs_ar: float) -> str:
    """Map |AR| to suggested primary weight tier per golden_set README."""
    if abs_ar > 0.05:
        return ">=0.7"
    elif abs_ar > 0.02:
        return "0.5-0.6"
    else:
        return "macro>=0.5"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate Golden Set v1.2 skeleton candidates")
    parser.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="Keep only the top N events by |AR| magnitude. Default: no limit (all candidates).",
    )
    args = parser.parse_args()

    all_tickers = TICKERS + [MARKET_PROXY]
    print(f"Downloading OHLCV for {len(all_tickers)} tickers ({START_DATE} to {END_DATE})...")
    data = yf.download(all_tickers, start=START_DATE, end=END_DATE, auto_adjust=True, progress=False)

    # Extract close prices — handle both MultiIndex and single-ticker formats
    close = data["Close"]

    # Compute daily returns
    returns = close.pct_change().dropna()

    # Filter to 2025 only for event detection
    returns_2025 = returns[returns.index >= "2025-01-01"]

    events: list[dict] = []
    event_id_counter = 6  # start at g006

    for ticker in TICKERS:
        if ticker not in returns.columns:
            print(f"  WARNING: {ticker} not in downloaded data, skipping")
            continue

        ticker_returns_2025 = returns_2025[ticker].dropna()
        market_returns_all = returns[MARKET_PROXY].dropna()
        stock_returns_all = returns[ticker].dropna()

        for date, ret in ticker_returns_2025.items():
            if abs(ret) <= RETURN_THRESHOLD:
                continue

            trade_date_str = date.strftime("%Y-%m-%d")

            # Find estimation window: 60 trading days before event
            # Get the index position in the full returns series
            all_dates = stock_returns_all.index
            if date not in all_dates:
                continue
            pos = all_dates.get_loc(date)
            if pos < ESTIMATION_WINDOW:
                continue

            est_dates = all_dates[pos - ESTIMATION_WINDOW : pos]
            est_stock = stock_returns_all.loc[est_dates].values
            est_market = market_returns_all.reindex(est_dates).dropna().values

            # Need aligned arrays
            common_est_dates = stock_returns_all.loc[est_dates].dropna().index.intersection(
                market_returns_all.reindex(est_dates).dropna().index
            )
            est_stock = stock_returns_all.loc[common_est_dates].values
            est_market = market_returns_all.loc[common_est_dates].values

            event_market_ret = market_returns_all.get(date, np.nan)
            if np.isnan(event_market_ret):
                continue

            ar = compute_abnormal_return(est_stock, est_market, ret, event_market_ret)
            if ar is None:
                continue

            pct = round(ret * 100, 2)
            ar_pct = round(ar * 100, 2)
            event_id = f"g{event_id_counter:03d}"
            event_id_counter += 1

            events.append({
                "id": event_id,
                "ticker": ticker,
                "trade_date": trade_date_str,
                "price_move_pct": pct,
                "ar_pct": ar_pct,
                "causes": [
                    {
                        "text": f"[TO_ANNOTATE] {ticker} moved {pct}% — AR={ar_pct}%",
                        "category": "sector",
                        "weight": 1.0,
                        "temporal_anchor": "intraday",
                        "evidence_ids": [],
                    }
                ],
            })

    print(f"Found {len(events)} candidate events with |return| > {RETURN_THRESHOLD * 100}%")

    # Optional: keep only top N by |AR| magnitude
    if args.max_events is not None and len(events) > args.max_events:
        events.sort(key=lambda e: abs(e["ar_pct"]), reverse=True)
        events = events[:args.max_events]
        print(f"Trimmed to top {args.max_events} events by |AR| magnitude")

    # Sort by date, then ticker
    events.sort(key=lambda e: (e["trade_date"], e["ticker"]))

    # Re-assign sequential IDs after sorting
    for i, event in enumerate(events):
        event["id"] = f"g{i + 6:03d}"

    # Write JSONL
    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSONL, "w") as f:
        for event in events:
            # Strip ar_pct from the JSONL output (not part of GoldenEvent schema)
            jsonl_event = {k: v for k, v in event.items() if k != "ar_pct"}
            f.write(json.dumps(jsonl_event) + "\n")
    print(f"JSONL written to {OUT_JSONL} ({len(events)} events)")

    # Write CSV summary
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "date", "return_pct", "ar_pct", "suggested_primary_weight_tier"])
        for event in events:
            writer.writerow([
                event["ticker"],
                event["trade_date"],
                event["price_move_pct"],
                event["ar_pct"],
                weight_tier(abs(event["ar_pct"]) / 100),
            ])
    print(f"CSV written to {OUT_CSV}")

    # Validate against schema
    from catalyst_eval.schema.golden_event import GoldenEvent
    for event in events:
        jsonl_event = {k: v for k, v in event.items() if k != "ar_pct"}
        GoldenEvent(**jsonl_event)
    print(f"Schema validation passed for all {len(events)} events")


if __name__ == "__main__":
    main()
