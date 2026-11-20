"""SEC 8-K and exhibit intake for the M9 web-body store.

The 2026-10-22 review observed a 4/5-hour JSON delay in the checked sample,
but that sample is not a provider-guaranteed upper bound. Daily completeness
uses exhaustive submissions pagination and the index page's Accepted time;
``list_8k`` remains a bounded convenience query, not a completeness proof.
Both timestamps are retained in ``body_times`` (page_versions).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse

from catalyst_data.ingestion.collectors import headlines, market
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.runtime_paths import default_sec_tickers_path

RELEVANT_ITEMS = {"1.01", "2.01", "2.02", "5.02", "7.01", "8.01"}
JSON_LATE_MAX_H = 5          # historical convenience-query heuristic, not a proven bound
TOLERANCE_S = 120
_ACCEPTED = re.compile(r'Accepted</div>\s*<div class="info">\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
NY = timefields.NY
SEC_INTERVAL_S = 0.2
SEC_BASE = "https://www.sec.gov"
SUBMISSIONS_BASE = "https://data.sec.gov"


def _tickers_path() -> Path:
    from catalyst_data.retrieval import web_bodies

    return web_bodies.name_map_path() or default_sec_tickers_path(market.REPO)


def cik_for(ticker: str, tickers_json=None) -> str | None:
    """Return the SEC's zero-padded CIK for a ticker, when it is mapped."""
    if tickers_json is None:
        tickers_json = _tickers_path()
    if isinstance(tickers_json, (str, Path)):
        tickers_json = json.loads(Path(tickers_json).read_text())
    for item in tickers_json.values():
        if str(item.get("ticker", "")).upper() == ticker.upper():
            return str(item["cik_str"]).zfill(10)
    return None


def _response_json(response):
    response.raise_for_status()
    return response.json()


def _response_text(response):
    response.raise_for_status()
    return response.text


def _utc(value: str) -> str:
    return timefields.stamp(timefields.stored(value))


def _parallel_filing_count(recent: dict, fields: tuple[str, ...]) -> int:
    """Validate the required SEC filing arrays before zipping their entries."""
    if not isinstance(recent, dict):
        raise ValueError("SEC recent filings must be an object")
    lengths = []
    for field in fields:
        values = recent.get(field)
        if not isinstance(values, list):
            raise ValueError(f"SEC parallel filing arrays require list field {field}")
        lengths.append(len(values))
    if len(set(lengths)) > 1:
        raise ValueError("SEC parallel filing arrays have mismatched lengths")
    return lengths[0]


def _filing_row(recent: dict, index: int) -> dict | None:
    """Validate fields needed to classify and retain an 8-K row.

    A well-formed non-8-K form is enough to skip that row; fields irrelevant to
    this endpoint are not required to be populated for other form types.
    """
    form = recent["form"][index]
    if not isinstance(form, str) or not form.strip():
        raise ValueError("SEC filing form must be nonempty text")
    form = form.strip()
    if form not in ("8-K", "8-K/A"):
        return None

    accession = recent["accessionNumber"][index]
    if not isinstance(accession, str) or not accession.strip():
        raise ValueError("SEC 8-K accessionNumber must be nonempty text")
    accepted_at = timefields.external_stamp(
        recent["acceptanceDateTime"][index], source="sec", field="acceptanceDateTime"
    )
    if accepted_at is None:
        raise ValueError("SEC 8-K acceptanceDateTime must be a valid zoned timestamp")
    raw_items = recent["items"][index]
    if not isinstance(raw_items, str):
        raise ValueError("SEC 8-K items must be text")
    items = [value.strip() for value in raw_items.split(",") if value.strip()]
    return {
        "accession": accession.strip(),
        "accepted_at": accepted_at,
        "items": items,
        "form": form,
    }


def list_8k(
    cik: str | int,
    start: str,
    cutoff: str,
    *,
    http_get: Callable,
    user_agent: str,
) -> list[dict]:
    padded_cik = str(cik).zfill(10)
    response = http_get(
        f"{SUBMISSIONS_BASE}/submissions/CIK{padded_cik}.json",
        headers={"User-Agent": user_agent}, timeout=30,
    )
    payload = _response_json(response)
    recent = payload.get("filings", {}).get("recent", {})
    count = _parallel_filing_count(
        recent, ("accessionNumber", "acceptanceDateTime", "form", "items")
    )
    primary_documents = recent.get("primaryDocument")
    descriptions = recent.get("primaryDocDescription")
    start_utc = _utc(start)
    # candidates by the JSON time, which may be up to JSON_LATE_MAX_H late; the index page's time decides
    late_utc = timefields.stamp(timefields.stored(cutoff) + timedelta(hours=JSON_LATE_MAX_H))
    out = []
    for index in range(count):
        filing = _filing_row(recent, index)
        if filing is None:
            continue
        accepted_at = filing["accepted_at"]
        if not start_utc < accepted_at <= late_utc:
            continue
        items = filing["items"]
        if not (set(items) & RELEVANT_ITEMS):
            continue
        out.append({
            "accession": filing["accession"],
            "accepted_at": accepted_at,
            "items": items,
            "primary_document": (
                primary_documents[index]
                if isinstance(primary_documents, list) and index < len(primary_documents)
                else None
            ),
            "description": (
                descriptions[index]
                if isinstance(descriptions, list) and index < len(descriptions)
                else None
            ),
            "company": payload.get("name", ""),
        })
    return out


