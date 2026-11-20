"""Transactional, append-only writes for refreshed web bodies."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from catalyst_data.dedup.links import normalize_identity_url
from catalyst_data.ingestion import ticker_identity
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval import evidence_fts_projection, web_bodies
from catalyst_data.storage import body_versions, temporal_evidence

_asset_tracker = threading.local()


@contextmanager
def track_assets(asset_ids: set[str], on_asset: Callable[[str], None] | None = None):
    """Collect only newly stored body IDs from writes in the current thread."""
    previous = getattr(_asset_tracker, "asset_ids", None)
    previous_callback = getattr(_asset_tracker, "on_asset", None)
    _asset_tracker.asset_ids = asset_ids
    _asset_tracker.on_asset = on_asset
    try:
        yield asset_ids
    finally:
        if previous is None:
            del _asset_tracker.asset_ids
        else:
            _asset_tracker.asset_ids = previous
        if previous_callback is None:
            del _asset_tracker.on_asset
        else:
            _asset_tracker.on_asset = previous_callback

MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_asset_media (
    asset_id TEXT PRIMARY KEY,
    canonical_url TEXT,
    image_url TEXT NOT NULL,
    image_source TEXT NOT NULL CHECK (image_source IN ('og','twitter','polygon','finnhub')),
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_asset_media_misses (
    asset_id TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""


class RefreshBusy(RuntimeError):
    """Raised when another refresh owns the store lock."""


def open_writer(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(web_bodies.SCHEMA)
    conn.executescript(MEDIA_SCHEMA)
    return conn


@contextmanager
def refresh_lock(db_path: Path | str, *, wait_seconds: float = 0):
    """Lock one physical store, optionally waiting for a bounded interval.

    Backfill execution uses the default non-waiting mode so contention is
    reported as BUSY without recording a failed acquisition.
    """
    if wait_seconds < 0:
        raise ValueError("wait_seconds must be non-negative")
    canonical_path = Path(db_path).resolve()
    lock_path = Path(f"{canonical_path}.refresh.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise RefreshBusy("REFRESH_BUSY") from exc
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass
class StoreReport:
    stored_assets: int = 0
    chunks: int = 0
    duplicate_assets: int = 0
    body_changed: int = 0
    skipped_no_publisher: int = 0
    tagged: int = 0
    new_asset_ids: list[str] = field(default_factory=list)


def _track_new_asset(asset_id: str) -> None:
    tracker = getattr(_asset_tracker, "asset_ids", None)
    if tracker is not None:
        tracker.add(asset_id)
        callback = getattr(_asset_tracker, "on_asset", None)
        if callback is not None:
            callback(asset_id)


def _write_versioned_asset(conn: sqlite3.Connection, row: dict[str, Any], publisher: str) -> None:
    """Append the exact fetched body and its temporal/search metadata in the caller's transaction."""
    body_versions.ensure_schema(conn)
    temporal_evidence.ensure_schema(conn)
    evidence_fts_projection.ensure_schema(conn)
    accession = row.get("accession")
    is_filing = (
        isinstance(accession, str)
        and re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession) is not None
        and (urlsplit(row["url"]).hostname or "").lower() == "www.sec.gov"
    )
    source_id = row.get("source_id") or ("daily:sec" if is_filing else "daily:finnhub+polygon")
    document_id = row.get("document_id") or row["asset_id"]
    observed_at = timefields.stored(row["fetched_at"])
    published_raw = row.get("published_at") or row.get("eligible_at")
    published_at = timefields.stored_or_none(published_raw)
    provider_updated_at = timefields.stored_or_none(row.get("provider_updated_at"))
    first_seen_at = timefields.stored_or_none(row.get("first_seen_at"))
    try:
        version_id = body_versions.put_version(
            conn,
            source_id=source_id,
            document_id=document_id,
            title=row["title"],
            body=row["text"],
            observed_at=observed_at,
            first_seen_at=first_seen_at,
            published_at=published_at,
            provider_updated_at=provider_updated_at,
        )
    except body_versions.BodyVersionError as exc:
        raise body_versions.BodyVersionError(
            f"{exc} (source_id={source_id}, document_id={document_id}, asset_id={row['asset_id']})"
        ) from exc
    # A replay of an identical version keeps its first persisted observation (put_version); the projection must
    # carry that one, or a retry hours after a partial attempt would read as a different row.
    stored_observation = conn.execute("SELECT observed_at FROM body_versions WHERE version_id=?",
                                      (version_id,)).fetchone()
    version_observed_at = timefields.stored(stored_observation[0]) if stored_observation is not None else observed_at
    canonical_url = normalize_identity_url(row["url"])
    evidence_id = (
        "web:" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:32]
        if canonical_url else f"{source_id}:{document_id}"
    )
    origin = row.get("origin") or None
    kind = "filing" if is_filing else "body"
    evidence_role = "evidence" if is_filing else "lead"
    registered = False
    for raw_ticker in row.get("tickers") or ():
        try:
            ticker = ticker_identity.normalize_ticker(raw_ticker)
        except (TypeError, ValueError):
            continue
        existing_association = conn.execute(
            "SELECT association_observed_at FROM temporal_evidence_map "
            "WHERE version_id=? AND ticker=? AND kind=?",
            (version_id, ticker, kind),
        ).fetchone()
        temporal_evidence.register_version(
            conn,
            version_id=version_id,
            ticker=ticker,
            kind=kind,
            evidence_id=evidence_id,
            evidence_role=evidence_role,
            association_observed_at=(
                timefields.stored(existing_association[0])
                if existing_association is not None else observed_at
            ),
            eligible_at=timefields.stored_or_none(row.get("eligible_at")),
            published_at_source=row.get("published_at_source") or ("sec_index" if is_filing else "page"),
            published_at_precision=row.get("published_at_precision") or ("second" if is_filing else "unknown"),
            api_published=row.get("api_published"),
            page_published=row.get("page_published"),
            url=row["url"],
            canonical_url=canonical_url,
            publisher=publisher,
            publisher_family=row.get("publisher_family"),
            provider=row.get("provider") or source_id,
            source_class=row.get("source_class"),
            origin=origin,
            sec_accession=accession if is_filing else None,
            is_backfill=bool(row.get("is_backfill", False)),
            time_flags=row.get("time_flags") or (),
        )
        registered = True
    if registered and evidence_role == "evidence":
        evidence_class = "filing_evidence" if kind == "filing" else "body_evidence"
        evidence_fts_projection.project_evidence_text(
            conn,
            [evidence_fts_projection.EvidenceText(
                evidence_id=evidence_id,
                version_id=version_id,
                source_id=source_id,
                document_id=document_id,
                provider=row.get("provider") or source_id,
                kind=kind,
                evidence_class=evidence_class,
                title=row["title"],
                text=row["text"],
                origin=origin,
                provenance="versioned",
                observed_at=version_observed_at.isoformat(timespec="microseconds"),
                published_at=published_at.isoformat(timespec="microseconds") if published_at else None,
                provider_updated_at=provider_updated_at.isoformat(timespec="microseconds") if provider_updated_at else None,
            )],
        )


