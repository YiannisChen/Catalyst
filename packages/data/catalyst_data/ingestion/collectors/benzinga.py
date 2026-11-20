"""Benzinga monthly-archive discovery: dated reporting that ticker news feeds miss.

Finnhub/Polygon company news is ticker-scoped and capped, so a session's market
wrap, its analyst-ratings roundups and articles that name a company only in the
body never reach the store. Benzinga lists every article in public monthly
archive sitemaps (named in its robots.txt). Discovery here is generic:

- candidates are archive URLs whose slug names a session subject (the company,
  a peer, the commodity proxy's terms, a US market wrap) or that are the day's
  analyst-ratings roundups;
- the archive carries no dates, so an article's time is estimated from its
  (monotonic) id against known (id, published) pairs, and the fetched page's own
  ``datePublished`` decides the window; a page modified after the cutoff is
  dropped (point-in-time);
- each kept body is tagged with every subject whose matcher hits its title or
  lead, the same rule retrieval applies.
"""
from __future__ import annotations

import bisect
import hashlib
import html
import json
import re
from datetime import datetime, timedelta, timezone

from catalyst_data.ingestion.collectors.fetch import extract, page_image_with_source
from catalyst_data.ingestion.storage import store
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval.web_bodies import (
    TERM_SUBJECTS,
    names_subject,
    subject_matcher,
)

ARCHIVE = "https://www.benzinga.com/sitemap-benzinga.com/www/archive/{year}/{month:02d}.xml"
SOURCE = "benzinga"
MAX_FETCHES_PER_SESSION = 40
MAX_PER_SUBJECT = {"target": 12, "peer": 4, "proxy": 6, "sector": 6, "market": 6, "ratings": 6}
_LOC = re.compile(r"<loc>(https://www\.benzinga\.com/([^<]+?)/\d\d/\d\d/(\d+)/([^<]+))</loc>")
# US market wraps and session previews (slug words).
MARKET_SLUG = re.compile(
    r"\b(?:stock market today|stock market|wall street|dow jones|dow|s p 500|nasdaq|"
    r"us stocks|stocks close|stock futures|markets? (?:wrap|close))\b"
)
NON_US_SLUG = re.compile(r"\b(?:ftse|tsx|nikkei|sensex|nifty|hang seng|asx|europe|asia|india|china)\b")
RATINGS_SLUG = re.compile(r"\btop \d+ (?:upgrades|downgrades|initiations)\b")
_LD_JSON = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
_ARTICLE_TYPES = {"NewsArticle", "Article", "ReportageNewsArticle", "AnalysisNewsArticle"}


