"""Polygon fallback (B8): when yfinance returns no bars, missing sessions are filled from Polygon's
grouped daily endpoint (one request per session, all US tickers) and stored with source='polygon'.
Both sources are split-adjusted and not dividend-adjusted (yfinance auto_adjust=False; Polygon
adjusted=true). Measured 2026-10-09 on 488 and 491 symbols (2026-10-01, 2026-09-15): median absolute
relative close difference 0.0, maximum 0.0001, none above 0.1%. Returns may therefore mix the two
sources; compare_polygon_closes re-checks agreement for a symbol.

Market bar and SEC SIC ingestion for M9.

When yfinance misses a session, grouped Polygon daily bars fill only that missing
date and carry ``source='polygon'``. Returns at source boundaries use two closes
from the same source; if no same-source previous close exists, no return is
reported. This avoids creating a return from a yfinance close and a Polygon
close when the providers differ.
"""
from __future__ import annotations

import functools
import json
import math
import os
import sqlite3
import statistics
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from catalyst_data import trading_calendar
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.runtime_paths import default_sec_tickers_path

ETFS = (
    "SPY", "QQQ", "SMH", "XLK", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV",
    "XLI", "XLB", "XLU", "XLRE", "GLD", "SLV", "CPER", "USO", "UNG",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL,
    close REAL, volume REAL, source TEXT NOT NULL, PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS ohlcv_metadata (
    symbol TEXT NOT NULL, date TEXT NOT NULL, first_seen_at TEXT,
    is_backfill INTEGER CHECK (is_backfill IS NULL OR is_backfill IN (0, 1)),
    provenance TEXT NOT NULL DEFAULT 'known',
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS issuer_sic (
    ticker TEXT PRIMARY KEY, cik TEXT NOT NULL, sic TEXT, sic_description TEXT,
    fetched_at TEXT NOT NULL
);
"""

REPO = Path(__file__).resolve().parents[5]
POLYGON_GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{}"
POLYGON_MIN_INTERVAL_S = 12.1
_polygon_cache: dict[str, dict[str, dict]] = {}
_polygon_last_request = 0.0
_polygon_lock = threading.Lock()


def _clear_polygon_cache() -> None:
    global _polygon_last_request
    with _polygon_lock:
        _polygon_cache.clear()
        _polygon_last_request = 0.0


def _polygon_session(session_date: str, *, http_get: Callable | None = None,
                     sleep: Callable[[float], None] = time.sleep) -> dict[str, dict]:
    global _polygon_last_request
    if session_date in _polygon_cache:
        return _polygon_cache[session_date]
    key = os.environ.get("POLYGON_API_KEY", "").strip().strip('"')
    if not key:
        return {}
    if http_get is None:
        import requests
        http_get = requests.get
    with _polygon_lock:
        if session_date in _polygon_cache:
            return _polygon_cache[session_date]
        wait = POLYGON_MIN_INTERVAL_S - (time.monotonic() - _polygon_last_request)
        if _polygon_last_request and wait > 0:
            sleep(wait)
        response = http_get(POLYGON_GROUPED_URL.format(session_date),
                            params={"adjusted": "true", "apiKey": key}, timeout=60)
        _polygon_last_request = time.monotonic()
        if response.status_code != 200:
            raise RuntimeError(f"Polygon grouped daily HTTP {response.status_code}")
        _polygon_cache[session_date] = {
            str(row["T"]): row for row in response.json().get("results", []) if row.get("T")
        }
        return _polygon_cache[session_date]


def compare_polygon_closes(conn: sqlite3.Connection, symbol: str, polygon_rows: Iterable[dict]) -> dict:
    """Compare Polygon rows with stored yfinance closes; the median is a fraction (0.02 = 2%)."""
    polygon = {str(row["date"]): float(row["close"]) for row in polygon_rows
               if row.get("date") and row.get("close") is not None}
    if not polygon:
        return {"symbol": symbol, "overlap": 0, "median_absolute_relative_difference": None}
    marks = ",".join("?" for _ in polygon)
    yfinance = dict(conn.execute(
        f"SELECT date, close FROM ohlcv WHERE symbol=? AND source='yfinance' AND date IN ({marks})",
        [symbol, *polygon.keys()]).fetchall())
    diffs = [abs(polygon[day] - float(close)) / abs(float(close))
             for day, close in yfinance.items() if close not in (None, 0)]
    return {"symbol": symbol, "overlap": len(diffs),
            "median_absolute_relative_difference": statistics.median(diffs) if diffs else None}


def _utc_now(now: datetime | None) -> datetime:
    return timefields.now_utc(now)


def session_closed(session_date: str, now: datetime | None = None) -> bool:
    if not trading_calendar.is_trading_day(session_date):
        return False
    close = timefields.stored(trading_calendar.session_close_utc(session_date))
    return close < _utc_now(now)


def last_closed_session(now: datetime | None = None) -> str:
    current = _utc_now(now)
    candidate = current.date()
    for _ in range(10):
        session = candidate.isoformat()
        if trading_calendar.is_trading_day(session) and session_closed(session, current):
            return session
        candidate -= timedelta(days=1)
    raise ValueError("NO_CLOSED_SESSION_WITHIN_10_DAYS")


_MIGRATED: set[tuple[str, int]] = set()


def open_market_writer(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    _ensure_ohlcv_metadata(conn)
    return conn


def _ensure_ohlcv_metadata(conn: sqlite3.Connection) -> None:
    """Run the provenance migration once per process and file. It scans every stored bar (~290k rows, 0.25 s) and the
    daily opens a writer and calls load_bars per symbol, so doing it each time made the price stage five times
    slower (0.2 -> 1.1 s per symbol from 10-28). Bars written afterwards record their own metadata."""
    path = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    try:
        stat = os.stat(path) if path else None
    except OSError:
        stat = None
    identity = (str(Path(path).resolve()), stat.st_dev, stat.st_ino) if stat else None
    if identity is not None and identity in _MIGRATED:
        return
    caller_owns_transaction = conn.in_transaction
    _migrate_ohlcv_metadata(conn)
    # Remember it only once it is committed: a caller that closes without committing (a skipped symbol) would roll
    # the migration back, and the memo would then hide that for the rest of the process. Inside a caller's own
    # transaction we cannot commit for it, so we do not remember.
    if identity is not None and not caller_owns_transaction:
        conn.commit()
        _MIGRATED.add(identity)


def _migrate_ohlcv_metadata(conn: sqlite3.Connection) -> None:
    """Additive, idempotent provenance migration; legacy observation times stay unknown."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ohlcv_metadata ("
        "symbol TEXT NOT NULL, date TEXT NOT NULL, first_seen_at TEXT, "
        "is_backfill INTEGER CHECK (is_backfill IS NULL OR is_backfill IN (0, 1)), "
        "provenance TEXT NOT NULL DEFAULT 'known', "
        "PRIMARY KEY (symbol, date))"
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ohlcv_metadata)")}
    backfill_info = next(row for row in conn.execute("PRAGMA table_info(ohlcv_metadata)")
                         if row[1] == "is_backfill")
    legacy_backfill = "0" if backfill_info[3] else "NULL"
    if "provenance" not in columns:
        # SQLite cannot alter a table CHECK constraint in place. Keep the old
        # constrained is_backfill value, and explicitly mark migrated rows as
        # unknown so readers never interpret that legacy 0 as verified.
        conn.execute("ALTER TABLE ohlcv_metadata ADD COLUMN provenance TEXT")
    conn.execute(
        "UPDATE ohlcv_metadata SET provenance='legacy_unknown' "
        "WHERE first_seen_at IS NULL AND (provenance IS NULL OR provenance='known')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO ohlcv_metadata "
        "(symbol,date,first_seen_at,is_backfill,provenance) "
        f"SELECT symbol,date,NULL,{legacy_backfill},'legacy_unknown' FROM ohlcv"
    )


def _record_ohlcv_metadata(conn: sqlite3.Connection, symbol: str, session: str, *,
                           first_seen_at: str | None, is_backfill: bool) -> None:
    conn.execute(
        "INSERT INTO ohlcv_metadata "
        "(symbol,date,first_seen_at,is_backfill,provenance) VALUES (?,?,?,?,?) "
        "ON CONFLICT(symbol,date) DO UPDATE SET "
        "first_seen_at=COALESCE(ohlcv_metadata.first_seen_at,excluded.first_seen_at), "
        "is_backfill=CASE WHEN ohlcv_metadata.first_seen_at IS NULL "
        "AND excluded.first_seen_at IS NOT NULL THEN excluded.is_backfill "
        "ELSE ohlcv_metadata.is_backfill END, "
        "provenance=CASE WHEN ohlcv_metadata.first_seen_at IS NULL "
        "AND excluded.first_seen_at IS NOT NULL THEN 'known' "
        "ELSE ohlcv_metadata.provenance END",
        (symbol, session, first_seen_at, int(is_backfill),
         "known" if first_seen_at is not None else "legacy_unknown"),
    )


@functools.lru_cache(maxsize=1)
def _yfinance_download() -> Callable:
    """yfinance.download; IPv4-only libcurl session when CATALYST_PREFER_IPV4=1
    (yfinance uses curl_cffi, which the requests-level IPv4 preference does not reach).
    The session matches yfinance's own default (curl_cffi, impersonate="chrome")
    except for IPv4 resolution. Cached: one session (one cookie warm-up) per process."""
    import yfinance

    if os.environ.get("CATALYST_PREFER_IPV4") != "1":
        return yfinance.download
    from curl_cffi import CurlOpt
    from curl_cffi import requests as curl_requests

    session = curl_requests.Session(impersonate="chrome", curl_options={CurlOpt.IPRESOLVE: 1})
    return functools.partial(yfinance.download, session=session)


def load_bars(
    conn: sqlite3.Connection,
    symbols: Iterable[str],
    start: str,
    through_session: str,
    *,
    now: datetime | None = None,
    download: Callable | None = None,
    polygon_get: Callable | None = None,
    polygon_sleep: Callable[[float], None] = time.sleep,
    fallback_sessions: Iterable[str] = (),
    is_backfill: bool = False,
) -> dict[str, int]:
    if not session_closed(through_session, now):
        raise ValueError("SESSION_NOT_CLOSED")
    if not isinstance(is_backfill, bool):
        raise ValueError("is_backfill must be a boolean")
    _ensure_ohlcv_metadata(conn)
    if download is None:
        download = _yfinance_download()
    exclusive_end = (date.fromisoformat(through_session) + timedelta(days=1)).isoformat()
    written: dict[str, int] = {}
    for symbol in symbols:
        previous_dates = {row[0] for row in conn.execute(
            "SELECT date FROM ohlcv WHERE symbol=? AND date>=? AND date<=?",
            (symbol, start, through_session),
        ).fetchall()}
        frame = download(
            symbol, start=start, end=exclusive_end, progress=False, auto_adjust=False
        )
        rows = []
        if not frame.empty:
            if hasattr(frame.columns, "levels"):
                frame.columns = [column[0] for column in frame.columns]
            rows = [
                (symbol, idx.strftime("%Y-%m-%d"), float(row["Open"]), float(row["High"]),
                 float(row["Low"]), float(row["Close"]), float(row["Volume"]), "yfinance")
                for idx, row in frame.iterrows()
                if start <= idx.strftime("%Y-%m-%d") <= through_session
            ]
            # A bar with a missing price is not stored (2026-10-22: Yahoo returned 53 bars of 10-21 without a
            # close, and the movers stage failed on them); the session then goes to the Polygon fallback below.
            rows = [r for r in rows if not any(math.isnan(v) for v in r[2:6])]
        if rows:
            conn.executemany("INSERT OR REPLACE INTO ohlcv VALUES (?,?,?,?,?,?,?,?)", rows)
            observed_at = timefields.stored(datetime.now(timezone.utc)).isoformat(timespec="microseconds")
            for row in rows:
                _record_ohlcv_metadata(
                    conn, symbol, row[1], first_seen_at=None if row[1] in previous_dates else observed_at,
                    is_backfill=is_backfill,
                )
        existing = previous_dates | {row[1] for row in rows}
        polygon_added = 0
        for session in fallback_sessions:
            if session < start or session > through_session:
                continue
            if session in existing:
                continue
            try:
                # Polygon spells share classes with a dot (BRK.B); the stores use the SEC/yfinance dash.
                bar = _polygon_session(session, http_get=polygon_get, sleep=polygon_sleep).get(symbol.replace("-", "."))
            except Exception:
                continue
            if bar is None or bar.get("c") is None:
                continue
            stamp = timefields.external(bar["t"], source="polygon", field="bar_t").utc.date().isoformat()
            if stamp != session:
                continue
            cursor = conn.execute("INSERT OR IGNORE INTO ohlcv VALUES (?,?,?,?,?,?,?,?)",
                                  (symbol, session, bar.get("o"), bar.get("h"), bar.get("l"),
                                   bar.get("c"), bar.get("v"), "polygon"))
            polygon_added += cursor.rowcount
            if cursor.rowcount:
                observed_at = timefields.stored(datetime.now(timezone.utc)).isoformat(timespec="microseconds")
                _record_ohlcv_metadata(
                    conn, symbol, session, first_seen_at=observed_at, is_backfill=is_backfill,
                )
        conn.commit()
        written[symbol] = len(rows) + polygon_added
    return written


def load_sic(
    conn: sqlite3.Connection,
    tickers: list[str],
    *,
    user_agent: str | None = None,
    http_get: Callable | None = None,
    sleep: Callable[[float], None] | None = None,
    tickers_json: str | Path | None = None,
) -> dict[str, str | None]:
    if user_agent is None:
        user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise ValueError("SEC_USER_AGENT not set")
    if tickers_json is None:
        from catalyst_data.retrieval import web_bodies

        tickers_json = web_bodies.name_map_path() or default_sec_tickers_path(REPO)
    data = json.loads(Path(tickers_json).read_text())
    cik_by_ticker = {item["ticker"]: str(item["cik_str"]).zfill(10) for item in data.values()}
    if http_get is None:
        import requests

        http_get = requests.get
    if sleep is None:
        sleep = time.sleep

    result: dict[str, str | None] = {}
    requested = False
    for ticker in tickers:
        existing = conn.execute("SELECT 1 FROM issuer_sic WHERE ticker = ?", (ticker,)).fetchone()
        if existing:
            result[ticker] = conn.execute(
                "SELECT sic FROM issuer_sic WHERE ticker = ?", (ticker,)
            ).fetchone()[0]
            continue
        cik = cik_by_ticker.get(ticker)
        if cik is None:
            result[ticker] = None
            continue
        if requested:
            sleep(0.2)
        # SEC drops bursty connections (SSL EOF); retry this ticker with
        # backoff and move on rather than abort the whole batch.
        response = None
        for attempt in range(3):
            try:
                response = http_get(
                    f"https://data.sec.gov/submissions/CIK{cik}.json",
                    timeout=30,
                    headers={"User-Agent": user_agent},
                )
                if response.status_code not in (429, 500, 502, 503, 504):
                    break
            except Exception:  # network error for this ticker only
                response = None
            sleep(2 * (2 ** attempt))
        requested = True
        if response is None or response.status_code != 200:
            result[ticker] = None
            continue
        body = response.json()
        sic = body.get("sic")
        conn.execute(
            "INSERT OR REPLACE INTO issuer_sic VALUES (?,?,?,?,?)",
            (ticker, cik, sic, body.get("sicDescription"),
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        conn.commit()
        result[ticker] = sic
    return result
