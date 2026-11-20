"""Session-scoped Polygon news feed used by the daily ingestion job."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable

import requests

from catalyst_data.ingestion.collectors import headlines
from catalyst_data.ingestion.time import timefields

POLYGON_URL = "https://api.polygon.io/v2/reference/news"
POLYGON_INTERVAL_S = 12.5
POLYGON_MAX_PAGES = 10


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PolygonFeed:
    """Fetch and cache the session window's Polygon news once per session."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS polygon_feed (
        session_date TEXT NOT NULL, news_id TEXT NOT NULL, published_utc TEXT NOT NULL,
        title TEXT NOT NULL, url TEXT NOT NULL, tickers_json TEXT NOT NULL, raw_json TEXT NOT NULL,
        PRIMARY KEY (session_date, news_id)
    );
    CREATE TABLE IF NOT EXISTS polygon_feed_sessions (
        session_date TEXT PRIMARY KEY, status TEXT NOT NULL, items INTEGER NOT NULL,
        pages INTEGER NOT NULL, fetched_at TEXT NOT NULL
    );
    """

    def __init__(self, conn: sqlite3.Connection, *, http_get: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.conn = conn
        self.conn.executescript(self.SCHEMA)
        self.http_get = http_get or requests.Session().get
        self.sleep = sleep
        self.key = os.environ.get("POLYGON_API_KEY", "").strip().strip('"')

    def _status(self, session_date: str) -> str | None:
        row = self.conn.execute("SELECT status FROM polygon_feed_sessions WHERE session_date=?",
                                (session_date,)).fetchone()
        return row[0] if row else None

    def ensure(self, session_date: str) -> str:
        cached = self._status(session_date)
        if cached in ("OK", "EMPTY"):
            return cached
        if not self.key:
            return "DISABLED"
        start, cutoff = headlines.session_window(session_date)
        params = {"published_utc.gte": start, "published_utc.lte": cutoff, "limit": 1000,
                  "order": "asc", "apiKey": self.key}
        url, pages, rows = POLYGON_URL, 0, []
        try:
            while url and pages < POLYGON_MAX_PAGES:
                self.sleep(POLYGON_INTERVAL_S)
                response = self.http_get(url, params=params, timeout=60)
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}")
                payload = response.json()
                pages += 1
                rows += payload.get("results", [])
                url = payload.get("next_url")
                params = {"apiKey": self.key}  # next_url carries the cursor
        except Exception:
            return "ERROR"
        for item in rows:
            published = timefields.external_stamp(item.get("published_utc"), source="polygon", field="published_utc")
            if published is None:
                continue  # no time zone: never assumed
            self.conn.execute(
                "INSERT OR IGNORE INTO polygon_feed VALUES (?,?,?,?,?,?,?)",
                (session_date, str(item.get("id")), published,
                 str(item.get("title") or ""), str(item.get("article_url") or ""),
                 json.dumps(item.get("tickers") or []), json.dumps(item, sort_keys=True)),
            )
        result = "OK" if rows else "EMPTY"
        self.conn.execute("INSERT OR REPLACE INTO polygon_feed_sessions VALUES (?,?,?,?,?)",
                          (session_date, result, len(rows), pages, _now()))
        self.conn.commit()
        return result

    def items(self, ticker: str, session_date: str) -> tuple[list[dict], str]:
        result = self.ensure(session_date)
        if result not in ("OK", "EMPTY"):
            return [], result
        out = [
            {"url": url, "title": title, "published": published, "provider": "polygon"}
            for title, url, published, tickers in self.conn.execute(
                "SELECT title, url, published_utc, tickers_json FROM polygon_feed WHERE session_date=?",
                (session_date,))
            if ticker in json.loads(tickers)
        ]
        return out, "OK" if out else "EMPTY"
