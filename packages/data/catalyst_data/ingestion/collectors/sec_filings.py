"""SEC 8-K index per issuer, with the filing index page as time authority.

CATALYST_SEC_SUBMISSIONS_DIR (default data/m9/sec_submissions) holds the current and paginated submissions
JSON files plus one CIK/accession index-page cache. Submissions supplies candidate accessions and Items;
the index-page Accepted time is authoritative. Offline (CATALYST_SEC_OFFLINE=1) requires these caches and
fails closed when an index page is missing. Online fetches require SEC_USER_AGENT and respect SEC pacing.
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Iterator

from catalyst_data.ingestion.collectors import sec
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.errors import StoreReadError
from catalyst_data.storage.runtime_paths import runtime_store_path

REFRESH_HOURS = 6


def _dir() -> Path:
    return runtime_store_path("sec_submissions_dir")


def cache_dir() -> Path:
    """Where submissions pages and immutable filing index pages are kept."""
    return _dir()


def _offline() -> bool:
    return os.environ.get("CATALYST_SEC_OFFLINE") == "1"


_CACHED_ONLY: ContextVar[bool] = ContextVar("sec_filings_cached_only", default=False)


@contextmanager
def cached_only() -> Iterator[None]:
    """Read only what is cached, and send no SEC request (used inside a run).

    A live run must not wait on SEC: one issuer can have hundreds of 8-K index pages. Missing
    submissions mean no filings; a missing archive file is skipped; a missing index page keeps the
    submissions time (flag ``index_missing``), as a failed fetch already does. The daily job still
    fetches and caches everything."""
    token = _CACHED_ONLY.set(True)
    try:
        yield
    finally:
        _CACHED_ONLY.reset(token)


def normalize_ticker(ticker: str) -> str:
    """Normalize punctuation in class-share tickers (for example, BRK.B and BRK-B)."""
    return ticker.strip().upper().replace(".", "-")


def _cik_for(ticker: str) -> str | None:
    """Resolve the SEC's current ticker table, accepting either class-share separator."""
    normalized = normalize_ticker(ticker)
    cik = sec.cik_for(normalized)
    if cik is not None:
        return cik
    # SEC's current ticker file commonly uses dots while data consumers use hyphens.
    return sec.cik_for(normalized.replace("-", "."))


def _load(ticker: str) -> dict | None:
    cik = _cik_for(ticker)
    if cik is None:
        return None
    path = _dir() / f"{cik}.json"
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < REFRESH_HOURS * 3600
    if path.exists() and (_offline() or fresh or _CACHED_ONLY.get()):
        try:
            return json.loads(path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StoreReadError(path, "read SEC submissions cache", str(exc)) from exc
    if _CACHED_ONLY.get():
        return None
    if _offline():
        cause = FileNotFoundError(path)
        raise StoreReadError(path, "read SEC submissions cache", str(cause)) from cause
    agent = os.environ.get("SEC_USER_AGENT")
    if agent:
        try:
            import requests
            time.sleep(sec.SEC_INTERVAL_S)
            resp = requests.get(f"{sec.SUBMISSIONS_BASE}/submissions/CIK{cik}.json",
                                headers={"User-Agent": agent}, timeout=30)
            resp.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(resp.text)
            return resp.json()
        except Exception as fetch_error:
            if path.exists():
                try:
                    return json.loads(path.read_text())
                except (OSError, UnicodeError, json.JSONDecodeError) as read_error:
                    raise StoreReadError(path, "read SEC submissions cache", str(read_error)) from read_error
            raise StoreReadError(path, "load SEC submissions", str(fetch_error)) from fetch_error
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StoreReadError(path, "read SEC submissions cache", str(exc)) from exc
    cause = FileNotFoundError(path)
    raise StoreReadError(path, "read SEC submissions cache", str(cause)) from cause


def _load_archive(name: str) -> dict | None:
    """Load an immutable SEC submissions archive file from cache or SEC (None: not cached, cached-only)."""
    if not re.fullmatch(r"CIK\d+-submissions-\d+\.json", name):
        raise ValueError(f"invalid SEC submissions archive filename: {name!r}")
    path = _dir() / name
    if path.exists():
        return json.loads(path.read_text())
    if _CACHED_ONLY.get():
        return None
    if _offline():
        cause = FileNotFoundError(f"SEC submissions archive is not cached: {name}")
        raise StoreReadError(path, "read SEC submissions cache", str(cause)) from cause
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        raise RuntimeError("SEC_USER_AGENT is required to fetch submissions archive files")
    import requests

    time.sleep(sec.SEC_INTERVAL_S)
    response = requests.get(f"{sec.SUBMISSIONS_BASE}/submissions/{name}",
                            headers={"User-Agent": agent}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return payload


def _index_accepted_at(cik: str, accession: str):
    """Return SEC index-page Accepted time, cached by CIK/accession.

    Submissions acceptanceDateTime is only a candidate bound; it is not the
    filing time because some issuers' values are four or five hours late.
    """
    cache = _dir() / f"CIK{cik}-{accession}-index.json"
    if cache.exists():
        payload = json.loads(cache.read_text())
        raw = payload.get("accepted_at")
        if raw:
            return timefields.stored(raw)
        if _offline():
            return None
    if _CACHED_ONLY.get():
        return None
    if _offline():
        raise FileNotFoundError(f"SEC filing index is not cached: {accession}")
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        raise RuntimeError("SEC_USER_AGENT is required to fetch filing index pages")
    import requests

    time.sleep(sec.SEC_INTERVAL_S)
    try:
        _, accepted_at = sec.index(cik, accession, http_get=requests.get, user_agent=agent)
    except Exception:
        # The submissions cache still has a candidate time. A failed index fetch
        # must not turn that cached filing into a crash.
        return None
    if not accepted_at:
        # A missing Accepted field is not an immutable filing fact. Retry on
        # the next online invocation, including for older null cache entries.
        return None
    parsed = timefields.stored(accepted_at)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"accession": accession, "accepted_at": accepted_at}))
    return parsed


