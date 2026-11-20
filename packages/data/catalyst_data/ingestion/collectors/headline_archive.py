"""Durable archive of Finnhub company-news headline lists.

Finnhub's free tier serves company news for a rolling year only, and each
request returns at most about 240 items (the most recent ones). This module
copies the headline index (URL, title, publication time, source) into a local
store before it rolls out of reach, so bodies can be fetched later from the
publishers' pages. Nothing here fetches article bodies.

- Windows start at a week and are halved while a response hits the item cap,
  down to one day. A capped one-day window is recorded as CAPPED.
- Windows are processed oldest first (those roll out of reach first).
- Every window is committed with its status; a rerun skips windows already OK,
  EMPTY or CAPPED, so the archive resumes after an interruption.
- Requests are spaced at REQUEST_INTERVAL_S (free tier: 60 calls/minute).

  PYTHONNOUSERSITE=1 PYTHONPATH=packages/data \
    python3 -m catalyst_data.ingestion.collectors.headline_archive --store data/m9/headline_archive.sqlite3 \
      --start 2025-10-08 --end 2026-10-06 [--symbols AAPL,MSFT]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4

import requests

from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.time import timefields

FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
ITEM_CAP = 230  # responses at or above this are treated as truncated (observed cap ~240)
INITIAL_WINDOW_DAYS = 7
REQUEST_INTERVAL_S = 1.05
MAX_ATTEMPTS = 3
REPO = Path(__file__).resolve().parents[5]
COVERAGE_SOURCE_ID = "daily:finnhub"
COVERAGE_TASK_ID = "headline_archive:finnhub_company"
COVERAGE_POLICY_VERSION = "headline_archive_v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS finnhub_news (
    symbol TEXT NOT NULL,
    news_id INTEGER NOT NULL,
    published_utc TEXT NOT NULL,
    headline TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT,
    category TEXT,
    related TEXT,
    summary TEXT,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (symbol, news_id)
);
CREATE INDEX IF NOT EXISTS idx_finnhub_news_symbol_time ON finnhub_news (symbol, published_utc);
CREATE TABLE IF NOT EXISTS archive_windows (
    symbol TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    status TEXT NOT NULL,          -- OK | EMPTY | CAPPED | SPLIT | ERROR
    items INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    detail TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, window_start, window_end)
);
"""
DONE = ("OK", "EMPTY", "CAPPED", "SPLIT")


def subjects() -> list[str]:
    """Supported universe + peer-map company peers + commodity proxies and SPY."""
    from catalyst_data.retrieval.web_bodies import COMMODITY_TERMS

    universe: set[str] = set()
    for names in json.loads((REPO / "data/manifests/heldout_universe_v1.json").read_text())["sectors"].values():
        universe.update(names)
    peers: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and value.isupper():
            peers.add(value)

    targets = json.loads((REPO / "data/manifests/peer_map_v1.json").read_text())["targets"]
    walk(targets)
    etfs = {p for p in peers if p.startswith("XL") or p == "SMH"}  # fund news is mostly paywalled
    return sorted(universe | (peers - etfs) | set(targets) - etfs | set(COMMODITY_TERMS))


def open_store(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    for attempt in range(10):  # parallel shards may race on the first WAL switch
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            coverage.ensure_schema(conn)
            conn.commit()
            return conn
        except sqlite3.OperationalError:
            if attempt == 9:
                raise
            time.sleep(1 + attempt)
    return conn


@dataclass
class Window:
    symbol: str
    start: date
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def initial_windows(symbols: Iterable[str], start: date, end: date) -> list[Window]:
    """Week windows, oldest first across all symbols."""
    out = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=INITIAL_WINDOW_DAYS - 1), end)
        out.extend(Window(symbol, cursor, stop) for symbol in symbols)
        cursor = stop + timedelta(days=1)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _done(conn: sqlite3.Connection, window: Window) -> bool:
    row = conn.execute(
        "SELECT status FROM archive_windows WHERE symbol=? AND window_start=? AND window_end=?",
        (window.symbol, window.start.isoformat(), window.end.isoformat()),
    ).fetchone()
    return row is not None and row[0] in DONE


