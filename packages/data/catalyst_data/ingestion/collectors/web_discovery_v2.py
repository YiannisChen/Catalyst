"""Retrieval-only, tier-aware web discovery with shared group-query caching."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.collectors import fetch, web_discovery_helpers, web_search
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.runtime_paths import (
    default_sec_tickers_path,
    runtime_store_path,
)

_EXTRA_DOMAINS = (
    "stockstory.org", "marketbeat.com", "tradingkey.com", "investors.com",
    "morningstar.com", "marketscreener.com", "tipranks.com", "nasdaq.com",
    "finance.yahoo.com",
)
INCLUDE_DOMAINS = tuple(dict.fromkeys((*web_search.FINANCE_DOMAINS, *_EXTRA_DOMAINS)))
_RELEVANCE_THRESHOLD = 0.12
_SPAM = re.compile(
    r"\b(?:investor alert|law firm|class action|securities fraud|rosen|pomerantz|"
    r"schall|levi\s*&\s*korsinsky|bragar|deadline|investors who lost)\b", re.I,
)
_MOVE_WORDS = re.compile(r"\b(?:rises?|falls?|jumps?|slides?|surges?|plunges?|gains?|drops?|rall(?:y|ies)|sinks?)\b", re.I)
_URL_DATE = re.compile(r"(?:^|/)(\d{4})/(\d{2})/(\d{2})(?:/|$)|(?:^|/)(\d{4}-\d{2}-\d{2})(?:/|$)")
_SECTOR_NAMES = {
    "XLK": "technology", "XLF": "financial", "XLE": "energy", "XLV": "healthcare",
    "XLI": "industrial", "XLB": "materials", "XLY": "consumer discretionary",
    "XLP": "consumer staples", "XLU": "utilities", "XLRE": "real estate",
    "XLC": "communication services",
}
_SECTOR_TERMS = {
    "XLK": ("technology", "tech stocks", "software", "semiconductor", "chip stocks"),
    "XLF": ("financial stocks", "banks", "bank stocks", "financial sector", "lenders"),
    "XLE": ("energy stocks", "energy sector", "oil stocks", "gas producers"),
    "XLV": ("healthcare stocks", "health care", "biotech", "drugmakers", "pharma"),
    "XLI": ("industrial stocks", "industrials", "manufacturers", "aerospace"),
    "XLB": ("materials stocks", "miners", "mining stocks", "chemicals"),
    "XLY": ("consumer discretionary", "retail stocks", "automakers", "restaurants"),
    "XLP": ("consumer staples", "food makers", "household products"),
    "XLU": ("utilities", "utility stocks", "electric utilities"),
    "XLRE": ("real estate", "reit", "property stocks"),
    "XLC": ("communication services", "telecom", "media stocks", "social media"),
}
_COMMODITIES = {
    "FCX": "copper", "NEM": "gold", "SCCO": "copper",
    **{ticker: "oil" for ticker in "XOM CVX EOG COP OXY SLB HAL DVN FANG MPC PSX VLO".split()},
    "ALB": "lithium",
}
_COMMODITY_TERMS = {
    "copper": ("copper", "copper prices", "copper miners"),
    "gold": ("gold", "gold prices", "bullion"),
    "oil": ("oil", "crude", "oil prices", "petroleum"),
    "lithium": ("lithium", "lithium prices", "battery metals"),
}
_MARKET_TERMS = (
    "stocks", "s&p 500", "dow", "nasdaq", "wall street", "futures", "yields",
    "fed", "tariffs", "oil", "gold", "copper",
)
_STATUS_NAMES = ("FULL_TEXT", "SPAM", "IRRELEVANT", "UNDATED", "OUT_OF_WINDOW", "UNFETCHED")
_COVERAGE_POLICY_VERSION = "web-discovery-v2"
_SAFE_SEARCH_ERRORS = {
    "daily_credit_limit", "tavily_invalid_response", "tavily_key_exhausted",
    "tavily_key_invalid", "tavily_key_missing", "tavily_keys_exhausted",
    "tavily_network_error", "web_search_failed",
}


_NAME_SUFFIXES = {"inc", "co", "corp", "corporation", "company", "companies", "holdings", "holding",
                  "ltd", "plc", "llc", "nv", "sa", "ag", "de", "incorporated", "limited", "the"}


def _clean_name(raw: str) -> str:
    """'BOEING CO' -> 'Boeing'; 'Tesla, Inc.' -> 'Tesla'; 'AT&T INC.' -> 'AT&T'."""
    words = [w for w in re.split(r"[\s,]+", raw.replace("/", " ").strip()) if w]
    while words and (re.sub(r"[^a-z]", "", words[-1].lower()) in _NAME_SUFFIXES or words[-1] == "&"):
        words.pop()
    if words and words[0].lower() == "the":
        words.pop(0)
    all_caps = raw.isupper()  # SEC titles are upper case; spec legal names are already cased
    out = []
    for word in words:
        if all_caps and not ("&" in word or any(ch.isdigit() for ch in word)):
            out.append(word.capitalize())
        else:
            out.append(word)
    return " ".join(out).strip(" .")


def _company(ticker: str) -> str:
    """Display name for queries and issuer matching.

    v1's universe spec names only 40 companies, so v1 searched and matched most tickers by the
    bare ticker ("Why did BA (BA) stock rise..."; "American Express" articles were IRRELEVANT for
    AXP). v2 falls back to the SEC company-tickers file, which covers every listed issuer.
    """
    ticker = ticker.upper()
    legal = web_discovery_helpers._company(ticker)
    if legal and legal != ticker:
        return _clean_name(legal)
    return _sec_names().get(ticker, ticker)


_SEC_NAMES: dict[str, str] | None = None


def _sec_names() -> dict[str, str]:
    global _SEC_NAMES
    if _SEC_NAMES is None:
        names: dict[str, str] = {}
        candidates = [Path(os.environ["CATALYST_NEWS_BODIES_DB"]).parent / "sec_company_tickers.json"] \
            if os.environ.get("CATALYST_NEWS_BODIES_DB") else []
        candidates.append(default_sec_tickers_path())
        for path in candidates:
            if path.is_file():
                for entry in json.loads(path.read_text()).values():
                    ticker = str(entry.get("ticker") or "").upper()
                    if ticker and ticker not in names:
                        names[ticker] = _clean_name(str(entry.get("title") or ticker))
                break
        _SEC_NAMES = names
    return _SEC_NAMES


def _connect(path: str | Path | None = None) -> sqlite3.Connection:
    db = Path(path).expanduser() if path else runtime_store_path("web_search_db")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS web_discovery_v2_queries (
            scope_key TEXT NOT NULL, session_date TEXT NOT NULL, family TEXT NOT NULL,
            query TEXT NOT NULL, searched_at TEXT NOT NULL, credits INTEGER NOT NULL,
            result_count INTEGER NOT NULL, pass INTEGER NOT NULL,
            PRIMARY KEY (scope_key, session_date, family, pass)
        );
        CREATE TABLE IF NOT EXISTS web_discovery_v2_rows (
            scope_key TEXT NOT NULL, session_date TEXT NOT NULL, family TEXT NOT NULL,
            canonical_url TEXT NOT NULL, url TEXT NOT NULL, publisher TEXT NOT NULL,
            publisher_family TEXT NOT NULL, published_at TEXT, date_source TEXT,
            title TEXT NOT NULL, body_text TEXT NOT NULL, body_source TEXT NOT NULL,
            html_sha256 TEXT, scope TEXT NOT NULL, relevance REAL NOT NULL,
            status TEXT NOT NULL, fetched_at TEXT NOT NULL,
            PRIMARY KEY (scope_key, session_date, family, canonical_url)
        );
        CREATE TABLE IF NOT EXISTS web_discovery_v2_case_links (
            case_key TEXT NOT NULL, ticker TEXT NOT NULL, session_date TEXT NOT NULL,
            scope_key TEXT NOT NULL, family TEXT NOT NULL,
            PRIMARY KEY (case_key, ticker, session_date, scope_key, family)
        );
        CREATE TABLE IF NOT EXISTS web_discovery_v2_request_cache (
            request_hash TEXT PRIMARY KEY, scope_key TEXT NOT NULL, session_date TEXT NOT NULL,
            family TEXT NOT NULL, pass INTEGER NOT NULL, query_spec_json TEXT NOT NULL,
            attempt_id TEXT NOT NULL, searched_at TEXT NOT NULL, result_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS web_discovery_v2_request_rows (
            request_hash TEXT NOT NULL, canonical_url TEXT NOT NULL,
            scope TEXT NOT NULL, relevance REAL NOT NULL,
            PRIMARY KEY (request_hash, canonical_url)
        );
        CREATE TABLE IF NOT EXISTS web_discovery_v2_request_cache_attempts (
            request_hash TEXT NOT NULL, attempt_id TEXT NOT NULL,
            scope_key TEXT NOT NULL, session_date TEXT NOT NULL, family TEXT NOT NULL,
            pass INTEGER NOT NULL, query_spec_json TEXT NOT NULL,
            searched_at TEXT NOT NULL, result_count INTEGER NOT NULL,
            PRIMARY KEY (request_hash, attempt_id)
        );
        CREATE TABLE IF NOT EXISTS web_discovery_v2_request_rows_attempts (
            request_hash TEXT NOT NULL, attempt_id TEXT NOT NULL,
            canonical_url TEXT NOT NULL, scope TEXT NOT NULL, relevance REAL NOT NULL,
            PRIMARY KEY (request_hash, attempt_id, canonical_url)
        );
        CREATE TABLE IF NOT EXISTS web_discovery_v2_plan_outcomes (
            outcome_id TEXT PRIMARY KEY, case_key TEXT NOT NULL, ticker TEXT NOT NULL,
            session_date TEXT NOT NULL, scope_key TEXT NOT NULL, family TEXT NOT NULL,
            pass INTEGER NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL, recorded_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS web_discovery_v2_plan_outcomes_no_update
        BEFORE UPDATE ON web_discovery_v2_plan_outcomes
        BEGIN SELECT RAISE(ABORT, 'plan outcomes are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS web_discovery_v2_plan_outcomes_no_delete
        BEFORE DELETE ON web_discovery_v2_plan_outcomes
        BEGIN SELECT RAISE(ABORT, 'plan outcomes are immutable'); END;
        CREATE INDEX IF NOT EXISTS idx_web_discovery_v2_case_links
            ON web_discovery_v2_case_links(case_key, session_date, scope_key, family);
    """)
    return conn


