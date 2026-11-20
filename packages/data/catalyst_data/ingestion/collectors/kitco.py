"""Kitco sitemap discovery and public Reuters-wire body extraction."""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from catalyst_data.ingestion.collectors.fetch import extract, page_image_with_source
from catalyst_data.ingestion.storage import store
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval.web_bodies import (
    names_subject,
    publisher_for_url,
    subject_matcher,
)

SITEMAP = "https://www.kitco.com/static-sitemaps/off-the-wire.xml"
# Kitco's own market reporting (daily gold/silver/metals reports).
SITEMAPS = (SITEMAP, "https://www.kitco.com/static-sitemaps/news.xml")
MAX_PER_SESSION = 8
# Session wires whose headline is about the day's markets rather than the
# commodity ("London's FTSE 100 record high, miners industrials boost"): fetched,
# then kept only if the lead names the commodity (names_subject).
# Commodity-adjacent session titles are tried before generic market titles.
BROAD_TITLE = re.compile(
    r"\b(?:miners?|mining|metals?|commodit(?:y|ies)|energy|resources|materials)\b", re.IGNORECASE,
)
MARKET_TITLE = re.compile(r"\b(?:stocks|shares|markets?|wall st(?:reet)?)\b", re.IGNORECASE)
MAX_BROAD_PER_SESSION = 8
_ENTRY = re.compile(
    r"<loc>([^<]+)</loc>.*?<news:publication_date>([^<]+)</news:publication_date>"
    r".*?<news:title><!\[CDATA\[(.*?)\]\]></news:title>", re.S,
)


def parse_sitemap(xml: str) -> list[tuple[str, str, str]]:
    """Entries whose publication date carries a time zone; one without is skipped, never given a default zone."""
    from catalyst_data.ingestion.time.times import stamp, utc_or_none
    out = []
    for loc, pub, title in _ENTRY.findall(xml):
        t = utc_or_none(pub)
        if t is not None:
            out.append((loc, stamp(t), html.unescape(title)))
    return out


def candidates(entries, proxy: str, start: str, cutoff: str, limit: int = MAX_PER_SESSION,
               broad_limit: int = 0):
    """Title matches first; then up to ``broad_limit`` market-session titles,
    which fetch_bodies keeps only when the lead names the commodity."""
    about = subject_matcher(proxy)
    window = [entry for entry in entries if start < entry[1] <= cutoff]
    named = [entry for entry in window if about(entry[2])][:limit]
    rest = [entry for entry in window if not about(entry[2])]
    broad = [entry for entry in rest if BROAD_TITLE.search(entry[2])]
    broad += [entry for entry in rest if not BROAD_TITLE.search(entry[2]) and MARKET_TITLE.search(entry[2])]
    return named + broad[:broad_limit]


_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def page_version(final_url: str, page_html: str) -> tuple[str, str] | None:
    """(createdAt, updatedAt) in UTC of the page's own article node, found by its
    URL alias in the embedded page data; None if it cannot be bound."""
    match = _NEXT_DATA.search(page_html)
    if not match:
        return None
    alias = urlparse(final_url).path
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("urlAlias") == alias and node.get("createdAt") and node.get("updatedAt"):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    try:
        walk(json.loads(match.group(1)))
    except ValueError:
        return None
    if not found:
        return None
    created = timefields.external_stamp(found[0]["createdAt"], source="kitco", field="createdAt")
    updated = timefields.external_stamp(found[0]["updatedAt"], source="kitco", field="createdAt")
    return (created, updated) if created and updated else None


def fetch_bodies(fetcher, wanted, proxy: str, start: str | None = None,
                 cutoff: str | None = None, conn=None) -> list[dict]:
    """Bodies whose own article version is published in the window and not
    updated after the cutoff (the sitemap date alone does not version the text)."""
    rows = []
    for url, published, title in wanted:
        known = store.stored_body(conn, url) if conn is not None else None
        if known is not None:
            # Fetched for another session or subject: re-tag, no refetch.
            in_window = cutoff is None or (start or "") < known["eligible_at"] <= cutoff
            if in_window and names_subject(proxy, known["title"], known["text"]):
                rows.append(store.retag_row(
                    known, [proxy], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                ))
            continue
        result = fetcher.fetch_page(url)
        if result is None:
            continue
        final_url, page_html = result
        version = page_version(final_url, page_html)
        from catalyst_data.ingestion.storage import page_versions
        if version is None:
            page_versions.record("kitco", url, final_url=final_url, page_html=page_html, subject=proxy,
                                 window_start=start, cutoff=cutoff)
        else:
            page_versions.record("kitco", url, final_url=final_url, published=version[0], modified=version[1],
                                 source="next_data", subject=proxy, window_start=start, cutoff=cutoff)
        if version is None:
            continue  # unverifiable version
        created, updated = version
        if cutoff is not None and (updated > cutoff or (start is not None and created <= start)):
            continue
        published = updated
        text = extract(page_html, title)
        if text is None:
            continue
        if not names_subject(proxy, title, text):
            continue
        image = page_image_with_source(page_html, final_url)
        rows.append({
            "asset_id": "web:" + hashlib.sha256(final_url.encode()).hexdigest()[:32],
            "text": text, "url": final_url, "title": title,
            "publisher": publisher_for_url(final_url), "source_class": "reported_news",
            "eligible_at": published,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "body_sha256": hashlib.sha256(text.encode()).hexdigest(), "tickers": [proxy],
            **({"image_url": image[0], "image_source": image[1]} if image else {}),
        })
    return rows