def _record(conn, window: Window, status: str, items: int, attempts: int, detail: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO archive_windows VALUES (?,?,?,?,?,?,?,?)",
        (window.symbol, window.start.isoformat(), window.end.isoformat(), status, items,
         attempts, detail[:300], _now()),
    )


def _split_window(window: Window) -> tuple[Window, Window]:
    middle = window.start + timedelta(days=window.days // 2 - 1)
    return (
        Window(window.symbol, window.start, middle),
        Window(window.symbol, middle + timedelta(days=1), window.end),
    )


def coverage_leaves(conn: sqlite3.Connection, symbols: Iterable[str], start: date, end: date) -> list[Window]:
    """Derive the current expected leaf windows from the durable split tree.

    SPLIT parents are never leaves. Missing/ERROR/CAPPED children remain expected
    leaves so their absence or incompleteness stays visible to readiness callers.
    """
    leaves: list[Window] = []

    def expand(window: Window) -> None:
        row = conn.execute(
            "SELECT status FROM archive_windows WHERE symbol=? AND window_start=? AND window_end=?",
            (window.symbol, window.start.isoformat(), window.end.isoformat()),
        ).fetchone()
        if row is None or row[0] != "SPLIT" or window.days <= 1:
            leaves.append(window)
            return
        older, newer = _split_window(window)
        expand(older)
        expand(newer)

    for window in initial_windows(symbols, start, end):
        expand(window)
    return leaves


def _coverage_bounds(window: Window) -> tuple[str, str]:
    """Convert inclusive UTC calendar dates to the contract's (start, end] interval."""
    begin = datetime.combine(window.start, datetime.min.time(), timezone.utc) - timedelta(microseconds=1)
    until = datetime.combine(window.end + timedelta(days=1), datetime.min.time(), timezone.utc) - timedelta(microseconds=1)
    return begin.isoformat(timespec="microseconds"), until.isoformat(timespec="microseconds")


def _record_coverage(conn, window: Window, *, status: str, enumeration_complete: bool | None) -> None:
    begin, until = _coverage_bounds(window)
    attempt = coverage.start_attempt(
        conn,
        logical_task_id=COVERAGE_TASK_ID,
        attempt_id=uuid4().hex,
        source_id=COVERAGE_SOURCE_ID,
        product="headlines",
        subject_kind="ticker",
        subject_id=window.symbol,
        query_spec={},
        coverage_policy_version=COVERAGE_POLICY_VERSION,
        requested_from=begin,
        requested_until=until,
    )
    coverage.record_coverage(
        conn,
        coverage_id=uuid4().hex,
        attempt_id=attempt.attempt_id,
        status=status,
        reason="ERROR" if status == "ERROR" else None,
        recorded_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        coverage_from=begin if enumeration_complete is True else None,
        coverage_until=until if enumeration_complete is True else None,
        enumeration_complete=enumeration_complete,
    )


def _validate_response_items(items) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("Finnhub response must be a list")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Finnhub item must be a mapping")
        news_id = item.get("id")
        if type(news_id) is int:
            valid_id = news_id >= 0
        elif isinstance(news_id, str) and news_id.isascii() and news_id.isdecimal():
            valid_id = news_id == "0" or not news_id.startswith("0")
        else:
            valid_id = False
        if not valid_id:
            raise ValueError("Finnhub item ID is invalid") from None
        if not item.get("datetime") or not isinstance(item.get("url"), str) or not item["url"].strip():
            raise ValueError("Finnhub item is missing its timestamp or URL")
    return items


def _error_receipt(conn, window: Window, attempts: int, detail: str) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        _record(conn, window, "ERROR", 0, attempts, detail)
        _record_coverage(conn, window, status="ERROR", enumeration_complete=None)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _store_items(conn, symbol: str, items: list[dict]) -> int:
    added = 0
    seen_at = _now()
    for item in items:
        news_id = item.get("id")
        when = item.get("datetime")
        if news_id is None or not when or not item.get("url"):
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO finnhub_news VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, int(news_id),
             timefields.external_stamp(when, source="finnhub", field="datetime"),
             str(item.get("headline") or ""), str(item["url"]), item.get("source"),
             item.get("category"), item.get("related"), item.get("summary"),
             json.dumps(item, sort_keys=True), seen_at),
        )
        added += cursor.rowcount
    return added


