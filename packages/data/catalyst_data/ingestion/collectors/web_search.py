"""User-initiated Tavily lead search, kept outside attribution and eval stores."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from catalyst_data.canonical.temporal import build_temporal_identity
from catalyst_data.ingestion import tavily_ledger, tavily_pool
from catalyst_data.ingestion.collectors import benzinga, fetch
from catalyst_data.ingestion.time import timefields
from catalyst_data.read.web_leads import issuer_matcher as issuer_matcher
from catalyst_data.retrieval.web_bodies import publisher_for_url
from catalyst_data.storage.runtime_paths import runtime_store_path

FINANCE_DOMAINS = (
    "reuters.com", "cnbc.com", "bloomberg.com", "wsj.com", "marketwatch.com", "barrons.com", "ft.com",
    "benzinga.com", "finance.yahoo.com", "investopedia.com", "fool.com", "seekingalpha.com", "investing.com",
    "247wallst.com", "thestreet.com", "apnews.com", "kitco.com", "businessinsider.com", "fortune.com",
    "zacks.com", "stocktwits.com", "proactiveinvestors.com", "nasdaq.com",
    "sec.gov", "prnewswire.com", "businesswire.com", "globenewswire.com",
)
LABEL = "web leads — not verified causes"
MAX_QUERIES = 5
MAX_RESULTS = 5
SERVER_DAILY_CREDITS = 50
CACHE_SECONDS = 7 * 24 * 60 * 60


class WebSearchError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_hints(hints: Any) -> tuple[str, ...]:
    if hints is None:
        return ()
    if not isinstance(hints, list) or len(hints) > 2:
        raise ValueError("hints_limit")
    result = []
    for value in hints:
        if not isinstance(value, str) or len(value) > 120:
            raise ValueError("hint_length")
        if not value.strip() or re.search(r"[<>\r\n]", value):
            raise ValueError("hint_plain_text")
        cleaned = " ".join(value.split())
        if re.search(r"\bwhy\s+(?:are\s+)?(?:the\s+)?(?:shares|stock)\s+moved\b", cleaned, re.I):
            raise ValueError("hint_disallowed_query")
        result.append(cleaned)
    return tuple(result)


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return timefields.external(value, source="page", field="declared").utc


def classify_page_time(page_html: str, start: datetime, cutoff: datetime) -> tuple[str, datetime | None, str | None]:
    """Return strict production PIT status, timestamp, and page-owned source."""
    article = benzinga.article_metadata(page_html)
    published = _parse_date(article.get("datePublished")) if article else None
    modified = _parse_date(article.get("dateModified")) if article else None
    if article is not None and article.get("dateModified") and modified is None:
        return "UNDATED", None, None
    if article is not None and published is not None:
        if published > cutoff or (modified is not None and modified > cutoff):
            return "POST", published, "jsonld.datePublished"
        if published <= start:
            return "UNDATED", published, "jsonld.datePublished"
        return "PRE", published, "jsonld.datePublished"
    soup = BeautifulSoup(page_html, "html.parser")
    meta = soup.find("meta", attrs={"property": re.compile(r"^article:published_time$", re.I)})
    published = _parse_date(meta.get("content")) if meta else None
    if published is None:
        return "UNDATED", None, None
    if published > cutoff:
        return "POST", published, "article:published_time"
    if published <= start:
        return "UNDATED", published, "article:published_time"
    return "PRE", published, "article:published_time"


def _company_name(ticker: str) -> str:
    from catalyst_data.retrieval.query_policy import UNIVERSE_SPEC_PATH

    data = json.loads(Path(UNIVERSE_SPEC_PATH).read_text())
    return str(data["companies"].get(ticker, {}).get("legal_name") or ticker)


def build_queries(ticker: str, company: str, session_date: str, hints: tuple[str, ...]) -> tuple[str, ...]:
    base = (
        f'{company} analyst upgrade OR downgrade OR price target {session_date}',
        f'{company} contract OR government OR regulator OR lawsuit {session_date}',
        f'{company} announces {session_date}',
    )
    return tuple((list(base) + [f"{company} {hint} {session_date}" for hint in hints])[:MAX_QUERIES])


def _db_path() -> Path:
    return runtime_store_path("web_search_db")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS response_cache (
            cache_key TEXT PRIMARY KEY, cached_at INTEGER NOT NULL, response_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS web_search_pages (
            lead_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, session_date TEXT NOT NULL,
            url TEXT NOT NULL, title TEXT NOT NULL, publisher TEXT NOT NULL,
            extracted_text TEXT NOT NULL, html_sha256 TEXT NOT NULL,
            published_at TEXT NOT NULL, time_source TEXT NOT NULL, pit_metadata_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_credits (
            day TEXT PRIMARY KEY, credits INTEGER NOT NULL
        );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(web_search_pages)")}
    if "publisher" not in columns:
        conn.execute("ALTER TABLE web_search_pages ADD COLUMN publisher TEXT NOT NULL DEFAULT ''")
    return conn


def _cache_key(ticker: str, session_date: str, queries: tuple[str, ...]) -> str:
    raw = json.dumps([ticker, session_date, queries], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _normalized_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _page_title(page_html: str, fallback: str) -> str:
    article = benzinga.article_metadata(page_html)
    if article and article.get("headline"):
        return " ".join(str(article["headline"]).split())
    soup = BeautifulSoup(page_html, "html.parser")
    meta = soup.find("meta", attrs={"property": re.compile(r"^og:title$", re.I)})
    if meta and meta.get("content"):
        return " ".join(str(meta["content"]).split())
    title = soup.find("title")
    return " ".join(title.get_text(" ", strip=True).split()) if title else " ".join(fallback.split())


def _reserve_server_credits(conn: sqlite3.Connection, credits: int) -> None:
    today = date.today().isoformat()
    cap = int(os.environ.get("CATALYST_TAVILY_DAILY_CREDITS", SERVER_DAILY_CREDITS))
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT credits FROM daily_credits WHERE day=?", (today,)).fetchone()
        if (row[0] if row else 0) + credits > cap:
            raise WebSearchError("daily_credit_limit")
        conn.execute(
            "INSERT INTO daily_credits(day,credits) VALUES (?,?) "
            "ON CONFLICT(day) DO UPDATE SET credits=credits+excluded.credits",
            (today, credits),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _tavily_search(query: str, api_key: str, *, start: str, end: str,
                   include_raw_content: bool = False,
                   include_domains: tuple[str, ...] | list[str] | None = None) -> dict[str, Any]:
    try:
        response = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "topic": "news", "search_depth": "basic", "max_results": MAX_RESULTS,
                  "start_date": start, "end_date": end,
                  "include_domains": list(include_domains or FINANCE_DOMAINS),
                  "include_raw_content": include_raw_content},
            timeout=30,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            error_response = getattr(exc, "response", None) or response
            status = getattr(error_response, "status_code", None)
            body = ""
            try:
                body = str(error_response.text or "")
            except Exception:
                pass
            if status in (401, 403):
                raise WebSearchError("tavily_key_invalid") from None
            if status == 432 or (status == 429 and re.search(r"usage|credit|plan|limit", body, re.I)):
                raise WebSearchError("tavily_key_exhausted") from None
            raise WebSearchError("tavily_network_error") from None
        payload = response.json()
    except WebSearchError:
        raise
    except (requests.RequestException, ValueError):
        raise WebSearchError("tavily_network_error") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
        raise WebSearchError("tavily_invalid_response")
    return payload


def server_key_pool(db_path: str | Path | None = None, *, server_key: str | None = None) -> tavily_pool.TavilyKeyPool:
    keys = [server_key.strip()] if server_key and server_key.strip() else tavily_pool.keys_from_env()
    store = Path(db_path) if db_path else _db_path()
    # Key health lives in its own file: callers hold write transactions on the search store
    # while a search runs, so sharing that file would lock the pool's own updates.
    return tavily_pool.TavilyKeyPool(keys, store.with_name("tavily_key_state.sqlite3"))


def search_leads(
    ticker: str,
    session_date: str,
    *,
    hints: Any = None,
    api_key: str | None = None,
    server_key: str | None = None,
    search_fn: Callable[..., dict[str, Any]] | None = None,
    fetcher: Any | None = None,
    db_path: str | Path | None = None,
    tavily_context: tavily_ledger.TavilyCallContext | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    clean_hints = validate_hints(hints)
    header_key = (api_key or "").strip()
    server_credential = not header_key
    identity = build_temporal_identity(session_date)
    start = identity.information_window_start_at
    cutoff = identity.cutoff_at
    queries = build_queries(ticker, _company_name(ticker), session_date, clean_hints)
    store_path = Path(db_path) if db_path else _db_path()
    conn = _connect(store_path)
    try:
        cache_key = _cache_key(ticker, session_date, queries)
        cached = conn.execute("SELECT cached_at,response_json FROM response_cache WHERE cache_key=?", (cache_key,)).fetchone()
        cached_at = int((now or datetime.now(timezone.utc)).timestamp())
        if cached and cached_at - cached[0] <= CACHE_SECONDS:
            result = json.loads(cached[1])
            result["cached"] = True
            result["credits_used"] = 0
            return result
        tavily_ledger.preflight(tavily_context, now=now)
        if server_credential and not tavily_pool.keys_from_env() and not server_key:
            raise WebSearchError("tavily_key_missing")
        pool = server_key_pool(db_path, server_key=server_key) if server_credential else None
        if server_credential and tavily_context is None:
            _reserve_server_credits(conn, len(queries))
        client = search_fn or _tavily_search
        fetcher = fetcher or fetch.Fetcher()
        pages: dict[str, dict[str, Any]] = {}
        seen_urls: set[str] = set()
        excluded = {"post_close": 0, "undated": 0, "unfetched": 0, "off_subject": 0}
        company_name = _company_name(ticker)
        matcher = issuer_matcher(ticker, company_name)
        for query in queries:
            call_kwargs = {"start": start.date().isoformat(),
                           "end": (date.fromisoformat(session_date) + timedelta(days=1)).isoformat()}
            if server_credential:
                def call_with_key(key):
                    with tavily_ledger.guard(tavily_context, now=now):
                        return client(query, key, **call_kwargs)

                payload = pool.call(call_with_key)
            else:
                with tavily_ledger.guard(tavily_context, now=now):
                    payload = client(query, header_key, **call_kwargs)
            for row in payload.get("results", ()):
                url = str(row.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                provider_date = _parse_date(row.get("published_date"))
                if provider_date is not None and provider_date > cutoff:
                    excluded["post_close"] += 1
                    continue
                fetched = fetcher.fetch_page(url)
                if fetched is None:
                    excluded["unfetched"] += 1
                    continue
                final_url, html = fetched
                status, published, time_source = classify_page_time(html, start, cutoff)
                if status == "POST":
                    excluded["post_close"] += 1
                    continue
                if status == "UNDATED":
                    excluded["undated"] += 1
                    continue
                title = _page_title(html, str(row.get("title") or "").strip())
                if not title:
                    excluded["unfetched"] += 1
                    continue
                content = fetch.extract(html, title) or ""
                if not content:
                    excluded["unfetched"] += 1
                    continue
                mentions_in_title = matcher(title)
                if not mentions_in_title and not matcher(content[:1500]):
                    excluded["off_subject"] += 1
                    continue
                lead_id = hashlib.sha256(f"{ticker}\0{session_date}\0{final_url}".encode()).hexdigest()
                article = benzinga.article_metadata(html) or {}
                publisher = str((row.get("source") or "")).strip() or publisher_for_url(final_url) or urlsplit(final_url).hostname or ""
                image = fetch.page_image_with_source(html, final_url)
                page_body = {
                    "lead_id": lead_id,
                    "ticker": ticker,
                    "session_date": session_date,
                    "url": final_url,
                    "title": title,
                    "publisher": publisher,
                    "extracted_text": content,
                    "html_sha256": hashlib.sha256(html.encode()).hexdigest(),
                    "published_at": published.isoformat().replace("+00:00", "Z"),
                    "time_source": time_source,
                    "pit_metadata_json": json.dumps({"datePublished": article.get("datePublished"), "dateModified": article.get("dateModified")}, sort_keys=True),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
                conn.execute(
                    "INSERT OR REPLACE INTO web_search_pages "
                    "(lead_id,ticker,session_date,url,title,publisher,extracted_text,html_sha256,published_at,time_source,pit_metadata_json,fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(page_body[key] for key in (
                        "lead_id", "ticker", "session_date", "url", "title", "publisher",
                        "extracted_text", "html_sha256", "published_at", "time_source",
                        "pit_metadata_json", "fetched_at",
                    )),
                )
                lead = {
                    "lead_id": lead_id, "title": title, "url": final_url, "publisher": publisher,
                    "published_at": page_body["published_at"], "time_source": time_source,
                    "snippet": " ".join(str(row.get("content") or "").split())[:400],
                    "image_url": image[0] if image else None,
                    "mentions_subject": True,
                    "mentions_in_title": mentions_in_title,
                    "also_published_by": [],
                    "status": "PRE",
                }
                title_key = _normalized_title(title)
                existing = pages.get(title_key)
                if existing is None:
                    pages[title_key] = lead
                else:
                    publishers = list(existing["also_published_by"])
                    publishers.append(existing["publisher"])
                    publishers.extend(lead["also_published_by"])
                    if lead["publisher"] not in publishers:
                        publishers.append(lead["publisher"])
                    publishers = list(dict.fromkeys(item for item in publishers if item))
                    if timefields.stored(lead["published_at"]) < timefields.stored(existing["published_at"]):
                        lead["also_published_by"] = [
                            item for item in publishers if item != lead["publisher"]
                        ]
                        pages[title_key] = lead
                    else:
                        existing["also_published_by"] = [
                            item for item in publishers if item != existing["publisher"]
                        ]
        leads = sorted(
            pages.values(),
            key=lambda item: (
                item["mentions_in_title"],
                timefields.stored(item["published_at"]),
            ),
            reverse=True,
        )
        result = {"ticker": ticker, "session_date": session_date, "leads": leads,
                  "excluded": excluded, "queries_used": list(queries), "credits_used": len(queries),
                  "cached": False, "provider": "tavily", "label": LABEL}
        conn.execute("INSERT OR REPLACE INTO response_cache VALUES (?,?,?)", (cache_key, cached_at, json.dumps(result, ensure_ascii=False)))
        conn.commit()
        return result
    except WebSearchError as exc:
        conn.rollback()
        if exc.code in {"web_search_budget_exhausted", "web_search_public_disabled"}:
            return {
                "ticker": ticker,
                "session_date": session_date,
                "leads": [],
                "excluded": {"post_close": 0, "undated": 0, "unfetched": 0, "off_subject": 0},
                "queries_used": list(queries),
                "credits_used": 0,
                "cached": False,
                "provider": "tavily",
                "label": LABEL,
                "reason": exc.code,
            }
        raise
    except Exception as exc:
        conn.rollback()
        raise WebSearchError("web_search_failed") from exc
    finally:
        conn.close()


def get_pre_lead(lead_id: str, *, db_path: str | Path | None = None) -> dict[str, Any] | None:
    conn = _connect(Path(db_path) if db_path else _db_path())
    try:
        row = conn.execute("SELECT * FROM web_search_pages WHERE lead_id=?", (lead_id,)).fetchone()
        if row is None:
            return None
        keys = tuple(item[1] for item in conn.execute("PRAGMA table_info(web_search_pages)"))
        return dict(zip(keys, row))
    finally:
        conn.close()