def _register_retagged_version(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Attach new tickers to an existing immutable version without rebuilding its body metadata."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_versions'"
    ).fetchone()
    if exists is None:
        return
    temporal_evidence.ensure_schema(conn)
    canonical_url = normalize_identity_url(row.get("url", ""))
    if not canonical_url:
        return

    identities = conn.execute(
        "SELECT DISTINCT v.source_id,v.document_id FROM body_versions v "
        "JOIN temporal_evidence_map m ON m.version_id=v.version_id "
        "WHERE m.canonical_url=?",
        (canonical_url,),
    ).fetchall()
    if len(identities) != 1:
        return
    source_id, document_id = identities[0]
    version = conn.execute(
        "SELECT v.version_id,v.observed_at FROM body_versions v "
        "JOIN temporal_evidence_map m ON m.version_id=v.version_id "
        "WHERE v.source_id=? AND v.document_id=? AND m.canonical_url=? "
        "ORDER BY v.observed_at DESC,v.rowid DESC LIMIT 1",
        (source_id, document_id, canonical_url),
    ).fetchone()
    if version is None:
        return

    columns = (
        "m.kind,m.eligible_at,m.evidence_id,m.url,m.canonical_url,m.publisher,"
        "m.publisher_family,m.provider,m.source_class,m.origin,m.evidence_role,"
        "m.is_backfill,m.time_flags_json,m.sec_accession,"
        "tv.published_at_source,tv.published_at_precision,tv.api_published,tv.page_published"
    )
    mappings = conn.execute(
        f"SELECT {columns} FROM temporal_evidence_map m "
        "JOIN temporal_evidence_versions tv ON tv.version_id=m.version_id "
        "WHERE m.version_id=? ORDER BY m.ticker",
        (version[0],),
    ).fetchall()
    if not mappings or any(mapping != mappings[0] for mapping in mappings[1:]):
        return
    (
        kind, eligible_at, evidence_id, url, stored_canonical_url, publisher,
        publisher_family, provider, source_class, origin, evidence_role,
        is_backfill, time_flags_json, sec_accession, published_at_source,
        published_at_precision, api_published, page_published,
    ) = mappings[0]
    try:
        time_flags = json.loads(time_flags_json)
        if not isinstance(time_flags, list) or any(not isinstance(flag, str) for flag in time_flags):
            return
        association_observed_at = timefields.stored(row["fetched_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return

    for raw_ticker in row.get("tickers") or ():
        try:
            ticker = ticker_identity.normalize_ticker(raw_ticker)
        except (TypeError, ValueError):
            continue
        already_registered = conn.execute(
            "SELECT 1 FROM temporal_evidence_map WHERE version_id=? AND ticker=? AND kind=?",
            (version[0], ticker, kind),
        ).fetchone()
        if already_registered is not None:
            continue
        temporal_evidence.register_version(
            conn,
            version_id=version[0],
            ticker=ticker,
            kind=kind,
            evidence_id=evidence_id,
            evidence_role=evidence_role,
            association_observed_at=association_observed_at,
            eligible_at=timefields.stored_or_none(eligible_at),
            published_at_source=published_at_source,
            published_at_precision=published_at_precision,
            api_published=api_published,
            page_published=page_published,
            url=url,
            canonical_url=stored_canonical_url,
            publisher=publisher,
            publisher_family=publisher_family,
            provider=provider,
            source_class=source_class,
            origin=origin,
            sec_accession=sec_accession,
            is_backfill=bool(is_backfill),
            time_flags=time_flags,
        )


def notify_assets_stored(report: StoreReport) -> None:
    """Publish new asset identities after a caller-owned transaction commits."""
    asset_ids, report.new_asset_ids = report.new_asset_ids, []
    for asset_id in asset_ids:
        _track_new_asset(asset_id)


def write_assets(
    conn: sqlite3.Connection,
    rows: Iterable[dict[str, Any]],
    *,
    commit: bool = True,
) -> StoreReport:
    if not commit and not conn.in_transaction:
        raise ValueError("commit=False requires a caller transaction")
    report = StoreReport()
    for row in rows:
        publisher = web_bodies.publisher_for_url(row["url"])
        if publisher is None:
            report.skipped_no_publisher += 1
            continue

        try:
            if commit:
                conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT body_sha256 FROM web_body_chunks WHERE asset_id = ? LIMIT 1",
                (row["asset_id"],),
            ).fetchone()
            if existing is not None:
                if row.get("_retag_only") is True:
                    _register_retagged_version(conn, row)
                else:
                    _write_versioned_asset(conn, row, publisher)
                for ticker in row.get("tickers") or ():
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO web_headlines VALUES (?,?,?)",
                        (row["asset_id"], ticker, row["eligible_at"]),
                    )
                    report.tagged += cursor.rowcount
                if existing[0] != row["body_sha256"]:
                    report.body_changed += 1
                else:
                    report.duplicate_assets += 1
                _write_media(conn, row)
                _write_structure(conn, row, commit=False)
                if commit:
                    conn.execute("COMMIT")
                continue

            if row.get("_retag_only") is True:
                raise ValueError("retag-only rows require an existing stored body")
            _write_versioned_asset(conn, row, publisher)
            stored_row = {**row, "publisher": publisher}
            chunks = web_bodies.chunk_text(row["text"])
            for ordinal, chunk in enumerate(chunks, start=1):
                conn.execute(
                    "INSERT INTO web_body_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["asset_id"], ordinal,
                        f"{row['asset_id']}:web_body:{ordinal:04d}", chunk,
                        hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                        row["body_sha256"], row["url"], row["title"], stored_row["publisher"],
                        row["source_class"], row["eligible_at"], row["fetched_at"],
                        "LIVE_REFETCH",
                    ),
                )
            for ticker in row.get("tickers") or ():
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO web_headlines VALUES (?,?,?)",
                    (row["asset_id"], ticker, row["eligible_at"]),
                )
                report.tagged += cursor.rowcount
            _write_media(conn, row)
            _write_structure(conn, row, commit=False)
            if commit:
                conn.execute("COMMIT")
                _track_new_asset(row["asset_id"])
            else:
                report.new_asset_ids.append(row["asset_id"])
            report.stored_assets += 1
            report.chunks += len(chunks)
        except Exception:
            if commit and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return report