@dataclass
class Stats:
    calls: int = 0
    windows: int = 0
    items: int = 0
    capped: int = 0
    errors: int = 0


def archive(
    conn: sqlite3.Connection,
    symbols: Iterable[str],
    start: date,
    end: date,
    *,
    token: str,
    http_get: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda _: None,
    interval_s: float = REQUEST_INTERVAL_S,
) -> Stats:
    if http_get is None:
        http_get = requests.Session().get  # one kept-alive connection for the whole run
    stats = Stats()
    queue = coverage_leaves(conn, list(symbols), start, end)
    queue.reverse()  # pop() takes the oldest
    while queue:
        window = queue.pop()
        if _done(conn, window):
            continue
        items, status, detail, attempts = [], "ERROR", "", 0
        for attempts in range(1, MAX_ATTEMPTS + 1):
            sleep(interval_s if attempts == 1 else interval_s * 5 * attempts)
            stats.calls += 1
            try:
                response = http_get(
                    FINNHUB_URL,
                    params={"symbol": window.symbol, "from": window.start.isoformat(),
                            "to": window.end.isoformat(), "token": token},
                    timeout=30,
                )
            except Exception as exc:
                detail = type(exc).__name__
                continue
            if response.status_code == 429:
                detail = "429"
                sleep(30)
                continue
            if response.status_code != 200:
                detail = f"HTTP {response.status_code}"
                continue
            try:
                items = _validate_response_items(response.json())
            except Exception as exc:
                detail = type(exc).__name__
                continue
            status = "OK" if items else "EMPTY"
            break
        if status == "ERROR":
            _error_receipt(conn, window, attempts, detail)
            stats.errors += 1
            log(f"ERROR {window.symbol} {window.start}..{window.end} {detail}")
            continue
        if len(items) >= ITEM_CAP and window.days > 1:
            older, newer = _split_window(window)
            try:
                conn.execute("BEGIN IMMEDIATE")
                added = _store_items(conn, window.symbol, items)
                _record(conn, window, "SPLIT", len(items), attempts)
                _record_coverage(conn, window, status="OK", enumeration_complete=False)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                _error_receipt(conn, window, attempts, type(exc).__name__)
                stats.errors += 1
                continue
            # newer half first on the stack so the older half is processed next
            queue.append(newer)
            queue.append(older)
            stats.items += added
            continue
        if len(items) >= ITEM_CAP:
            status = "CAPPED"
        coverage_status = "OK" if status == "CAPPED" else status
        enumeration_complete = False if status == "CAPPED" else True
        try:
            conn.execute("BEGIN IMMEDIATE")
            added = _store_items(conn, window.symbol, items)
            _record(conn, window, status, len(items), attempts)
            _record_coverage(conn, window, status=coverage_status,
                             enumeration_complete=enumeration_complete)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            _error_receipt(conn, window, attempts, type(exc).__name__)
            stats.errors += 1
            continue
        if status == "CAPPED":
            stats.capped += 1
        stats.items += added
        stats.windows += 1
        if stats.windows % 200 == 0:
            log(f"{stats.windows} windows, {stats.calls} calls, {stats.items} new items, at {window.start}")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", help="comma-separated; default: universe + peers + proxies")
    parser.add_argument("--shard", default="0/1",
                        help="i/n: take every n-th symbol from i (parallel processes share the store)")
    parser.add_argument("--interval", type=float, default=REQUEST_INTERVAL_S,
                        help="seconds between requests in this process; keep the sum under 60/min")
    args = parser.parse_args(argv)
    token = os.environ.get("FINNHUB_API_KEY", "").strip().strip('"')
    if not token:
        parser.error("FINNHUB_API_KEY is not set")
    symbols = args.symbols.split(",") if args.symbols else subjects()
    index, count = (int(part) for part in args.shard.split("/"))
    symbols = symbols[index::count]
    conn = open_store(args.store)
    print(f"archiving {len(symbols)} symbols {args.start}..{args.end}", flush=True)
    stats = archive(conn, symbols, date.fromisoformat(args.start), date.fromisoformat(args.end),
                    token=token, log=lambda line: print(line, flush=True), interval_s=args.interval)
    print(json.dumps(stats.__dict__), flush=True)
    return 1 if stats.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