def _document_url(href: str) -> str:
    href = unquote(href.strip())
    if href.startswith("/ix?doc="):
        href = href[len("/ix?doc="):]
    return urljoin(SEC_BASE, href)


def index_url(cik: str | int, accession: str) -> str:
    return f"{SEC_BASE}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"


def index_accepted(html: str) -> str | None:
    """EDGAR's acceptance time from a filing index page ("Accepted", New York time), as UTC; None if absent."""
    m = _ACCEPTED.search(html)
    if m is None:
        return None
    return timefields.external_stamp(m.group(1), source="sec", field="index_accepted")


def acceptance(json_utc: str, index_utc: str | None) -> tuple[str | None, str]:
    """(the time to use, flag): the index page's time when there is one. Flags: agree, api_offset (the JSON is a
    whole 4 or 5 h later or earlier: the known error, corrected), disagree (any other difference), index_missing."""
    if index_utc is None:
        return None, "index_missing"
    diff = (timefields.stored(json_utc) - timefields.stored(index_utc)).total_seconds()
    if abs(diff) <= TOLERANCE_S:
        return index_utc, "agree"
    if any(abs(abs(diff) - h * 3600) <= TOLERANCE_S for h in (4, 5)):
        return index_utc, "api_offset"
    return index_utc, "disagree"


def exhibits(
    cik: str | int,
    accession: str,
    *,
    http_get: Callable,
    user_agent: str,
) -> list[dict]:
    return index(cik, accession, http_get=http_get, user_agent=user_agent)[0]


def index(cik: str | int, accession: str, *, http_get: Callable, user_agent: str,
          cache_dir: Path | None = None) -> tuple[list[dict], str | None]:
    """The filing's documents to store (EX-99 exhibits, else the primary 8-K) and its acceptance time (UTC).

    A filing's index page does not change once accepted, so with ``cache_dir`` a lookup that found an Accepted time
    is kept and not fetched again (the daily job asks about every 8-K an issuer ever filed, every day). A page
    without the time is not kept: that is a retry, not a fact."""
    cache = (Path(cache_dir) / f"CIK{str(cik).zfill(10)}-{accession}-indexpage.json") if cache_dir else None
    if cache is not None and cache.is_file():
        try:
            stored = json.loads(cache.read_text())
            if stored.get("accepted_at") and isinstance(stored.get("documents"), list):
                return stored["documents"], stored["accepted_at"]
        except (OSError, ValueError):
            pass  # unreadable cache: fetch again
    response = http_get(index_url(cik, accession), headers={"User-Agent": user_agent}, timeout=30)
    html = _response_text(response)
    documents, accepted_at = _documents(html), index_accepted(html)
    if cache is not None and accepted_at:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"accession": accession, "accepted_at": accepted_at, "documents": documents}))
        tmp.replace(cache)
    return documents, accepted_at


def _documents(html: str) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    primary = None
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        link = cells[2].find("a", href=True)
        if link is None:
            continue
        kind = " ".join(cells[3].stripped_strings).upper()
        href = link["href"]
        url = _document_url(href)
        filename = urlparse(url).path.rsplit("/", 1)[-1]
        extension = Path(filename).suffix.lower()
        if kind.startswith("EX-99") and extension in (".htm", ".html", ".txt"):
            candidates.append({"type": kind, "filename": filename, "url": url})
        elif kind == "8-K":
            primary = {"type": "8-K", "filename": filename, "url": url}
    return candidates if candidates else ([primary] if primary else [])


