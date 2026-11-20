"""Item 2.02 (results of operations) 8-K text for the investigator's ``earnings_event`` tool.

The SEC submissions index gives each 8-K's acceptance time and item list, so an earnings release is
timed by acceptance (never by filing date or a provider's report date). Exhibit text is fetched once
and cached as JSON, so replays read the cache and make no requests.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from catalyst_data.ingestion.collectors import sec
from catalyst_data.ingestion.structured_types import FactRow
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.runtime_paths import runtime_store_path

EARNINGS_ITEM = "2.02"


def _cache_dir() -> Path:
    return runtime_store_path("sec_earnings_cache")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(ticker: str, filing: dict, body: dict) -> FactRow:
    accepted = timefields.stored(filing["accepted_at"])
    url = body["url"]
    evidence = {
        "query_kind": "sec_8k", "canonical_url": url, "url": url, "title": body["title"],
        "body_text": body["text"], "published_at": filing["accepted_at"],
        "publisher": "SEC filing", "publisher_family": "sec.gov",
        "html_sha256": body["body_sha256"], "body_source": "sec_exhibit", "content_state": "FULL_TEXT",
        "source_class": "issuer_disclosure",
    }
    return FactRow(kind="sec_8k", subject=ticker, event_at=accepted, available_at=accepted,
                   precision="SECOND", source="sec",
                   payload={"accession": filing["accession"], "items": filing["items"], "url": url,
                            "title": body["title"], "evidence_item": evidence})


def find_earnings_filings(
    ticker: str, start: datetime, end: datetime, *,
    http_get: Callable | None = None, user_agent: str | None = None,
    cache_dir: str | Path | None = None, offline: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> tuple[FactRow, ...]:
    """Item 2.02 8-Ks accepted in ``(start, end]`` with their exhibit text, one row per document."""
    ticker = ticker.upper()
    folder = Path(cache_dir) if cache_dir is not None else _cache_dir()
    key = hashlib.sha256(f"{ticker}|{_iso(start)}|{_iso(end)}".encode()).hexdigest()[:24]
    path = folder / ticker / f"{key}.json"
    if path.exists():
        cached = json.loads(path.read_text())
    elif offline:
        return ()
    else:
        cached = _fetch(ticker, start, end, http_get=http_get, user_agent=user_agent, sleep=sleep)
        if cached is None:
            return ()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cached, sort_keys=True))
    return tuple(_row(ticker, entry["filing"], body) for entry in cached for body in entry["bodies"])


def _fetch(ticker: str, start: datetime, end: datetime, *, http_get: Callable | None,
           user_agent: str | None, sleep: Callable[[float], None] | None) -> list[dict] | None:
    """Fetch and return the cache payload, or ``None`` when a request failed (nothing is cached)."""
    user_agent = user_agent or os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        return None
    if http_get is None:
        import requests
        http_get = requests.get
    if sleep is None:
        import time
        sleep = time.sleep
    cik = sec.cik_for(ticker)
    if cik is None:
        return []

    def paced(url, **kwargs):
        sleep(sec.SEC_INTERVAL_S)
        return http_get(url, **kwargs)

    try:
        filings = sec.list_8k(cik, _iso(start), _iso(end), http_get=paced, user_agent=user_agent)
    except Exception:
        return None
    out = []
    for filing in filings:
        if EARNINGS_ITEM not in filing["items"]:
            continue
        rows, failed = sec.rows_for_filing(cik, ticker, filing, http_get=paced, user_agent=user_agent,
                                           window=(_iso(start), _iso(end)))
        if failed and not rows:
            return None
        if not rows and not failed:
            continue  # outside (start, end] by EDGAR's acceptance time (the JSON's may be 4-5 h late)
        out.append({"filing": filing, "bodies": [
            {"url": row["url"], "title": row["title"], "text": row["text"], "body_sha256": row["body_sha256"]}
            for row in rows]})
    return out


__all__ = ["EARNINGS_ITEM", "find_earnings_filings"]