def _parallel_filing_count(recent: dict) -> int:
    """Validate the required SEC filing arrays before treating them as rows."""
    if not isinstance(recent, dict):
        raise ValueError("SEC filing arrays must be an object")
    fields = ("accessionNumber", "acceptanceDateTime", "form", "items")
    lengths = []
    for field in fields:
        values = recent.get(field)
        if not isinstance(values, list):
            raise ValueError(f"SEC parallel filing arrays require list field {field}")
        lengths.append(len(values))
    if len(set(lengths)) > 1:
        raise ValueError("SEC parallel filing arrays have mismatched lengths")
    return lengths[0]


def _filing_rows(recent: dict) -> list[dict]:
    return [row for _index, row in _filing_rows_with_indices(recent)]


def _filing_rows_with_indices(recent: dict) -> list[tuple[int, dict]]:
    count = _parallel_filing_count(recent)
    out = []
    for index in range(count):
        filing = sec._filing_row(recent, index)
        if filing is None:
            continue
        out.append((index, {
            "accession": filing["accession"],
            "accepted_at": timefields.stored(filing["accepted_at"]),
            "items": filing["items"],
            "form": filing["form"],
        }))
    return out


def candidates_for_window(
    cik: str | int,
    start: str,
    cutoff: str,
    *,
    http_get,
    user_agent: str,
) -> list[dict]:
    """Enumerate SEC 8-K candidates for a daily window, oldest first.

    ``acceptanceDateTime`` is provisional and has no verified maximum skew from
    the index-page Accepted time. Therefore this routine does not use it to
    prune candidates: it reads every advertised submissions archive and returns
    every 8-K/8-K/A. The caller must inspect each filing index and establish
    actual window membership there. A failed page or malformed array raises,
    so callers cannot mistake partial enumeration for an empty window.
    """
    padded_cik = str(cik).zfill(10)
    if not re.fullmatch(r"\d{10}", padded_cik):
        raise ValueError("SEC CIK must contain at most ten digits")
    # Validate the requested observation window even though provisional JSON
    # timestamps cannot safely narrow the candidate set.
    start_utc = timefields.stamp(timefields.stored(start))
    cutoff_utc = timefields.stamp(timefields.stored(cutoff))
    if start_utc >= cutoff_utc:
        raise ValueError("SEC candidate window must be increasing")
    if _offline():
        raise RuntimeError("offline submissions cache cannot prove a fresh complete enumeration")

    root_url = f"{sec.SUBMISSIONS_BASE}/submissions/CIK{padded_cik}.json"
    # Share the legacy current-submissions cache filename, but never accept it
    # as a substitute for an online refresh in this completeness-sensitive path.
    root_path = _dir() / f"{padded_cik}.json"

    def load_page(url: str, cache_path: Path) -> dict:
        if _offline():
            if not cache_path.is_file():
                raise FileNotFoundError(f"SEC submissions page is not cached: {cache_path.name}")
            payload = json.loads(cache_path.read_text())
        else:
            if not user_agent:
                raise RuntimeError("SEC_USER_AGENT is required to enumerate submissions")
            response = http_get(url, headers={"User-Agent": user_agent}, timeout=30)
            payload = sec._response_json(response)
        if not isinstance(payload, dict):
            raise ValueError("SEC submissions page must be a JSON object")
        if not _offline():
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload))
        return payload

    root = load_page(root_url, root_path)
    issuer = root.get("name")
    filings = root.get("filings")
    if not isinstance(filings, dict):
        raise ValueError("SEC submissions page is missing filings object")
    recent = filings.get("recent")
    if not isinstance(recent, dict):
        raise ValueError("SEC submissions page is missing recent filings")
    archive_refs = filings.get("files")
    if not isinstance(archive_refs, list):
        raise ValueError("SEC submissions page is missing archive list")

    pages = [(root, recent)]
    archive_names = []
    for ref in archive_refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("name"), str):
            raise ValueError("SEC submissions archive reference is malformed")
        name = ref["name"]
        if not re.fullmatch(rf"CIK{padded_cik}-submissions-\d+\.json", name):
            raise ValueError("SEC submissions archive name does not match CIK")
        if name in archive_names:
            raise ValueError("SEC submissions archive list contains duplicate page")
        archive_names.append(name)

    for name in archive_names:
        payload = load_page(
            f"{sec.SUBMISSIONS_BASE}/submissions/{name}", _dir() / name
        )
        archived = payload.get("filings", payload)
        if not isinstance(archived, dict):
            raise ValueError("SEC archive is missing filing arrays")
        pages.append((payload, archived))

    by_accession: dict[str, dict] = {}
    for payload, arrays in pages:
        primary_docs = arrays.get("primaryDocument")
        descriptions = arrays.get("primaryDocDescription")
        company_name = payload.get("name") or issuer or ""
        for index, row in _filing_rows_with_indices(arrays):
            # Preserve optional metadata only when the corresponding optional
            # array has a value at this index; it is not used to infer scope.
            description = (
                descriptions[index]
                if isinstance(descriptions, list) and index < len(descriptions)
                else None
            )
            primary_document = (
                primary_docs[index]
                if isinstance(primary_docs, list) and index < len(primary_docs)
                else None
            )
            candidate = {
                **row,
                "accepted_at": timefields.stamp(row["accepted_at"]),
                "json_accepted_at": timefields.stamp(row["accepted_at"]),
                "company": company_name,
                "description": description if isinstance(description, str) else None,
                "primary_document": (
                    primary_document if isinstance(primary_document, str) else None
                ),
            }
            previous = by_accession.get(row["accession"])
            if previous is not None and previous != candidate:
                raise ValueError("SEC submissions pages disagree on an accession")
            by_accession[row["accession"]] = candidate

    return sorted(
        by_accession.values(),
        key=lambda filing: (filing["accepted_at"], filing["accession"]),
    )