def _date_text(session_date: str) -> str:
    parsed = date.fromisoformat(session_date)
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _direction(value: Any, positive: str, negative: str) -> str:
    try:
        return positive if float(value) >= 0 else negative
    except (TypeError, ValueError):
        return positive


def queries_for_case(case: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return (scope_key, family, query) entries in stable, credit-aware order."""
    ticker = str(case["ticker"]).upper()
    tier = str(case.get("tier") or "T1").upper()
    if tier == "T1":
        return []
    company = _company(ticker)
    text_date = _date_text(str(case["session_date"]))
    rise_fall = _direction(case.get("return_pct"), "rise", "fall")
    result = []
    if tier == "T2":
        etf = str(case.get("sector_etf") or "").upper()
        sector = _SECTOR_NAMES.get(etf)
        if sector:
            result.append((f"SECTOR:{etf}", "sector",
                           f"Why did {sector} stocks {rise_fall} on {text_date}?"))
        market_word = _direction(case.get("spy_pct"), "rose", "fell")
        result.append(("MARKET", "market", f"Stock market today {text_date}: why stocks {market_word}"))
    # A commodity producer's move is often the commodity's move even when the tier is T3
    # (v6: FCX on copper was T3), so the shared commodity query runs for both tiers.
    commodity = _COMMODITIES.get(ticker)
    if commodity:
        result.append((f"COMMODITY:{commodity}", "commodity", f"{commodity} prices {text_date}"))
    result.append((ticker, "issuer_primary",
                   f"Why did {company} ({ticker}) stock {rise_fall} on {text_date}?"))
    if tier == "T3":
        result.append((ticker, "issuer_secondary", f"{company} shares "
                       f"{_direction(case.get('return_pct'), 'jump', 'drop')} {text_date}"))
    return result


def _case_question(case: dict[str, Any], scope_key: str) -> str:
    ticker = str(case["ticker"]).upper()
    day = _date_text(str(case["session_date"]))
    if scope_key.startswith("SECTOR:"):
        subject = _SECTOR_NAMES.get(scope_key.split(":", 1)[1], "market") + " stocks"
    elif scope_key == "MARKET":
        subject = "stocks"
    elif scope_key.startswith("COMMODITY:"):
        subject = scope_key.split(":", 1)[1] + " prices"
    else:
        subject = f"{_company(ticker)} {ticker} stock"
    direction = _direction(case.get("return_pct"), "rise", "fall")
    return f"why did {subject} {direction} on {day}"


def _score(title: str, body: str, question: str, published_at: str | None) -> float:
    """Lexical overlap over title plus lead, with small move/date evidence bonuses."""
    text = f"{title} {(body or '')[:600]}".casefold()
    terms = set(re.findall(r"[a-z0-9]+", question.casefold())) - {"why", "did", "on", "the", "a"}
    overlap = sum(1 for term in terms if term in text) / max(len(terms), 1)
    score = overlap * 0.75 + (0.15 if _MOVE_WORDS.search(text) else 0.0)
    if published_at and _date_mentioned(text, published_at):
        score += 0.10
    return round(min(1.0, score), 4)


def _date_mentioned(text: str, value: str) -> bool:
    try:
        day = date.fromisoformat(value[:10])
    except ValueError:
        return False
    return bool(re.search(rf"\b{re.escape(day.strftime('%B'))}\s+{day.day}(?:st|nd|rd|th)?[,]?\s+{day.year}\b", text, re.I)
                or day.isoformat() in text)


def _scope_for(ticker: str, company: str, scope_key: str, title: str, body: str) -> str:
    lead = " ".join(re.split(r"(?<=[.!?])\s+", body or "")[:2])
    if _SPAM.search(f"{title} {lead}"):
        return "spam"
    issuer = web_discovery_helpers._issuer_relevance_matcher(ticker, company)
    if issuer(title) or (len(re.findall(r"(?<![A-Za-z0-9])" + re.escape(company) + r"(?![A-Za-z0-9])", body or "", re.I)) >= 2):
        return "issuer"
    if scope_key.startswith("SECTOR:"):
        terms = _SECTOR_TERMS.get(scope_key.split(":", 1)[1], ())
    elif scope_key.startswith("COMMODITY:"):
        terms = _COMMODITY_TERMS.get(scope_key.split(":", 1)[1], ())
    elif scope_key == "MARKET":
        terms = _MARKET_TERMS
    else:
        return "irrelevant"
    searchable = f"{title} {lead}".casefold()
    return "group" if any(term in searchable for term in terms) else "irrelevant"


def _url_date(url: str) -> date | None:
    match = _URL_DATE.search(urlsplit(url).path)
    if not match:
        return None
    value = "-".join(match.groups()[:3]) if match.group(1) else match.group(4)
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _provider_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return timefields.external_date(value, source="tavily", field="published_date")


def _provider_stamp(value: Any) -> str | None:
    """A provider timestamp with a time of day, as UTC ISO; None for date-only values."""
    if not isinstance(value, str) or len(value.strip()) <= 10:
        return None
    return timefields.external_stamp(value.strip(), source="tavily", field="published_date")


def _same_session_wrap(published: str, session_date: str) -> bool:
    """True if a group wrap was published on the session day or before the next day's open."""
    if not published:
        return False
    day = published[:10]
    if day == session_date:
        return True
    if len(published) <= 10:
        return False
    from catalyst_data import trading_calendar
    sessions = trading_calendar.calendar_trading_days(session_date, (date.fromisoformat(session_date)
                                                      + __import__("datetime").timedelta(days=7)).isoformat())
    following = next((d for d in sessions if d > session_date), None)
    # 13:30 UTC is the 09:30 New York open in daylight time; a pre-open wrap in standard time
    # (14:30 UTC) is still about the prior session, so 14:30 is the bound.
    return following is not None and day == following and published[11:16] < "14:30"


def _date_in_window(value: date, start: str, end: str) -> bool:
    return date.fromisoformat(start) <= value <= date.fromisoformat(end)


def _status(scope: str, has_body: bool, published: date | None, start: str, end: str) -> str:
    if scope == "spam":
        return "SPAM"
    if scope == "irrelevant":
        return "IRRELEVANT"
    if published is None:
        return "UNDATED"
    if not _date_in_window(published, start, end):
        return "OUT_OF_WINDOW"
    if not has_body:
        return "UNFETCHED"
    return "FULL_TEXT"


def _search(client: Callable[..., dict[str, Any]], query: str, *, start: str, end: str,
            include_domains: tuple[str, ...] | None) -> dict[str, Any]:
    kwargs = {"topic": "news", "search_depth": "basic", "max_results": 10,
              "start": start, "end": end, "include_domains": include_domains,
              "include_raw_content": True}
    return client(query, None, **kwargs)


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _request_spec(*, query: str, scope_key: str, session: str, family: str,
                  pass_number: int, start: str, end: str,
                  include_domains: tuple[str, ...] | None) -> dict[str, Any]:
    return {
        "provider": "tavily",
        "adapter": "web_discovery_v2",
        "scope_key": scope_key,
        "session_date": session,
        "family": family,
        "pass": pass_number,
        "query": query,
        "start": start,
        "end": end,
        "topic": "news",
        "search_depth": "basic",
        "max_results": 10,
        "include_domains": list(include_domains) if include_domains is not None else [],
        "include_raw_content": True,
    }


def _request_identity(spec: dict[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(spec, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(f"{_COVERAGE_POLICY_VERSION}:{encoded}".encode()).hexdigest()
    return key, encoded


def _coverage_window(start: str, end: str) -> tuple[str, str]:
    beginning = datetime.combine(date.fromisoformat(start), time.min, timezone.utc)
    until = datetime.combine(date.fromisoformat(end) + timedelta(days=1), time.min, timezone.utc)
    return (beginning.isoformat(timespec="microseconds"), until.isoformat(timespec="microseconds"))


def _start_search_attempt(conn: sqlite3.Connection, *, spec: dict[str, Any],
                          scope_key: str, session: str, family: str,
                          start: str, end: str) -> coverage.Attempt:
    requested_from, requested_until = _coverage_window(start, end)
    subject_id = f"{scope_key}:{session}:{family}:pass-{spec['pass']}"
    return coverage.start_attempt(
        conn,
        logical_task_id=f"web-discovery-v2:{subject_id}",
        attempt_id=uuid4().hex,
        source_id="daily:discovery_v2",
        product="search",
        subject_kind="query",
        subject_id=subject_id,
        query_spec=spec,
        coverage_policy_version=_COVERAGE_POLICY_VERSION,
        requested_from=requested_from,
        requested_until=requested_until,
    )


def _record_search_state(conn: sqlite3.Connection, *, attempt: coverage.Attempt,
                         status: str, reason: str | None = None) -> None:
    coverage.record_coverage(
        conn,
        coverage_id=uuid4().hex,
        attempt_id=attempt.attempt_id,
        status=status,
        reason=reason,
        recorded_at=_observed_at(),
        # Tavily is capped and does not expose an exhaustive count/cursor. These
        # facts describe an observed response, not complete source enumeration.
        coverage_from=None,
        coverage_until=None,
        enumeration_complete=None,
    )


def _cached_request(conn: sqlite3.Connection, *, request_hash: str,
                    query_spec_json: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT q.* FROM web_discovery_v2_request_cache_attempts q
           JOIN source_coverage_attempts a ON a.attempt_id=q.attempt_id
           JOIN source_coverage_facts f ON f.attempt_id=a.attempt_id
           WHERE q.request_hash=? AND q.query_spec_json=a.query_spec_json
             AND q.query_spec_json=? AND f.status IN ('OK','EMPTY')
             AND a.attempt_seq=(SELECT MAX(a_latest.attempt_seq)
                                FROM source_coverage_attempts a_latest
                                WHERE a_latest.partition_id=a.partition_id)
             AND f.state_seq=(SELECT MAX(f_latest.state_seq)
                              FROM source_coverage_facts f_latest
                              WHERE f_latest.attempt_id=f.attempt_id)
           ORDER BY f.state_seq DESC LIMIT 1""",
        (request_hash, query_spec_json),
    ).fetchone()


def _validate_search_results(payload: Any) -> list[dict[str, Any]]:
    if (not isinstance(payload, dict) or "results" not in payload
            or not isinstance(payload["results"], list)):
        raise web_search.WebSearchError("tavily_invalid_response")
    if "error" in payload and payload["error"] not in (None, "", False):
        raise web_search.WebSearchError("tavily_invalid_response")
    results = payload["results"]
    for item in results:
        if not isinstance(item, dict):
            raise web_search.WebSearchError("tavily_invalid_response")
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            raise web_search.WebSearchError("tavily_invalid_response")
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise web_search.WebSearchError("tavily_invalid_response")
        for field in ("title", "content", "raw_content", "published_date"):
            if field in item and item[field] is not None and not isinstance(item[field], str):
                raise web_search.WebSearchError("tavily_invalid_response")
    return results


def _record_plan_outcome(conn: sqlite3.Connection, *, case_key: str, ticker: str,
                         session: str, scope_key: str, family: str, pass_number: int,
                         outcome: str, reason: str, evidence: dict[str, Any]) -> None:
    evidence_json = json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    identity = json.dumps(
        {"case_key": case_key, "ticker": ticker, "session": session,
         "scope_key": scope_key, "family": family, "pass": pass_number,
         "outcome": outcome, "reason": reason, "evidence": evidence},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    )
    outcome_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT OR IGNORE INTO web_discovery_v2_plan_outcomes
           (outcome_id,case_key,ticker,session_date,scope_key,family,pass,outcome,reason,
            evidence_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (outcome_id, case_key, ticker, session, scope_key, family, pass_number,
         outcome, reason, evidence_json, _observed_at()),
    )
    conn.commit()


def _has_search_key() -> bool:
    return bool(web_discovery_helpers.tavily_pool.keys_from_env())


def _safe_search_error(exc: web_search.WebSearchError) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code in _SAFE_SEARCH_ERRORS else "web_search_failed"


def _issue_search(conn: sqlite3.Connection, *, case: dict[str, Any],
                  scope_key: str, family: str, query: str, pass_number: int,
                  start: str, end: str, include_domains: tuple[str, ...] | None,
                  search_fn: Callable[..., dict[str, Any]] | None, fetcher: Any,
                  max_credits: int, used: int,
                  db_path: str | Path | None) -> tuple[int, sqlite3.Row | None]:
    session = str(case["session_date"])
    spec = _request_spec(query=query, scope_key=scope_key, session=session,
                         family=family, pass_number=pass_number, start=start,
                         end=end, include_domains=include_domains)
    request_hash, query_spec_json = _request_identity(spec)
    cached = _cached_request(conn, request_hash=request_hash, query_spec_json=query_spec_json)
    if cached is not None:
        return used, cached

    if used >= max_credits and pass_number == 1:
        _record_plan_outcome(
            conn, case_key=str(case.get("case_key") or case["ticker"]),
            ticker=str(case["ticker"]).upper(), session=session,
            scope_key=scope_key, family=family, pass_number=pass_number,
            outcome="NOT_EVALUATED", reason="deferred", evidence={"request_hash": request_hash},
        )
        return used, None

    attempt = _start_search_attempt(
        conn, spec=spec, scope_key=scope_key, session=session, family=family,
        start=start, end=end,
    )
    conn.commit()
    if used >= max_credits:
        _record_search_state(conn, attempt=attempt, status="NOT_FETCHED", reason="deferred")
        conn.commit()
        return used, None
    if search_fn is None and not _has_search_key():
        _record_search_state(conn, attempt=attempt, status="NOT_FETCHED", reason="DISABLED")
        conn.commit()
        return used, None

    used += 1
    try:
        if search_fn is None:
            # Keep v1's key-pool behavior and raw-content request while adding v2's query parameters.
            def keyed(query_text, key, *, start, end, include_raw_content=False):
                return _tavily_search_v2(query_text, key, start=start, end=end,
                                         include_domains=include_domains)

            payload = web_discovery_helpers._call_tavily(
                keyed, query, api_key=None, db_path=db_path,
                start=start, end=end, include_raw_content=True,
            )
        else:
            payload = _search(search_fn, query, start=start, end=end,
                              include_domains=include_domains)
        observed_at = _observed_at()
        results = _validate_search_results(payload)
        inserted_rows = _insert_results(
            conn, case=case, scope_key=scope_key, family=family,
            results=results, fetcher=fetcher, fetched_at=observed_at,
            start=start, end=end,
        )
        conn.execute("INSERT OR IGNORE INTO web_discovery_v2_queries VALUES (?,?,?,?,?,?,?,?)",
                     (scope_key, session, family, query, observed_at, 1, len(results), pass_number))
        conn.execute(
            """INSERT INTO web_discovery_v2_request_cache_attempts
               (request_hash,attempt_id,scope_key,session_date,family,pass,query_spec_json,searched_at,result_count)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (request_hash, attempt.attempt_id, scope_key, session, family, pass_number, query_spec_json,
             observed_at, len(results)),
        )
        conn.executemany(
            "INSERT INTO web_discovery_v2_request_rows_attempts VALUES (?,?,?,?,?)",
            [(request_hash, attempt.attempt_id, canonical_url, scope, relevance)
             for canonical_url, scope, relevance in inserted_rows],
        )
        _record_search_state(conn, attempt=attempt, status="EMPTY" if not results else "OK")
        conn.commit()
        return used, _cached_request(conn, request_hash=request_hash,
                                     query_spec_json=query_spec_json)
    except Exception as exc:
        conn.rollback()
        _record_search_state(conn, attempt=attempt, status="ERROR", reason="ERROR")
        conn.commit()
        try:
            setattr(exc, "credits_used", used)
        except Exception:
            pass
        raise


def _tavily_search_v2(query: str, api_key: str, *, start: str, end: str,
                      include_domains: tuple[str, ...] | None) -> dict[str, Any]:
    """v2 request settings; kept local so v1's five-result contract remains unchanged."""
    try:
        response = web_search.requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "topic": "news", "search_depth": "basic", "max_results": 10,
                  "start_date": start, "end_date": end,
                  "include_domains": list(include_domains) if include_domains is not None else [],
                  "include_raw_content": True}, timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except web_search.requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None) or locals().get("response"), "status_code", None)
        if status in (401, 403):
            raise web_search.WebSearchError("tavily_key_invalid") from None
        if status in (429, 432):
            raise web_search.WebSearchError("tavily_key_exhausted") from None
        raise web_search.WebSearchError("tavily_network_error") from None
    except (web_search.requests.RequestException, ValueError):
        raise web_search.WebSearchError("tavily_network_error") from None
    if not isinstance(payload, dict) or "results" not in payload or not isinstance(payload["results"], list):
        raise web_search.WebSearchError("tavily_invalid_response")
    return payload


def _insert_results(conn: sqlite3.Connection, *, case: dict[str, Any], scope_key: str,
                    family: str, results: list[dict[str, Any]], fetcher: Any,
                    fetched_at: str, start: str, end: str) -> list[tuple[str, str, float]]:
    ticker = str(case["ticker"]).upper()
    company = _company(ticker)
    seen = set()
    pending: list[tuple] = []
    urls, firsts = [], set()  # the URLs the loop below fetches: the first result per canonical URL
    for result in results:
        url = str(result.get("url") or "").strip()
        if url and web_discovery_helpers._canonical_url(url) not in firsts:
            firsts.add(web_discovery_helpers._canonical_url(url))
            urls.append(url)
    fetcher = fetch.PrefetchedFetcher(fetcher, urls)
    for result in results:
        url = str(result.get("url") or "").strip()
        if not url:
            continue
        canonical = web_discovery_helpers._canonical_url(url)
        if canonical in seen:
            continue
        seen.add(canonical)
        title = " ".join(str(result.get("title") or "").split())
        publisher = str(web_search.publisher_for_url(url) or urlsplit(url).hostname or "")
        final_url = url
        html = ""
        body = ""
        body_source = "fetch_unavailable"
        html_sha = None
        page_date = None
        page_stamp = None
        fetched_page = False
        row_fetched_at = fetched_at
        try:
            page = fetcher.fetch_page(url)
            if page:
                final_url, html = page
                fetched_page = True
                # A full-text row is observed when this page response completes,
                # independently for each URL. Inline provider content instead
                # retains the search-response observation time below.
                row_fetched_at = _observed_at()
                body_source = "fetch"
                from catalyst_data.ingestion.collectors import headlines as _headlines
                from catalyst_data.ingestion.storage import page_versions
                page_versions.record("web_discovery", url, final_url=final_url, page_html=html, subject=ticker,
                                     cutoff=_headlines.session_window(str(case["session_date"]))[1])
                title = web_search._page_title(html, title)
                _, published, _ = web_search.classify_page_time(
                    html,
                    datetime.combine(date.fromisoformat(start), time.min, timezone.utc),
                    datetime.combine(date.fromisoformat(end), time.max, timezone.utc),
                )
                if published is not None:
                    page_date = published.date()
                    page_stamp = published.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                html_sha = hashlib.sha256(html.encode()).hexdigest()
                body = fetch.extract(html, title) or ""
                if len(body) < 400:
                    body = web_discovery_helpers._jsonld_article_body(html, title)
                    if body:
                        body_source = "jsonld_articleBody"
                publisher = web_search.publisher_for_url(final_url) or publisher
        except Exception:
            if not fetched_page:
                body_source = "fetch_unavailable"
        if not body:
            raw = str(result.get("raw_content") or "")
            cleaned = web_discovery_helpers._clean_tavily_raw(raw)
            if cleaned:
                confirmed, _ = web_discovery_helpers._confirmed_raw_date(raw, result.get("published_date"))
                # Fetched page evidence may support a hybrid row; raw-only rows need a dateline.
                if fetched_page or confirmed is not None:
                    body = cleaned
                    body_source = "tavily_raw_page_dated" if fetched_page else "tavily_raw"
                    if not html_sha:
                        html_sha = hashlib.sha256(body.encode()).hexdigest()
        selected_date = page_date
        stamp = page_stamp
        date_source = "page" if page_date else None
        if selected_date is None:
            selected_date = _url_date(final_url or url)
            date_source = "url" if selected_date else None
        if selected_date is None:
            selected_date = _provider_date(result.get("published_date"))
            date_source = "provider" if selected_date else None
            stamp = _provider_stamp(result.get("published_date")) if selected_date else None
        scope = _scope_for(ticker, company, scope_key, title, body)
        relevance = _score(title, body, _case_question(case, scope_key),
                           selected_date.isoformat() if selected_date else None)
        status = _status(scope, bool(body), selected_date, start, end)
        pending.append(
            (scope_key, str(case["session_date"]), family, web_discovery_helpers._canonical_url(final_url),
             final_url, publisher, web_discovery_helpers._publisher_family(final_url, publisher),
             (stamp or selected_date.isoformat()) if selected_date else None, date_source, title, body, body_source,
             html_sha, scope, relevance, status, row_fetched_at))
    # Pages are fetched before any write so a slow fetch never holds the sqlite write lock
    # (parallel discovery processes share one store).
    conn.executemany("""INSERT OR IGNORE INTO web_discovery_v2_rows
        (scope_key,session_date,family,canonical_url,url,publisher,publisher_family,published_at,
         date_source,title,body_text,body_source,html_sha256,scope,relevance,status,fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", pending)
    return [(row[3], row[13], row[14]) for row in pending]


def discover_v2(case: dict[str, Any], *, search_fn: Callable[..., dict[str, Any]] | None = None,
                fetcher: Any | None = None, db_path: str | Path | None = None,
                max_credits: int = 4, now: datetime | None = None) -> dict[str, Any]:
    """Search only missing scope/family/pass keys and link the resulting rows to this case."""
    if isinstance(max_credits, bool) or not isinstance(max_credits, int) or max_credits < 0:
        raise ValueError("max_credits must be a non-negative integer")
    case = dict(case)
    case["ticker"] = str(case["ticker"]).upper()
    case_key = str(case.get("case_key") or case["ticker"])
    session = str(case["session_date"])
    start, end = web_discovery_helpers._publication_bounds(session, now)
    client = search_fn or web_search._tavily_search
    fetcher = fetcher or fetch.Fetcher()
    conn = _connect(db_path)
    from catalyst_data.ingestion.storage import page_versions
    if db_path is not None:
        page_versions.use_store_dir(db_path)
    used = 0
    scopes = []
    try:
        coverage.ensure_schema(conn)
        conn.commit()
        planned = queries_for_case(case)
        if str(case.get("tier") or "T1").upper() == "T1" and not planned:
            _record_plan_outcome(
                conn, case_key=case_key, ticker=case["ticker"], session=session,
                scope_key=case["ticker"], family="tier1_no_search", pass_number=1,
                outcome="NOT_APPLICABLE", reason="tier1_no_search_scopes",
                evidence={"tier": "T1", "planned_scope_count": 0},
            )
        issuer_requests: dict[str, sqlite3.Row | None] = {}
        for scope_key, family, query in planned:
            conn.execute("INSERT OR IGNORE INTO web_discovery_v2_case_links VALUES (?,?,?,?,?)",
                         (case_key, case["ticker"], session, scope_key, family))
            conn.commit()
            if (scope_key, family) not in scopes:
                scopes.append((scope_key, family))
            used, cached = _issue_search(
                conn, case=case, scope_key=scope_key, family=family, query=query,
                pass_number=1, start=start, end=end, include_domains=INCLUDE_DOMAINS,
                search_fn=search_fn, fetcher=fetcher, max_credits=max_credits,
                used=used, db_path=db_path,
            )
            if family.startswith("issuer_"):
                issuer_requests[family] = cached
        issuer_families = [(scope, family, query) for scope, family, query in planned
                           if scope == case["ticker"] and family.startswith("issuer_")]
        if issuer_families:
            scope_key, family, query = issuer_families[0]
            pass_one = issuer_requests.get(family)
            relevant = []
            if pass_one is not None:
                relevant = [dict(row) for row in conn.execute(
            """SELECT canonical_url,scope,relevance FROM web_discovery_v2_request_rows_attempts
                       WHERE request_hash=? AND attempt_id=? AND scope='issuer' AND relevance>=?
                       ORDER BY relevance DESC,canonical_url""",
                    (pass_one["request_hash"], pass_one["attempt_id"], _RELEVANCE_THRESHOLD),
                )]
            if relevant:
                _record_plan_outcome(
                    conn, case_key=case_key, ticker=case["ticker"], session=session,
                    scope_key=scope_key, family=family, pass_number=2,
                    outcome="NOT_APPLICABLE", reason="pass1_relevant_issuer_result",
                    evidence={"request_hash": pass_one["request_hash"], "rows": relevant},
                )
            elif pass_one is None:
                _record_plan_outcome(
                    conn, case_key=case_key, ticker=case["ticker"], session=session,
                    scope_key=scope_key, family=family, pass_number=2,
                    outcome="NOT_EVALUATED", reason="pass1_not_completed", evidence={},
                )
            else:
                used, _ = _issue_search(
                    conn, case=case, scope_key=scope_key, family=family, query=query,
                    pass_number=2, start=start, end=end, include_domains=None,
                    search_fn=search_fn, fetcher=fetcher, max_credits=max_credits,
                    used=used, db_path=db_path,
                )
        counts = {name: 0 for name in _STATUS_NAMES}
        for row in conn.execute("""SELECT r.status,COUNT(*) FROM web_discovery_v2_rows r
            JOIN web_discovery_v2_case_links l USING(scope_key,session_date,family)
            WHERE l.case_key=? AND l.ticker=? AND l.session_date=? GROUP BY r.status""",
            (case_key, case["ticker"], session)):
            counts[row[0]] = row[1]
        return {"case_key": case_key, "ticker": case["ticker"], "session_date": session,
                "credits_used": used, "scopes": [{"scope_key": scope, "family": family}
                                                  for scope, family in scopes],
                "counts": counts}
    except Exception as exc:
        used = max(used, int(getattr(exc, "credits_used", 0) or 0))
        try:
            setattr(exc, "credits_used", used)
        except Exception:
            pass
        raise
    finally:
        conn.close()


def rows_for_case(case_key: str, *, db_path: str | Path | None = None,
                  statuses: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        sql = """SELECT DISTINCT r.* FROM web_discovery_v2_rows r
            JOIN web_discovery_v2_case_links l USING(scope_key,session_date,family)
            WHERE l.case_key=?"""
        params: list[Any] = [case_key]
        if statuses:
            sql += " AND r.status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        sql += " ORDER BY r.relevance DESC, r.published_at DESC, r.canonical_url"
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def cached_rows_for_ticker_session(
    ticker: str, session_date: str, *, db_path: str | Path | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Return cached issuer/group FULL_TEXT rows linked to a ticker and date.

    Runtime requests do not carry the discovery case key, so resolve the shared
    scope links by ticker and session instead. Canonical URLs are de-duplicated
    after relevance and publication-time ordering, before applying the limit.
    """
    if limit <= 0:
        return []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT DISTINCT r.* FROM web_discovery_v2_rows r
               JOIN web_discovery_v2_case_links l
                 ON l.scope_key=r.scope_key AND l.session_date=r.session_date
                AND l.family=r.family
               WHERE l.ticker=? AND l.session_date=?
                 AND r.status='FULL_TEXT' AND r.scope IN ('issuer','group')
               ORDER BY r.relevance DESC, r.published_at DESC, r.canonical_url""",
            (ticker.upper(), session_date),
        ).fetchall()
        selected: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for row in rows:
            value = dict(row)
            canonical_url = str(value.get("canonical_url") or "").rstrip("/").casefold()
            if not canonical_url or canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)
            published = str(value.get("published_at") or "")
            if value.get("scope") == "group" and not _same_session_wrap(published, session_date):
                # A market/sector/commodity wrap describes the day it was written for; a wrap from
                # another day in the D-3..D+3 window reports another session (v6 dev: XOM cited an
                # April 22 oil wrap for an April 17 move). Issuer recaps keep the full window.
                continue
            if len(published) == 10:
                # A date-only publication (URL or provider date) has no time of day; treat it as
                # end of day so it can never pass as published before the move.
                value["published_at"] = f"{published}T23:59:59Z"
            selected.append(value)
            if len(selected) >= limit:
                break
        return selected
    finally:
        conn.close()


def _all_tiers(cases: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    from catalyst_data.ingestion.ops.daily import Paths
    paths = Paths.from_env()
    from catalyst_data.ingestion.ops.daily import publish_paths_environment
    publish_paths_environment(paths)
    peer_map = Path(os.environ.get("CATALYST_PEER_MAP") or "data/manifests/peer_map_v1.json")
    result = {}
    for session in sorted({str(row.get("session_date") or row.get("date")) for row in cases}):
        command = [sys.executable, "-m", "catalyst_agents.runtime.web_tier_candidates",
                   "--market-db", str(paths.market_db), "--peer-map", str(peer_map),
                   "--session-date", session, "--all-tiers"]
        if paths.snapshot_db:
            command.extend(("--snapshot-db", str(paths.snapshot_db)))
        proc = subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
        for row in json.loads(proc.stdout):
            result[(session, str(row["ticker"]).upper())] = str(row["tier"])
    return result


def _case_payload(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    rows = payload.get("candidates", []) if isinstance(payload, dict) else payload
    return [dict(row, case_key=str(row.get("case_id") or row["ticker"])) for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--cases-file", required=True, type=Path)
    run.add_argument("--case", action="append")
    run.add_argument("--max-credits", type=int, default=4)
    run.add_argument("--max-credits-total", type=int)
    run.add_argument("--tiers-from", type=Path)
    dump = sub.add_parser("dump")
    dump.add_argument("--cases-file", required=True, type=Path)
    dump.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    cases = _case_payload(args.cases_file)
    if args.command == "dump":
        with args.out.open("w") as out:
            for case in cases:
                key = str(case.get("case_id") or case["ticker"])
                rows = rows_for_case(key)
                out.write(json.dumps({"case_key": key, "rows": [{
                    "status": row["status"], "scope": row["scope"], "relevance": row["relevance"],
                    "publisher": row["publisher"], "published_at": row["published_at"],
                    "date_source": row["date_source"], "title": row["title"],
                    "lead": row["body_text"][:300], "family": row["family"],
                    "scope_key": row["scope_key"],
                } for row in rows]}, sort_keys=True) + "\n")
        return 0
    if args.case:
        selected = set(args.case)
        cases = [row for row in cases if str(row.get("case_id") or row["ticker"]) in selected]
    if args.tiers_from:
        tier_payload = json.loads(args.tiers_from.read_text())
        for row in cases:
            row["tier"] = tier_payload.get(str(row.get("case_id") or row["ticker"]), "T1")
    else:
        try:
            tiers = _all_tiers(cases)
            for row in cases:
                row["tier"] = tiers.get((str(row.get("session_date") or row.get("date")), str(row["ticker"]).upper()), "T1")
        except Exception as exc:
            error = "tier_candidates_failed"
            for row in cases:
                print(json.dumps({"case_key": row["case_key"], "status": "FAILED",
                                  "error": error, "credits_used": 0}, sort_keys=True))
            return 0
    total_cap = args.max_credits_total if args.max_credits_total is not None else 2**31 - 1
    total_used = 0
    for row in cases:
        remaining = min(args.max_credits, max(0, total_cap - total_used))
        case_used = 0
        result = None
        for attempt in range(3):
            try:
                result = discover_v2(row, max_credits=max(0, remaining - case_used))
                result["credits_used"] = int(result.get("credits_used", 0)) + case_used
                break
            except web_search.WebSearchError as exc:
                case_used += int(getattr(exc, "credits_used", 0) or 0)
                safe_error = _safe_search_error(exc)
                if safe_error != "tavily_network_error" or attempt == 2:
                    result = {"case_key": row["case_key"], "status": "FAILED", "error": safe_error, "credits_used": case_used}
                    break
                if case_used >= remaining:
                    result = {"case_key": row["case_key"], "status": "FAILED",
                              "error": safe_error, "credits_used": case_used}
                    break
                _time.sleep(5 * (attempt + 1))
            except Exception as exc:
                case_used += int(getattr(exc, "credits_used", 0) or 0)
                result = {"case_key": row["case_key"], "status": "FAILED",
                          "error": "web_search_failed", "credits_used": case_used}
                break
        if result is None:
            result = {"case_key": row["case_key"], "status": "FAILED", "error": "unknown", "credits_used": case_used}
        total_used += int(result.get("credits_used", 0))
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