def article_metadata(page_html: str) -> dict | None:
    """The page's own NewsArticle object (JSON-LD), or None if there is not
    exactly one. Its dates, not the first date string on the page, version the
    extracted body (a WebPage or related-article object can carry others)."""
    found = []
    for block in _LD_JSON.findall(page_html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            kind = item.get("@type") if isinstance(item, dict) else None
            kinds = set(kind) if isinstance(kind, list) else {kind}
            if kinds & _ARTICLE_TYPES:
                found.append(item)
    return found[0] if len(found) == 1 else None


def parse_archive(xml: str) -> list[tuple[int, str, str, str]]:
    """(article id, url, section, slug words) for every archive entry."""
    return sorted(
        (int(aid), url, section, slug.replace("-", " ").lower())
        for url, section, aid, slug in _LOC.findall(xml)
    )


def _utc(value: str) -> datetime:
    return timefields.stored(value)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def calibration(conn) -> list[tuple[int, float]]:
    """Known (article id, published epoch) pairs from Benzinga bodies already stored."""
    pairs = {}
    for url, eligible in conn.execute(
        "SELECT canonical_url, eligible_at FROM web_body_chunks "
        "WHERE ordinal = 1 AND canonical_url LIKE 'https://www.benzinga.com/%'"
    ):
        match = re.search(r"/\d\d/\d\d/(\d+)/", url)
        if match:
            pairs[int(match.group(1))] = _utc(eligible).timestamp()
    return sorted(pairs.items())


def estimate_time(calib: list[tuple[int, float]], article_id: int) -> float | None:
    """Piecewise-linear id -> time; None outside the calibrated range."""
    ids = [p[0] for p in calib]
    i = bisect.bisect_left(ids, article_id)
    if i == 0 or i == len(ids):
        return None
    (a, ta), (b, tb) = calib[i - 1], calib[i]
    return ta + (tb - ta) * (article_id - a) / (b - a)


def _slug_matcher(subject: str):
    if subject == "SPY":
        return lambda slug: bool(MARKET_SLUG.search(slug)) and not NON_US_SLUG.search(slug)
    if subject in TERM_SUBJECTS:
        terms = re.compile(r"\b(?:" + "|".join(TERM_SUBJECTS[subject]) + r")\b")
        return lambda slug: bool(terms.search(slug))
    # Company: the SEC name word only. Slugs are lower-case, so the matcher's
    # case-sensitive ticker rule cannot fire on a stray word like "ma" or "v".
    return subject_matcher(subject)


def candidates(entries, subjects: dict[str, str], start: str, cutoff: str, calib,
               slack: timedelta = timedelta(hours=36)) -> list[tuple[str, str, set[str]]]:
    """Archive entries worth fetching: (url, kind, subjects named by the slug)."""
    lo = (_utc(start) - slack).timestamp()
    hi = (_utc(cutoff) + slack).timestamp()
    matchers = {s: _slug_matcher(s) for s in subjects}
    picked: dict[str, tuple[str, str, set[str]]] = {}
    counts: dict[str, int] = {}
    centre = _utc(cutoff).timestamp()
    in_range = []
    for article_id, url, section, slug in entries:
        when = estimate_time(calib, article_id)
        if when is not None and lo <= when <= hi:
            in_range.append((abs(when - centre), url, section, slug))
    for _dist, url, section, slug in sorted(in_range):
        named = {s for s, m in matchers.items() if m(slug)}
        kinds = [subjects[s] for s in named]
        if section.startswith("analyst-stock-ratings") and RATINGS_SLUG.search(slug):
            kinds.append("ratings")
        for kind in kinds:
            if counts.get(kind, 0) < MAX_PER_SUBJECT[kind] and url not in picked:
                counts[kind] = counts.get(kind, 0) + 1
                picked[url] = (url, kind, named)
    return list(picked.values())[:MAX_FETCHES_PER_SESSION]


def tag_subjects(subjects, title: str, text: str) -> list[str]:
    """Subjects a body is about. A ratings roundup is about the companies it
    names, never the market, a sector or a commodity ("Top Wall Street analysts
    changed their outlook ...")."""
    roundup = bool(RATINGS_SLUG.search(title.lower()))
    return [
        s for s in subjects
        if not (roundup and (s == "SPY" or s in TERM_SUBJECTS)) and names_subject(s, title, text)
    ]


def page_row(final_url: str, page_html: str, subjects, start: str, cutoff: str, fetched_at: str):
    """A storable row if the article was published in the window and its served
    version (dateModified, required) is not later than the cutoff."""
    meta = article_metadata(page_html)
    from catalyst_data.ingestion.storage import page_versions
    page_versions.record("benzinga", final_url, final_url=final_url, page_html=page_html,
                         window_start=start, cutoff=cutoff)
    if meta is None or not meta.get("datePublished") or not meta.get("dateModified"):
        return None  # unverifiable version
    published = timefields.external(meta["datePublished"], source="benzinga", field="datePublished").utc
    modified = timefields.external(meta["dateModified"], source="benzinga", field="datePublished").utc
    if published is None or modified is None:
        return None  # a time without a time zone is not given a default one (2026-10-22 review)
    eligible = published
    if not (_utc(start) < eligible <= _utc(cutoff)):
        return None
    if modified > _utc(cutoff):
        return None  # the served text may postdate the cutoff
    title = html.unescape(str(meta.get("headline") or "")).strip()
    if not title:
        return None
    text = extract(page_html, title)
    if text is None:
        return None
    tickers = tag_subjects(subjects, title, text)
    if not tickers:
        return None
    page_image = page_image_with_source(page_html, final_url)
    return {
        "asset_id": "web:" + hashlib.sha256(final_url.encode()).hexdigest()[:32],
        "text": text, "url": final_url, "title": title,
        "publisher": "Benzinga", "source_class": "reported_news",
        "eligible_at": _iso(eligible), "fetched_at": fetched_at,
        "body_sha256": hashlib.sha256(text.encode()).hexdigest(), "tickers": tickers,
        **({"image_url": page_image[0], "image_source": page_image[1]}
           if page_image else {}),
    }


_ARCHIVES: dict[tuple[int, int], list] = {}


def archive_entries(fetcher, start: str, cutoff: str) -> tuple[list, list[tuple[int, float]]]:
    """Entries of the archives the window touches, plus month-boundary anchors
    (a month's lowest id ~ its first instant, its highest ~ its last).

    The window's own months are required; slack-only neighbours are optional.
    """
    required = {(d.year, d.month) for d in (_utc(start), _utc(cutoff))}
    padded = {(d.year, d.month) for d in (_utc(start) - timedelta(days=2), _utc(cutoff) + timedelta(days=2))}
    entries, anchors = [], []
    for year, month in sorted(required | padded):
        if (year, month) not in _ARCHIVES:
            url = ARCHIVE.format(year=year, month=month)
            xml = None
            if fetcher.allowed(url):
                response = fetcher.get(url)
                if response.status_code == 200:
                    xml = response.text
            if xml is None:
                if (year, month) in required:
                    raise RuntimeError(f"archive unavailable: {year}-{month:02d}")
                continue
            _ARCHIVES[(year, month)] = parse_archive(xml)
        month_entries = _ARCHIVES[(year, month)]
        if month_entries:
            first = datetime(year, month, 1, tzinfo=timezone.utc)
            nxt = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
            anchors += [(month_entries[0][0], first.timestamp()), (month_entries[-1][0], nxt.timestamp())]
        entries.extend(month_entries)
    return entries, anchors


def discover(fetcher, conn, subjects: dict[str, str], start: str, cutoff: str,
             fetched_at: str) -> tuple[list[dict], int]:
    """Rows for a session's subjects ({ticker: kind}) and the number of candidates."""
    entries, anchors = archive_entries(fetcher, start, cutoff)
    # Stored (id, published) pairs refine the month anchors. No entries means
    # no usable archive: an error (retryable), not an empty result.
    if not entries:
        raise RuntimeError("benzinga archive empty for the window")
    known = dict(anchors)
    known.update(dict(calibration(conn)))
    calib = sorted(known.items())
    wanted = candidates(entries, subjects, start, cutoff, calib)
    rows = []
    for url, _kind, _named in wanted:
        known = store.stored_body(conn, url)
        if known is not None:
            # Fetched for another session: re-tag in this window, no refetch.
            if start < known["eligible_at"] <= cutoff:
                tickers = tag_subjects(subjects, known["title"], known["text"])
                if tickers:
                    rows.append(store.retag_row(known, tickers, fetched_at))
            continue
        result = fetcher.fetch_page(url)
        if result is None:
            continue
        row = page_row(result[0], result[1], subjects, start, cutoff, fetched_at)
        if row is not None:
            rows.append(row)
    return rows, len(wanted)


__all__ = ["SOURCE", "calibration", "candidates", "discover", "estimate_time", "page_row", "parse_archive"]