def filings_8k(ticker: str) -> list[dict]:
    """Issuer 8-K and 8-K/A filings, including SEC history files, oldest first."""
    data = _load(ticker)
    if not data:
        return []
    filings = data.get("filings", {})
    out = _filing_rows(filings.get("recent", {}))
    seen = {filing["accession"] for filing in out}
    for archive in filings.get("files", []):
        archived = _load_archive(archive["name"])
        if archived is None:
            continue
        for filing in _filing_rows(archived.get("filings", archived)):
            if filing["accession"] not in seen:
                out.append(filing)
                seen.add(filing["accession"])
    cik = _cik_for(ticker)
    assert cik is not None
    verified = []
    for filing in out:
        json_accepted_at = timefields.stamp(filing["accepted_at"])
        index_accepted_at = _index_accepted_at(cik, filing["accession"])
        if index_accepted_at:
            accepted_at, flag = sec.acceptance(
                json_accepted_at,
                timefields.stamp(index_accepted_at),
            )
        else:
            # The index page corrects a submissions time when it can be read.
            # A failed or missing fetch keeps the cached submissions time. Dropping
            # the filing would look like the issuer had no 8-K.
            accepted_at, flag = json_accepted_at, "index_missing"
        if accepted_at is None:
            continue
        verified.append({
            **filing,
            "json_accepted_at": filing["accepted_at"],
            "accepted_at": timefields.stored(accepted_at),
            "time_flag": flag,
        })
    return sorted(verified, key=lambda filing: (filing["accepted_at"], filing["accession"]))


def between(ticker: str, start: datetime, end: datetime, *, item: str | None = None) -> list[dict]:
    """Filings accepted in (start, end], optionally only those listing ``item``."""
    return [f for f in filings_8k(ticker) if start < f["accepted_at"] <= end and (item is None or item in f["items"])]


__all__ = ["between", "candidates_for_window", "filings_8k", "normalize_ticker"]


def company(ticker: str) -> str | None:
    """The issuer's name as the SEC submissions index gives it (v2.1 filing fact)."""
    data = _load(ticker)
    return (data or {}).get("name")
