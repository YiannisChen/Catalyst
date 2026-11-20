"""Shared parsing and request helpers for v2 web discovery."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from catalyst_data import trading_calendar
from catalyst_data.ingestion import tavily_pool
from catalyst_data.ingestion.collectors import fetch, web_search
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval.query_policy import UNIVERSE_SPEC_PATH

_MONTH_DATE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+\d{4}\b", re.I,
)
_NUMERIC_DATE = re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_DATE_LABEL = re.compile(r"\b(Published|Updated)\b\s*:?\s*", re.I)
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_BOILERPLATE = re.compile(
    r"\b(?:accept cookies?|cookie preferences?|subscribe now|sign up for|newsletter|"
    r"privacy policy|terms of (?:use|service)|all rights reserved|advertisement|"
    r"download our app|follow us on|manage cookies?)\b", re.I,
)


def _company(ticker: str) -> str:
    data = json.loads(Path(UNIVERSE_SPEC_PATH).read_text())
    return str(data["companies"].get(ticker, {}).get("legal_name") or ticker)


def _call_tavily(client: Callable[..., dict[str, Any]], query: str, *, api_key: str | None,
                 db_path: str | Path | None, start: str, end: str,
                 include_raw_content: bool = False) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"start": start, "end": end}
    if include_raw_content:
        kwargs["include_raw_content"] = True
    if api_key:
        return client(query, api_key.strip(), **kwargs)
    pool = web_search.server_key_pool(db_path)
    if not tavily_pool.keys_from_env():
        raise web_search.WebSearchError("tavily_key_missing")
    return pool.call(lambda key: client(query, key, **kwargs))


def _provider_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return timefields.external_date(value, source="tavily", field="published_date")


def _confirmed_raw_date(raw_text: str, tavily_date: Any) -> tuple[date | None, str | None]:
    """Accept an explicit dateline near the top only when Tavily corroborates it."""
    provider_date = _provider_date(tavily_date)
    if provider_date is None:
        return None, None
    prefix = (raw_text or "")[:1500]
    label = _DATE_LABEL.search(prefix)
    candidates: list[tuple[int, str, str | None]] = []
    if label:
        match = re.match(r"\s*(?:on\s+)?(\d{4}-\d{2}-\d{2}|[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+\d{4}|\d{1,2}[-/]\d{1,2}[-/]\d{4})", prefix[label.end():])
        if match:
            candidates.append((label.end(), match.group(1), label.group(1).casefold()))
    candidates.extend((match.start(), match.group(0), None)
                      for pattern in (_ISO_DATE, _MONTH_DATE, _NUMERIC_DATE)
                      for match in pattern.finditer(prefix))
    for offset, raw_date, semantics in sorted(candidates, key=lambda item: item[0]):
        candidate = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw_date.strip(), flags=re.I)
        parsed = timefields.dateline(candidate)
        if parsed is not None and abs((parsed - provider_date).days) <= 1:
            return parsed, "updated" if semantics == "updated" else "published"
    return None, None


def _clean_tavily_raw(raw_text: str) -> str:
    """Keep prose from Tavily's markdown while dropping navigation and boilerplate."""
    paragraphs: list[str] = []
    for raw_line in (raw_text or "").splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line or line.startswith(("#", "---", "***", ">")):
            continue
        if _BOILERPLATE.search(line):
            continue
        links = list(_MARKDOWN_LINK.finditer(line))
        plain = _MARKDOWN_LINK.sub(lambda match: match.group(1), line)
        plain = re.sub(r"(?:[*_`~]|\s{2,})", " ", plain).strip(" -*\t")
        if not plain:
            continue
        linked_chars = sum(len(match.group(0)) for match in links)
        if links and linked_chars / max(len(line), 1) >= 0.35:
            continue
        if len(plain) < 55 and not re.search(r"[.!?]", plain):
            continue
        if re.fullmatch(r"[\w\s|/,:-]{1,80}", plain) and not re.search(r"[.!?]", plain):
            continue
        paragraphs.append(plain)
    text = fetch.strip_zero_width("\n\n".join(paragraphs))
    prose_chars = sum(
        len(paragraph) for paragraph in text.split("\n\n")
        if len(paragraph) >= 80 and len(paragraph.split()) >= 12
        and re.search(r"[.!?]", paragraph)
    )
    return text if prose_chars >= 400 else ""


def _jsonld_article_body(page_html: str, title: str) -> str:
    article = web_search.benzinga.article_metadata(page_html) or {}
    value = article.get("articleBody")
    if not isinstance(value, str):
        return ""
    soup = BeautifulSoup(value, "html.parser")
    text = "\n\n".join(
        " ".join(node.get_text(" ", strip=True).split())
        for node in (soup.find_all(["p"]) or [soup])
        if len(node.get_text(" ", strip=True)) > 30
    )
    text = fetch.strip_zero_width(text)
    key_terms = {word for word in re.findall(r"[a-z]{5,}", title.lower())}
    if (len(text) < 400 or (key_terms and not any(term in text.lower() for term in key_terms))):
        return ""
    return text


def _issuer_relevance_matcher(ticker: str, company_name: str) -> Callable[[str], bool]:
    # "Target" is also a common noun in analyst copy ("price target"); keep that
    # brand match case-sensitive while retaining the exact ticker path.
    if ticker.upper() == "TGT":
        def matches(text: str) -> bool:
            text = text or ""
            without_price_target = re.sub(r"\bprice\s+target\b", "", text, flags=re.I)
            return bool(
                re.search(r"(?<![A-Za-z])TGT(?![A-Za-z])", text)
                or re.search(r"(?<![A-Za-z])(?:Target|TARGET)(?![A-Za-z])", without_price_target)
            )
        return matches
    return web_search.issuer_matcher(ticker, company_name)


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def _publisher_family(url: str, publisher: str) -> str:
    host = urlsplit(url).hostname or publisher
    host = host.lower().removeprefix("www.")
    if host.endswith("yahoo.com"):
        return "yahoo.com"
    return host


def _publication_bounds(session_date: str, now: datetime | None = None) -> tuple[str, str]:
    calendar_start = f"{trading_calendar.CALENDAR_YEARS[0]}-01-01"
    calendar_end = f"{trading_calendar.CALENDAR_YEARS[1]}-12-31"
    days = trading_calendar.calendar_trading_days(calendar_start, calendar_end)
    index = days.index(session_date)
    start_date = days[max(0, index - 3)]
    end_date = days[min(len(days) - 1, index + 3)]
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    return start_date, min(end_date, today)


__all__ = [
    "_BOILERPLATE", "_DATE_LABEL", "_ISO_DATE", "_MARKDOWN_LINK", "_MONTH_DATE",
    "_NUMERIC_DATE", "_call_tavily", "_canonical_url", "_clean_tavily_raw", "_company",
    "_confirmed_raw_date", "_issuer_relevance_matcher", "_jsonld_article_body",
    "_publication_bounds", "_provider_date", "_publisher_family", "tavily_pool", "web_search",
]