def exhibit_text(html: str) -> str | None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    # Prose lives in innermost p/div/li blocks, and in table cells when a
    # release is laid out with tables (Visa). Each block is kept or dropped on
    # its own: numeric financial-table cells fail the prose filter below, so
    # tables are never flattened into figure strings (the c05/c09 failure).
    blocks = []
    block_tags = ["p", "div", "li", "td"]
    for block in soup.find_all(block_tags):
        # Innermost blocks only: EDGAR HTML nests divs, and a parent's text
        # would repeat every child paragraph.
        if block.find(block_tags) is not None:
            continue
        text = " ".join(block.get_text(" ", strip=True).split())
        if len(text) < 40:
            continue
        letters = sum(character.isalpha() for character in text)
        if letters / len(text) < 0.5:
            continue
        if not blocks or blocks[-1] != text:
            blocks.append(text)
    from catalyst_data.ingestion.collectors.fetch import strip_zero_width

    result = strip_zero_width("\n\n".join(blocks)).strip()
    return result if len(result) >= 300 else None


def _attempt_time(now: datetime | None) -> str:
    return timefields.stamp(timefields.now_utc(now))


def intake(
    ticker: str,
    session_date: str,
    *,
    http_get: Callable | None = None,
    sleep: Callable[[float], None] | None = None,
    user_agent: str | None = None,
    tickers_json=None,
    now: datetime | None = None,
) -> tuple[list[dict], str]:
    if user_agent is None:
        user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        return [], "DISABLED"
    if http_get is None:
        import requests

        http_get = requests.get
    if sleep is None:
        sleep = time.sleep
    cik = cik_for(ticker, tickers_json)
    if cik is None:
        return [], "EMPTY"

    last_request = [None]

    def paced_get(url, **kwargs):
        if last_request[0] is not None:
            sleep(SEC_INTERVAL_S)
        last_request[0] = True
        return http_get(url, **kwargs)

    start, cutoff = headlines.session_window(session_date)
    rows, failed = [], False
    try:
        filings = list_8k(cik, start, cutoff, http_get=paced_get, user_agent=user_agent)
    except Exception:
        return [], "ERROR"
    for filing in filings:
        filing_rows, filing_failed = rows_for_filing(
            cik, ticker, filing, http_get=paced_get, user_agent=user_agent, now=now, window=(start, cutoff),
        )
        rows += filing_rows
        failed = failed or filing_failed
    if rows:
        return rows, "OK"
    return [], "ERROR" if failed else "EMPTY"


def rows_for_filing(
    cik: str | int,
    ticker: str,
    filing: dict,
    *,
    http_get: Callable,
    user_agent: str,
    now: datetime | None = None,
    window: tuple[str, str] | None = None,
    cache_dir: Path | None = None,
) -> tuple[list[dict], bool]:
    """Store rows for one 8-K's exhibits (or primary document); ``True`` when a fetch failed.

    The filing's time is EDGAR's acceptance from the index page: ``filing["accepted_at"]`` is replaced by it (the
    JSON's value moves to ``json_accepted_at``). A filing whose index page shows no acceptance time counts as a
    failed fetch (retried with the item), and one outside ``window`` (start, cutoff] by that time gives no rows."""
    rows, failed = [], False
    try:
        documents, index_utc = index(cik, filing["accession"], http_get=http_get, user_agent=user_agent,
                                     cache_dir=cache_dir)
    except Exception:
        return [], True
    json_utc = filing.get("json_accepted_at") or filing["accepted_at"]
    accepted, flag = acceptance(json_utc, index_utc)
    from catalyst_data.ingestion.storage import page_versions
    page_versions.record_times(index_url(cik, filing["accession"]), provider="sec_submissions",
                               publisher="SEC filing", api_published=json_utc, page_published=index_utc,
                               eligible_at=accepted, flag=flag)
    if accepted is None:
        return [], True
    filing["json_accepted_at"], filing["accepted_at"], filing["time_flag"] = json_utc, accepted, flag
    if window is not None and not _utc(window[0]) < accepted <= _utc(window[1]):
        return [], False
    if not documents:
        return [], True
    for document in documents:
        try:
            response = http_get(document["url"], headers={"User-Agent": user_agent}, timeout=30)
            page = _response_text(response)
            text = exhibit_text(page)
            from catalyst_data.ingestion.collectors import sec_structure
            structure = sec_structure.parse(page) if text is not None else None
        except Exception:
            failed = True
            continue
        if text is None:
            failed = True
            continue
        url = document["url"]
        items_text = ",".join(filing["items"])
        detail = filing["description"] or f"Items {items_text}"
        rows.append({
            "asset_id": "web:" + hashlib.sha256(url.encode()).hexdigest()[:32],
            "text": text,
            "url": url,
            "title": f"{filing['company']} ({ticker}) Form 8-K {document['type']}: {detail}",
            "publisher": "SEC filing",
            "source_class": "issuer_disclosure",
            "eligible_at": filing["accepted_at"],
            "fetched_at": _attempt_time(now),
            "body_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "tickers": [ticker],
            "accession": filing["accession"],
            "structure": structure,
        })
    return rows, failed
