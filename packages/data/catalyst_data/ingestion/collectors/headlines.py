"""M9 headline discovery from Finnhub, Polygon, and commodity proxy rules."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

from catalyst_data.canonical.temporal import build_temporal_identity, utc_iso_z
from catalyst_data.ingestion import ratelimit
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval import web_bodies
from catalyst_data.retrieval.web_bodies import COMMODITY_TERMS, subject_matcher
from catalyst_data.storage.runtime_paths import default_sec_tickers_path

REPO = Path(__file__).resolve().parents[5]
MAX_HEADLINES = 30
MAX_PEER_HEADLINES = 10
POLYGON_INTERVAL_S = 12.5  # kept for callers; the shared budget is ratelimit.POLYGON
http_get = requests.get
sleep = time.sleep
_SEC_NAMES: dict[str, str] = {}


def session_window(session_date: str) -> tuple[str, str]:
    identity = build_temporal_identity(session_date)
    return utc_iso_z(identity.information_window_start_at), utc_iso_z(identity.cutoff_at)


def _names(ticker: str, title: str) -> bool:
    if not _SEC_NAMES:
        data_path = web_bodies.name_map_path() or default_sec_tickers_path(REPO)
        data = json.loads(data_path.read_text())
        _SEC_NAMES.update({v["ticker"]: str(v["title"]) for v in data.values()})
    company = _SEC_NAMES.get(ticker, "")
    first = re.sub(r"[^a-z]", "", company.split()[0].lower()) if company else ""
    if re.search(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])", title):
        return True
    return len(first) >= 4 and re.search(rf"\b{re.escape(first)}\b", title, re.I) is not None


def finnhub_headlines_with_status(ticker: str, start: str, end: str) -> tuple[list[dict], str]:
    key = os.environ.get("FINNHUB_API_KEY", "").strip().strip('"')
    if not key:
        return [], "DISABLED"
    resp = None
    for attempt in range(3):
        ratelimit.FINNHUB.acquire()
        try:
            resp = http_get(
                "https://finnhub.io/api/v1/company-news",
                params={"symbol": ticker, "from": start[:10], "to": end[:10], "token": key},
                timeout=30,
            )
        except requests.RequestException:
            return [], "ERROR"
        if resp.status_code != 429:
            break
        ratelimit.FINNHUB.backoff(attempt)
    if resp.status_code != 200:
        return [], "ERROR"
    out = []
    for item in resp.json():
        when = timefields.external_stamp(item.get("datetime"), source="finnhub", field="datetime")
        if when is None:
            continue  # no time: cannot be placed in a session window
        out.append({"url": item.get("url", ""), "title": item.get("headline", ""),
                    "published": when, "provider": "finnhub"})
    return out, "OK" if out else "EMPTY"


def finnhub_headlines(ticker: str, start: str, end: str) -> list[dict]:
    return finnhub_headlines_with_status(ticker, start, end)[0]


def polygon_headlines_with_status(ticker: str, start: str, end: str) -> tuple[list[dict], str]:
    key = os.environ.get("POLYGON_API_KEY", "").strip().strip('"')
    if not key:
        return [], "DISABLED"
    resp = None
    for attempt in range(3):
        ratelimit.POLYGON.acquire()  # was a fixed 12.5 s sleep before every call, the first one included
        try:
            resp = http_get(
                "https://api.polygon.io/v2/reference/news",
                params={"ticker": ticker, "published_utc.gte": start, "published_utc.lte": end,
                        "limit": 100, "order": "asc", "apiKey": key},
                timeout=30,
            )
        except requests.RequestException:
            return [], "ERROR"
        if resp.status_code != 429:
            break
        ratelimit.POLYGON.backoff(attempt)
    if resp.status_code != 200:
        return [], "ERROR"
    from catalyst_data.ingestion.time.times import stamp, utc_or_none
    out = [
        {"url": item.get("article_url", ""), "title": item.get("title", ""),
         "published": stamp(utc_or_none(item["published_utc"])), "provider": "polygon"}
        for item in resp.json().get("results", [])
        if utc_or_none(item.get("published_utc")) is not None  # no time zone: skipped, never assumed
    ]
    return out, "OK" if out else "EMPTY"


def polygon_headlines(ticker: str, start: str, end: str) -> list[dict]:
    return polygon_headlines_with_status(ticker, start, end)[0]


def select_with_status(ticker: str, start: str, cutoff: str, limit: int) -> tuple[list[dict], str]:
    if ticker in COMMODITY_TERMS:
        headlines, finn_status = finnhub_headlines_with_status(ticker, start, cutoff)
        statuses = [finn_status]
    else:
        finn, finn_status = finnhub_headlines_with_status(ticker, start, cutoff)
        polygon, polygon_status = polygon_headlines_with_status(ticker, start, cutoff)
        headlines, statuses = finn + polygon, [finn_status, polygon_status]
    return pick(ticker, headlines, statuses, start, cutoff, limit)


def pick(
    ticker: str, headlines: list[dict], statuses: list[str], start: str, cutoff: str, limit: int,
) -> tuple[list[dict], str]:
    """The session's headlines that name the subject: newest first, deduplicated by title."""
    if ticker in COMMODITY_TERMS:
        about = subject_matcher(ticker)
    else:
        about = lambda title: _names(ticker, title)
    seen, picked = set(), []
    for item in sorted(headlines, key=lambda i: i["published"], reverse=True):
        if not (start < item["published"] <= cutoff) or not item["url"]:
            continue
        key = item["title"].strip().lower()
        if key in seen or not about(item["title"]):
            continue
        seen.add(key)
        picked.append({**item, "ticker": ticker})
        if len(picked) >= limit:
            break
    status = (
        "OK" if "OK" in statuses else "ERROR" if "ERROR" in statuses
        else "DISABLED" if all(value == "DISABLED" for value in statuses) else "EMPTY"
    )
    return picked, status


def select(ticker: str, start: str, cutoff: str, limit: int) -> list[dict]:
    return select_with_status(ticker, start, cutoff, limit)[0]
