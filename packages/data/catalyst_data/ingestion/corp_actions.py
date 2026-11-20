"""Cash dividends and splits (Polygon reference endpoints) for move-card corrections.

Stored bars are split-adjusted but not dividend-adjusted, so on an ex-dividend session the
close-to-close return understates the holder's return by cash / previous close. The move
card adds the cash back (total return); a split on the session is only flagged, because the
bars are already adjusted.

The store is optional: without it no correction is applied and nothing is flagged.

    python -m catalyst_data.ingestion.corp_actions fetch [--start 2024-09-01] [--end 2026-12-31]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.request
from functools import lru_cache
from pathlib import Path

from catalyst_data.storage.connect import connect_readonly
from catalyst_data.storage.errors import StoreReadError
from catalyst_data.storage.runtime_paths import runtime_store_path

SCHEMA = """CREATE TABLE IF NOT EXISTS dividends (symbol TEXT, ex_date TEXT, cash REAL, frequency INTEGER,
    PRIMARY KEY (symbol, ex_date));
CREATE TABLE IF NOT EXISTS splits (symbol TEXT, date TEXT, ratio REAL, PRIMARY KEY (symbol, date));"""


def store_path() -> Path:
    return runtime_store_path("corp_actions_db")


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> tuple[dict, dict]:
    try:
        with connect_readonly(path, immutable=False, timeout_ms=10_000) as conn:
            dividends = {(s, d): c for s, d, c in conn.execute("SELECT symbol, ex_date, cash FROM dividends")
                         if c is not None}
            splits = {(s, d): r for s, d, r in conn.execute("SELECT symbol, date, ratio FROM splits")}
    except sqlite3.Error as exc:
        raise StoreReadError(path, "read corporate actions", str(exc)) from exc
    return dividends, splits


def _tables() -> tuple[dict, dict]:
    path = store_path()
    if not path.exists():
        return {}, {}
    return _load(str(path), path.stat().st_mtime)


def _lookup(table: dict, symbol: str, day: str):
    # Polygon spells share classes with a dot (BRK.B); the stores use the SEC/yfinance dash.
    value = table.get((symbol, day))
    return table.get((symbol.replace("-", "."), day)) if value is None else value


def dividend(symbol: str, day: str) -> float | None:
    """Cash dividend per share going ex on ``day``, or None."""
    return _lookup(_tables()[0], symbol, day)


def split_ratio(symbol: str, day: str) -> float | None:
    """Split ratio (new shares per old) executed on ``day``, or None."""
    return _lookup(_tables()[1], symbol, day)


def total_return_pct(close: float | None, previous_close: float | None, symbol: str, day: str) -> float | None:
    """Close-to-close return in percent with the ex-day cash dividend added back."""
    if close is None or not previous_close:
        return None
    return ((float(close) + (dividend(symbol, day) or 0.0)) / float(previous_close) - 1.0) * 100.0


# --------------------------------------------------------------------------- fetch

def _pages(url: str, key: str):
    while url:
        request = urllib.request.Request(f"{url}&apiKey={key}", headers={"User-Agent": "catalyst"})
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode())
        yield payload.get("results") or []
        url = payload.get("next_url")
        time.sleep(12.5)  # free tier: 5 requests per minute


def fetch(db: Path, start: str, end: str, *, key: str) -> dict[str, int]:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    base = "https://api.polygon.io/v3/reference"
    counts = {"dividends": 0, "splits": 0}
    for rows in _pages(f"{base}/dividends?ex_dividend_date.gte={start}&ex_dividend_date.lte={end}&limit=1000", key):
        conn.executemany("INSERT OR REPLACE INTO dividends VALUES (?,?,?,?)",
                         [(x["ticker"], x["ex_dividend_date"], x.get("cash_amount"), x.get("frequency")) for x in rows])
        conn.commit()
        counts["dividends"] += len(rows)
    for rows in _pages(f"{base}/splits?execution_date.gte={start}&execution_date.lte={end}&limit=1000", key):
        conn.executemany("INSERT OR REPLACE INTO splits VALUES (?,?,?)",
                         [(x["ticker"], x["execution_date"], x["split_to"] / x["split_from"])
                          for x in rows if x.get("split_from")])
        conn.commit()
        counts["splits"] += len(rows)
    conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m catalyst_data.ingestion.corp_actions")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("fetch")
    cmd.add_argument("--start", default="2024-09-01")
    cmd.add_argument("--end", default="2026-12-31")
    args = parser.parse_args(argv)
    print(json.dumps(fetch(store_path(), args.start, args.end, key=os.environ["POLYGON_API_KEY"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
