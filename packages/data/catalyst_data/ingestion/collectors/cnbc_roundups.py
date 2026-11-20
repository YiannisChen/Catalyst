"""CNBC-owned daily-sitemap discovery and immutable page cache for M9 roundups."""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from catalyst_data import trading_calendar
from catalyst_data.ingestion.collectors import fetch, market
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval.query_policy import UNIVERSE_SPEC_PATH
from catalyst_data.storage.runtime_paths import (
    default_sec_tickers_path,
    runtime_store_path,
)

KINDS = ("premarket", "midday", "after_hours_prev", "analyst_calls")
_KNOWN_BRANDS = {
    "NETFLIX": "NFLX", "LOCKHEED MARTIN": "LMT", "AMAZON": "AMZN",
    "AMAZON.COM": "AMZN", "INTEL": "INTC", "ADOBE": "ADBE", "AT&T": "T",
    "NEWMONT": "NEM", "FREEPORT-MCMORAN": "FCX", "BLACKROCK": "BLK",
    "AIR PRODUCTS AND CHEMICALS": "APD", "MEDTRONIC": "MDT",
}
_NAME_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "company", "co",
                  "limited", "ltd", "plc", "holdings", "holding", "group", "sa", "nv"}
_EXPLICIT_TICKER = re.compile(r"(?:\(\s*([A-Z]{1,5})\s*\)|\b(?:NYSE|NASDAQ|AMEX)\s*:\s*([A-Z]{1,5})\b)", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roundup_pages (
    session_date TEXT NOT NULL, roundup_kind TEXT NOT NULL, url TEXT NOT NULL,
    canonical_url TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT,
    published_time_source TEXT, body_text TEXT NOT NULL, html_sha256 TEXT NOT NULL,
    fetched_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'cnbc_sitemap',
    PRIMARY KEY (session_date, roundup_kind, canonical_url)
);
CREATE TABLE IF NOT EXISTS roundup_segments (
    session_date TEXT NOT NULL, roundup_kind TEXT NOT NULL, url TEXT NOT NULL,
    canonical_url TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT,
    published_time_source TEXT, body_source TEXT NOT NULL, ticker TEXT NOT NULL,
    segment_index INTEGER NOT NULL, segment_text TEXT NOT NULL,
    segment_sha256 TEXT NOT NULL, page_sha256 TEXT NOT NULL,
    fetched_at TEXT NOT NULL, credits INTEGER NOT NULL DEFAULT 0,
    segment_type TEXT DEFAULT 'block',
    PRIMARY KEY (session_date, canonical_url, ticker, segment_index)
);
CREATE INDEX IF NOT EXISTS idx_roundup_segments_lookup
    ON roundup_segments(ticker, session_date, published_at);
CREATE TABLE IF NOT EXISTS roundup_day_errors (
    session_date TEXT NOT NULL, roundup_kind TEXT NOT NULL, url TEXT NOT NULL,
    error TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY(session_date, roundup_kind, url)
);
"""


def _connect(db_path: str | Path | None) -> sqlite3.Connection:
    path = Path(db_path).expanduser() if db_path else runtime_store_path("web_search_db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(roundup_segments)")}
    if "segment_type" not in columns:
        conn.execute("ALTER TABLE roundup_segments ADD COLUMN segment_type TEXT DEFAULT 'block'")
    return conn


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = unquote(parts.path).rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def sitemap_url(day: str | date) -> str:
    value = date.fromisoformat(day) if isinstance(day, str) else day
    return f"https://www.cnbc.com/site-map/articles/{value:%Y}/{value:%B}/{value.day}/"


def _walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def parse_sitemap(page_html: str) -> list[dict[str, str]]:
    """Extract CNBC story records from the page's embedded JSON."""
    soup = BeautifulSoup(page_html or "", "html.parser")
    items: dict[str, dict[str, str]] = {}
    for script in soup.find_all("script"):
        raw = html.unescape(script.string or script.get_text())
        if not raw or "cnbcnewsstory" not in raw.casefold():
            continue
        candidates = []
        try:
            candidates.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            decoder = json.JSONDecoder()
            for match in re.finditer(r"[\[{]", raw):
                try:
                    value, _end = decoder.raw_decode(raw[match.start():])
                    candidates.append(value)
                except json.JSONDecodeError:
                    continue
        for root in candidates:
            for item in _walk_json(root):
                if str(item.get("__typename", "")).casefold() != "cnbcnewsstory":
                    continue
                url = unquote(html.unescape(str(item.get("url") or "")).replace("\\/", "/")).strip()
                title = " ".join(html.unescape(str(item.get("title") or "")).split())
                if url.startswith("https://www.cnbc.com/") and title and classify_title(title):
                    items.setdefault(_canonical_url(url), {"url": url, "title": title})
    return list(items.values())


def classify_title(title: str) -> str | None:
    value = " ".join((title or "").split()).casefold()
    if re.search(r"stocks making the biggest moves\s*(?:\([^)]*\)\s*)?premarket", value):
        return "premarket"
    if re.search(r"stocks making the biggest moves\s*(?:\([^)]*\)\s*)?midday", value):
        return "midday"
    if re.search(r"stocks making the biggest moves\s*(?:\([^)]*\)\s*)?after hours", value):
        return "after_hours"
    if re.search(r"\bbiggest analyst calls\b", value):
        return "analyst_calls"
    return None


def _jsonld_objects(page_html: str):
    soup = BeautifulSoup(page_html or "", "html.parser")
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            value = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        yield from _walk_json(value)


def extract_published_at(page_html: str) -> str | None:
    """Return normalized JSON-LD datePublished; no non-page-owned fallback."""
    for item in _jsonld_objects(page_html):
        value = item.get("datePublished")
        if not isinstance(value, str) or not value.strip():
            continue
        stamp = timefields.external_stamp(value, source="cnbc", field="datePublished")
        if stamp is None:
            continue  # unparseable, or no time zone: never assumed (2026-10-22 review)
        return stamp
    return None


def extract_body(page_html: str) -> str:
    soup = BeautifulSoup(page_html or "", "html.parser")
    body = soup.find(class_="ArticleBody-articleBody")
    if body is None:
        body = soup.find("span", class_=lambda value: value and "xyz-data" in value.split())
    if body is None:
        return ""
    for tag in body(["script", "style"]):
        tag.decompose()
    return fetch.strip_zero_width(" ".join(body.get_text(" ", strip=True).split()))


def _universe_tickers() -> set[str]:
    path = market.REPO / "data/manifests/heldout_universe_v1.json"
    payload = json.loads(path.read_text())
    return {ticker.upper() for members in payload.get("sectors", {}).values() for ticker in members}


def _sec_names(db_path: str | Path | None) -> dict[str, str]:
    from catalyst_data.retrieval import web_bodies

    name_map = web_bodies.name_map_path()
    candidates = []
    if name_map is not None:
        candidates.append(name_map)
    if db_path is not None:
        candidates.append(Path(db_path).expanduser().parent / "sec_company_tickers.json")
    candidates.append(default_sec_tickers_path(market.REPO))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        names = {}
        for item in payload.values():
            ticker = str(item.get("ticker") or "").upper()
            name = str(item.get("title") or item.get("name") or "").strip()
            if ticker and name:
                names[ticker] = name
        return names
    return {}


def _norm_name(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", value.casefold().replace("&", " and "))
    while tokens and tokens[-1] in _NAME_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _name_map(db_path: str | Path | None = None) -> dict[str, str]:
    allowed = _universe_tickers()
    mapping: dict[str, str] = {}
    try:
        companies = json.loads(Path(UNIVERSE_SPEC_PATH).read_text()).get("companies", {})
    except (OSError, json.JSONDecodeError):
        companies = {}
    for ticker, data in companies.items():
        if ticker.upper() in allowed and data.get("legal_name"):
            mapping[_norm_name(str(data["legal_name"]))] = ticker.upper()
    for ticker, name in _sec_names(db_path).items():
        if ticker in allowed:
            mapping[_norm_name(name)] = ticker
    for brand, ticker in _KNOWN_BRANDS.items():
        mapping[_norm_name(brand)] = ticker
    try:
        from catalyst_data.retrieval import query_policy
        for brand, ticker in query_policy.conservative_issuer_brands().items():
            if ticker.upper() in allowed:
                mapping[_norm_name(brand)] = ticker.upper()
        for phrase, ticker in query_policy.conservative_issuer_phrases():
            if ticker.upper() in allowed:
                mapping[_norm_name(phrase)] = ticker.upper()
    except (ImportError, AttributeError):
        pass
    return mapping


def map_company(company: str, *, db_path: str | Path | None = None) -> list[str]:
    normalized = _norm_name(company)
    if not normalized:
        return []
    mapping = _name_map(db_path)
    if normalized in mapping:
        return [mapping[normalized]]
    if len(normalized) < 4:  # a bare letter or short ticker ("T") never prefix-matches a name ("T-Mobile")
        return []
    matches = [(name, ticker) for name, ticker in mapping.items()
               if len(name) >= 4 and (normalized.startswith(name + " ") or name.startswith(normalized + " "))]
    if not matches:
        return []
    return [max(matches, key=lambda row: len(row[0]))[1]]


def _lead_tickers(company: str, *, db_path: str | Path | None = None) -> list[str]:
    # An exact full-name match wins ("Air Products and Chemicals" is one company).
    if _norm_name(company) in _name_map(db_path):
        return map_company(company, db_path=db_path)
    # A listed head ("Newmont , Freeport-McMoRan") maps to every company it names; a prefix
    # match on the whole head would stop at the first name.
    tickers: list[str] = []
    for part in re.split(r"\s*(?:,|;|\band\b|&)\s*", company, flags=re.I):
        tickers.extend(map_company(part.strip(" ,;"), db_path=db_path))
    tickers = list(dict.fromkeys(tickers))
    return tickers or map_company(company, db_path=db_path)


def _explicit_tickers(block: str, allowed: set[str]) -> list[str]:
    found = []
    for match in _EXPLICIT_TICKER.finditer(block):
        ticker = (match.group(1) or match.group(2) or "").upper()
        if ticker in allowed and ticker not in found:
            found.append(ticker)
    return found


def _segments_from_text(text: str, *, analyst: bool, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    flat = " ".join(fetch.strip_zero_width(text or "").split())
    if not flat:
        return []
    markers: list[tuple[int, int, str, list[str]]] = []
    if analyst:
        pattern = re.compile(r"\b(?:[A-Z][A-Za-z&.'-]*(?:\s+[A-Z][A-Za-z&.'-]*){0,3})\s+(?:upgrade|downgrade|reiterate|initiate|resume)s?\s+(.+?)(?=\s+(?:to|from|as|at)\b)", re.I)
        for match in pattern.finditer(flat):
            target = match.group(1).strip(" ,")
            tickers = _lead_tickers(target, db_path=db_path)
            head_end = match.end(1)
            start = match.start()
            markers.append((start, head_end, target, tickers))
    else:
        for match in re.finditer(r"\s+[—–]\s*", flat):
            left = flat[:match.start()]
            boundaries = list(re.finditer(r"(?:^|[.!?:]\s+)", left))
            start = boundaries[-1].end() if boundaries else 0
            head = left[start:].strip(" \t,;–—")
            if not head or len(head.split()) > 8:
                continue
            tickers = _lead_tickers(head, db_path=db_path)
            if not tickers:
                # A parenthesized or exchange-qualified ticker is acceptable for multi-letter symbols.
                tickers = _explicit_tickers(head, _universe_tickers())
            if tickers:
                markers.append((start, match.end(), head, tickers))
    markers.sort(key=lambda row: row[0])
    unique = []
    for marker in markers:
        if unique and unique[-1][0] == marker[0]:
            continue
        unique.append(marker)
    output = []
    block_starts: set[int] = set()
    for index, (start, _end, company, tickers) in enumerate(unique):
        end = unique[index + 1][0] if index + 1 < len(unique) else len(flat)
        segment = flat[start:end].strip()
        # Explicit tickers anywhere in the company block can recover punctuation/brand variants.
        all_tickers = list(dict.fromkeys(tickers + _explicit_tickers(segment, _universe_tickers())))
        if segment and all_tickers:
            output.append({"company": company, "tickers": all_tickers, "text": segment,
                           "segment_type": "block"})
            block_starts.add(start)
    output.extend(_inline_segments(flat, block_starts, db_path=db_path))
    return output


def _inline_segments(text: str, block_starts: set[int], *,
                     db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Capture company mentions tied to an explicit same-sentence price move."""
    mapping = _name_map(db_path)
    aliases = sorted(mapping, key=len, reverse=True)
    if not aliases:
        return []
    patterns = []
    for alias in aliases:
        tokens = alias.split()
        pieces = [r"(?:and|&)" if token == "and" else re.escape(token) for token in tokens]
        patterns.append((alias, re.compile(r"(?<![A-Za-z0-9])" + r"[\s\-–]+".join(pieces)
                                           + r"(?![A-Za-z0-9])", re.I)))
    sentence_pattern = re.compile(r"[^.!?]+(?:[.!?]+|$)")
    move_pattern = re.compile(
        r"\s*(?:['’]\s*s\s*)?(?:(?:shares?|stock)\s+)?"
        r"(?:rose|rallied|gained|climbed|jumped|surged|advanced|fell|dropped|"
        r"tumbled|shed|declined|slid|sank|traded)\b[^.!?%]*\d+(?:\.\d+)?\s*%", re.I,
    )
    found = []
    for sentence_match in sentence_pattern.finditer(text):
        sentence = sentence_match.group(0).strip()
        if not sentence:
            continue
        sentence_start = sentence_match.start() + len(sentence_match.group(0)) - len(sentence_match.group(0).lstrip())
        tickers = []
        for alias, pattern in patterns:
            for match in pattern.finditer(sentence):
                if not move_pattern.match(sentence[match.end():]):
                    continue
                if sentence_start + match.start() in block_starts:
                    continue
                ticker = mapping[alias]
                if ticker not in tickers:
                    tickers.append(ticker)
        if tickers:
            found.append({"company": "", "tickers": tickers, "text": sentence,
                          "segment_type": "inline"})
    return found


def segment_body(body: str, roundup_kind: str, *, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    return _segments_from_text(body, analyst=roundup_kind == "analyst_calls", db_path=db_path)


def segment_article(page_html: str, roundup_kind: str, *, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    if roundup_kind != "analyst_calls":
        return segment_body(extract_body(page_html), roundup_kind, db_path=db_path)
    soup = BeautifulSoup(page_html or "", "html.parser")
    article = soup.find(class_="ArticleBody-articleBody") or soup
    pieces = []
    for node in article.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        value = " ".join(node.get_text(" ", strip=True).split())
        if value:
            pieces.append(value)
    text = " ".join(pieces) if pieces else extract_body(page_html)
    return _segments_from_text(text, analyst=True, db_path=db_path)


def _record_error(conn: sqlite3.Connection, session_date: str, kind: str, url: str, exc: BaseException) -> None:
    conn.execute("INSERT OR REPLACE INTO roundup_day_errors VALUES (?,?,?,?,?)",
                 (session_date, kind, url, f"{type(exc).__name__}: {str(exc)[:240]}", _now()))
    conn.commit()


def record_day_error(db_path: str | Path | None, session_date: str, kind: str,
                     url: str, error: str) -> None:
    """Persist an orchestration-level error (for example a failed Tavily day)."""
    conn = _connect(db_path)
    try:
        conn.execute("INSERT OR REPLACE INTO roundup_day_errors VALUES (?,?,?,?,?)",
                     (session_date, kind, url, error[:300], _now()))
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _previous_session(session_date: str) -> str:
    days = trading_calendar.calendar_trading_days(
        f"{trading_calendar.CALENDAR_YEARS[0]}-01-01", f"{trading_calendar.CALENDAR_YEARS[1]}-12-31")
    index = days.index(session_date)
    if index == 0:
        return days[0]
    return days[index - 1]


def discover_day(session_date: str, fetcher: Any | None = None,
                 db_path: str | Path | None = None) -> dict[str, Any]:
    """Fetch the two relevant daily sitemaps, storing every fetched roundup page."""
    date.fromisoformat(session_date)
    fetcher = fetcher or fetch.Fetcher()
    conn = _connect(db_path)
    counts = {"pages": 0, "segments": 0}
    sitemap_days = ((session_date, {"premarket", "midday", "analyst_calls"}),
                    (_previous_session(session_date), {"after_hours"}))
    try:
        conn.execute("DELETE FROM roundup_day_errors WHERE session_date=?", (session_date,))
        conn.commit()
        for source_day, accepted in sitemap_days:
            sitemap = sitemap_url(source_day)
            try:
                fetched_sitemap = fetcher.fetch_page(sitemap)
                if not fetched_sitemap:
                    raise RuntimeError("sitemap_unavailable")
                _final_sitemap, sitemap_html = fetched_sitemap
                items = parse_sitemap(sitemap_html)
            except Exception as exc:
                _record_error(conn, session_date, "sitemap", sitemap, exc)
                continue
            for item in items:
                source_kind = classify_title(item["title"])
                if source_kind not in accepted:
                    continue
                kind = "after_hours_prev" if source_kind == "after_hours" else source_kind
                url = item["url"]
                try:
                    page = fetcher.fetch_page(url)
                    if not page:
                        raise RuntimeError("article_unavailable")
                    final_url, page_html = page
                    canonical = _canonical_url(final_url)
                    soup = BeautifulSoup(page_html, "html.parser")
                    title_tag = soup.find("meta", property="og:title")
                    title = " ".join((title_tag.get("content", "") if title_tag else "").split()) or (soup.title.get_text(" ", strip=True) if soup.title else item["title"])
                    published_at = extract_published_at(page_html)
                    body = extract_body(page_html)
                    page_sha = hashlib.sha256(page_html.encode()).hexdigest()
                    fetched_at = _now()
                    conn.execute("""INSERT INTO roundup_pages
                        (session_date,roundup_kind,url,canonical_url,title,published_at,
                         published_time_source,body_text,html_sha256,fetched_at,source)
                        VALUES (?,?,?,?,?,?,?,?,?,?, 'cnbc_sitemap')
                        ON CONFLICT(session_date,roundup_kind,canonical_url) DO UPDATE SET
                          url=excluded.url,title=excluded.title,published_at=excluded.published_at,
                          published_time_source=excluded.published_time_source,body_text=excluded.body_text,
                          html_sha256=excluded.html_sha256,fetched_at=excluded.fetched_at,source=excluded.source""",
                        (session_date, kind, final_url, canonical, title, published_at,
                         "jsonld.datePublished" if published_at else None, body, page_sha, fetched_at))
                    conn.execute("DELETE FROM roundup_segments WHERE session_date=? AND canonical_url=?", (session_date, canonical))
                    conn.commit()
                    counts["pages"] += 1
                    if published_at and body:
                        for segment_index, segment in enumerate(segment_body(body, kind, db_path=db_path), start=1):
                            for ticker in segment["tickers"]:
                                conn.execute("""INSERT OR REPLACE INTO roundup_segments
                                    (session_date,roundup_kind,url,canonical_url,title,published_at,
                                     published_time_source,body_source,ticker,segment_index,segment_text,
                                     segment_sha256,page_sha256,fetched_at,credits,segment_type)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                                    (session_date, kind, final_url, canonical, title, published_at,
                                     "jsonld.datePublished", "cnbc_article_body", ticker, segment_index,
                                     segment["text"], hashlib.sha256(segment["text"].encode()).hexdigest(),
                                     page_sha, fetched_at, segment.get("segment_type", "block")))
                                counts["segments"] += 1
                        conn.commit()
                except Exception as exc:
                    _record_error(conn, session_date, kind, url, exc)
                    continue
        errors = conn.execute("SELECT count(*) FROM roundup_day_errors WHERE session_date=?", (session_date,)).fetchone()[0]
        return {"session_date": session_date, **counts, "errors": errors,
                "status": "PARTIAL" if errors else "COMPLETED", "credits_used": 0}
    finally:
        conn.close()


def cached_segments(ticker: str, session_date: str, *, db_path: str | Path | None = None,
                    start_at: str | None = None,
                    end_at: str | None = None) -> list[dict[str, Any]]:
    """Read cached roundup segments for a ticker/session and publication window."""
    conn = _connect(db_path)
    try:
        sql = ("SELECT * FROM roundup_segments WHERE ticker=? AND session_date=? "
               "AND published_at IS NOT NULL")
        params: list[Any] = [ticker.upper(), session_date]
        if start_at:
            sql += " AND published_at >= ?"
            params.append(start_at)
        if end_at:
            sql += " AND published_at <= ?"
            params.append(end_at)
        sql += " ORDER BY published_at DESC, roundup_kind, canonical_url, segment_index"
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def reprocess(db_path: str | Path) -> dict[str, int]:
    """Re-segment all persisted page bodies without network access."""
    conn = _connect(db_path)
    pages = conn.execute("SELECT * FROM roundup_pages ORDER BY session_date,canonical_url").fetchall()
    segment_count = 0
    try:
        conn.execute("DELETE FROM roundup_segments WHERE canonical_url IN (SELECT canonical_url FROM roundup_pages)")
        for page in pages:
            if not page["published_at"] or not page["body_text"]:
                continue
            for segment_index, segment in enumerate(segment_body(page["body_text"], page["roundup_kind"], db_path=db_path), start=1):
                for ticker in segment["tickers"]:
                    conn.execute("""INSERT OR REPLACE INTO roundup_segments
                        (session_date,roundup_kind,url,canonical_url,title,published_at,
                         published_time_source,body_source,ticker,segment_index,segment_text,
                         segment_sha256,page_sha256,fetched_at,credits,segment_type)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                        (page["session_date"], page["roundup_kind"], page["url"], page["canonical_url"],
                         page["title"], page["published_at"], page["published_time_source"],
                         "cnbc_article_body", ticker, segment_index, segment["text"],
                         hashlib.sha256(segment["text"].encode()).hexdigest(), page["html_sha256"],
                         page["fetched_at"], segment.get("segment_type", "block")))
                    segment_count += 1
        conn.commit()
        return {"pages": len(pages), "segments": segment_count}
    finally:
        conn.close()
