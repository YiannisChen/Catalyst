"""Page-declared version times, recorded for every page an intake path fetches (RG-17, step 1: record only).

Only Benzinga (``benzinga.page_row``) and Kitco (``kitco.fetch_bodies``) drop a page whose own version postdates the
session cutoff; the web-search cache and company news bodies do not look. Before deciding whether to filter there too
(a third-version decision), this records, for every fetched page on every path, the publication and modification
times the page declares and whether the modification is after the cutoff the page was fetched for. Recording never
filters and never raises into the caller.

The store is ``page_versions.sqlite3`` next to the bodies store (``use_store_dir``), or ``CATALYST_PAGE_VERSIONS_DB``.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from catalyst_data.ingestion.time import timefields

SCHEMA = """
CREATE TABLE IF NOT EXISTS page_versions (
    path TEXT NOT NULL,             -- company_body | benzinga | kitco | web_discovery
    url TEXT NOT NULL,
    final_url TEXT,
    subject TEXT,
    window_start TEXT,
    cutoff TEXT,
    declared_published TEXT,
    declared_modified TEXT,
    modified_source TEXT,           -- jsonld | meta | next_data | none
    modified_after_cutoff INTEGER,  -- 1, 0, or NULL when the page declares no modification time
    kept INTEGER,                   -- whether the path stored the page (1/0), NULL when not known here
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_page_versions_path ON page_versions(path, modified_after_cutoff);
CREATE TABLE IF NOT EXISTS body_times (   -- company bodies: the API's time and the page's own, and the one kept
    url TEXT NOT NULL,
    provider TEXT,                  -- finnhub | polygon
    publisher TEXT,
    api_published TEXT,
    page_published TEXT,
    eligible_at TEXT,               -- NULL when the time is imprecise and the body was not stored
    flag TEXT NOT NULL,             -- times.reconcile: agree | page_later | api_later | suspected_tz | page_only | api_only | imprecise
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_body_times_recorded ON body_times(recorded_at, publisher);
"""
_LOCK = threading.Lock()
_DIR: Path | None = None
_META = re.compile(
    r"""<meta[^>]+(?:property|name|itemprop)\s*=\s*["'](article:modified_time|og:updated_time|dateModified|"""
    r"""last-modified)["'][^>]*content\s*=\s*["']([^"']+)["']""", re.I)
_META_PUB = re.compile(
    r"""<meta[^>]+(?:property|name|itemprop)\s*=\s*["'](article:published_time|datePublished)["'][^>]*"""
    r"""content\s*=\s*["']([^"']+)["']""", re.I)


def use_store_dir(bodies_db) -> None:
    """Record next to this bodies store (called by the intake entry points)."""
    global _DIR
    if bodies_db:
        _DIR = Path(bodies_db).parent


def _path() -> Path | None:
    env = os.environ.get("CATALYST_PAGE_VERSIONS_DB", "").strip()
    if env:
        return Path(env)
    return (_DIR / "page_versions.sqlite3") if _DIR is not None else None


def _utc(value) -> str | None:
    return timefields.external_stamp(value, source="page", field="declared")


def declared_times(page_html: str) -> tuple[str | None, str | None, str]:
    """(published, modified, source) the page declares: its JSON-LD article node first, then meta tags."""
    from catalyst_data.ingestion.collectors import benzinga
    meta = None
    try:
        meta = benzinga.article_metadata(page_html or "")
    except Exception:  # noqa: BLE001
        meta = None
    if meta and (meta.get("dateModified") or meta.get("datePublished")):
        return _utc(meta.get("datePublished")), _utc(meta.get("dateModified")), (
            "jsonld" if meta.get("dateModified") else "none")
    mod = _META.search(page_html or "")
    pub = _META_PUB.search(page_html or "")
    return (_utc(pub.group(2)) if pub else None, _utc(mod.group(2)) if mod else None, "meta" if mod else "none")


def record_times(url: str, *, provider: str | None, publisher: str | None, api_published: str | None,
                 page_published: str | None, eligible_at: str | None, flag: str) -> None:
    """Keep both publication times of a company body and the one used (times.reconcile). Never raises."""
    try:
        db = _path()
        if db is None:
            return
        row = (url, provider, publisher, api_published, page_published, eligible_at, flag,
               datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        with _LOCK:
            conn = sqlite3.connect(db, timeout=10)
            try:
                conn.executescript(SCHEMA)
                conn.execute("INSERT INTO body_times VALUES (?,?,?,?,?,?,?,?)", row)
                conn.commit()
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        return


def record(path: str, url: str, *, final_url: str | None = None, page_html: str | None = None,
           published: str | None = None, modified: str | None = None, source: str | None = None,
           subject: str | None = None, window_start: str | None = None, cutoff: str | None = None,
           kept: bool | None = None) -> None:
    """Record one fetched page. Pass ``page_html`` to read the declared times from it, or ``published``/``modified``
    when the path already parsed them (Kitco's page data). Never raises."""
    try:
        db = _path()
        if db is None:
            return
        if page_html is not None and modified is None and published is None:
            published, modified, source = declared_times(page_html)
        cut = _utc(cutoff) if cutoff else None
        mod = _utc(modified) if modified else None
        after = None if (mod is None or cut is None) else int(mod > cut)
        row = (path, url, final_url, subject, _utc(window_start) if window_start else None, cut,
               _utc(published) if published else None, mod, source or ("none" if mod is None else "given"),
               after, None if kept is None else int(kept),
               datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        with _LOCK:
            conn = sqlite3.connect(db, timeout=10)
            try:
                conn.executescript(SCHEMA)
                conn.execute("INSERT INTO page_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
                conn.commit()
            finally:
                conn.close()
    except Exception:  # noqa: BLE001  (recording must never break intake)
        return