def _write_structure(conn: sqlite3.Connection, row: dict[str, Any], *, commit: bool = True) -> None:
    """v2.1: an SEC exhibit's units (title, bullets, paragraphs) next to its body (catalyst_data.ingestion.collectors.sec_structure)."""
    if row.get("structure"):
        from catalyst_data.ingestion.collectors import sec_structure
        sec_structure.write(conn, row["url"], row.get("accession"), row["structure"])
        if commit:
            conn.commit()


def _write_media(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    if row.get("image_url") and row.get("image_source"):
        conn.execute(
            "INSERT OR REPLACE INTO web_asset_media "
            "(asset_id, canonical_url, image_url, image_source, checked_at) VALUES (?,?,?,?,?)",
            (row["asset_id"], row.get("url"), row["image_url"], row["image_source"],
             row.get("fetched_at") or ""),
        )


def stored_body(conn: sqlite3.Connection, url: str) -> dict[str, Any] | None:
    """The stored lead chunk of the body fetched from ``url``, if any (bodies are
    immutable, so a later session's discovery re-tags it without refetching)."""
    asset_id = "web:" + hashlib.sha256(url.encode()).hexdigest()[:32]
    row = conn.execute(
        "SELECT asset_id, title, text, canonical_url, eligible_at, body_sha256, source_class, publisher "
        "FROM web_body_chunks WHERE asset_id = ? AND ordinal = 1",
        (asset_id,),
    ).fetchone()
    if row is None:
        return None
    keys = ("asset_id", "title", "text", "url", "eligible_at", "body_sha256", "source_class", "publisher")
    return dict(zip(keys, row))


def retag_row(stored: dict[str, Any], tickers: list[str], fetched_at: str) -> dict[str, Any]:
    """A write_assets row that only adds subject tags to an existing body."""
    return {**stored, "fetched_at": fetched_at, "tickers": tickers, "_retag_only": True}



DEFERRED_SCHEMA = """
CREATE TABLE IF NOT EXISTS deferred_fetches (
    url TEXT NOT NULL,
    ticker TEXT NOT NULL,
    session_date TEXT NOT NULL,
    title TEXT NOT NULL,
    published TEXT NOT NULL,
    host TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL,          -- PENDING | DONE | NO_BODY | DEAD (host failed on DEAD_AFTER_DAYS days)
    attempts INTEGER NOT NULL DEFAULT 0,
    deferred_at TEXT NOT NULL,
    resolved_at TEXT,
    fail_days INTEGER NOT NULL DEFAULT 0,   -- distinct UTC days on which the host did not answer for this link
    last_fail_day TEXT,
    PRIMARY KEY (url, ticker)
);
"""

DEAD_AFTER_DAYS = 3


def ensure_deferred(conn: sqlite3.Connection) -> None:
    """Create the queue, or add the failure-day columns to a queue made before them.

    Links already waiting get the distinct days they have waited as their failure days."""
    conn.executescript(DEFERRED_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(deferred_fetches)")}
    if "fail_days" not in columns:
        conn.execute("ALTER TABLE deferred_fetches ADD COLUMN fail_days INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE deferred_fetches ADD COLUMN last_fail_day TEXT")
        # Distinct UTC days a link has waited (every daily run tried its host); today is already counted, so the
        # first run after the upgrade does not charge it a second time.
        conn.execute("UPDATE deferred_fetches SET fail_days = CAST(julianday(date('now')) - julianday(date(deferred_at)) "
                     "AS INTEGER), last_fail_day = date('now') WHERE status = 'PENDING'")
        conn.commit()


def charge_failed_day(conn: sqlite3.Connection, url: str, ticker: str, day: str) -> bool:
    """One more day on which the link's host did not answer; True when that makes the link permanently dead.

    Counted once per UTC day however often the day's runs retry. A dead link is no longer retried and no longer
    keeps the daily job PARTIAL; it is reported in the daily summary. ``revive`` undoes it."""
    conn.execute("UPDATE deferred_fetches SET attempts = attempts + 1, "
                 "fail_days = fail_days + (COALESCE(last_fail_day, '') != ?), last_fail_day = ? "
                 "WHERE url = ? AND ticker = ? AND status = 'PENDING'", (day, day, url, ticker))
    row = conn.execute("SELECT fail_days FROM deferred_fetches WHERE url = ? AND ticker = ?", (url, ticker)).fetchone()
    if row is not None and row[0] >= DEAD_AFTER_DAYS:
        conn.execute("UPDATE deferred_fetches SET status = 'DEAD', resolved_at = ? WHERE url = ? AND ticker = ?",
                     (day, url, ticker))
        return True
    return False


def revive(conn: sqlite3.Connection, host: str) -> int:
    """Queue a host's dead links again (the site is back)."""
    cursor = conn.execute("UPDATE deferred_fetches SET status = 'PENDING', fail_days = 0, resolved_at = NULL "
                          "WHERE host = ? AND status = 'DEAD'", (host,))
    conn.commit()
    return cursor.rowcount


def defer(conn: sqlite3.Connection, items: Iterable[dict[str, Any]], session_date: str,
          deferred_at: str) -> int:
    """Queue URLs whose host did not answer; they are fetched later, never dropped."""
    ensure_deferred(conn)
    added = 0
    for item in items:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO deferred_fetches (url, ticker, session_date, title, published, host, "
            "reason, status, deferred_at) VALUES (?,?,?,?,?,?,?, 'PENDING', ?)",
            (item["url"], item["ticker"], session_date, item["title"], item["published"],
             item["host"], item.get("reason"), deferred_at),
        )
        added += cursor.rowcount
    conn.commit()
    return added
