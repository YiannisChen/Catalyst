"""Alpaca market data (free account): Benzinga news with full text, and minute bars. Both can be back-filled.

News (/v1beta1/news) is Benzinga's wire, history from 2015, with ``created_at`` (publication, to the second, UTC) and
``updated_at`` (last modification). Each article is stored once with ``first_seen_at``; every distinct ``updated_at``
seen is kept in ``news_versions`` (RG-17 step 1: recorded, nothing filtered). A reader asking "what was public at t"
takes ``created_at <= t``; a body whose ``updated_at`` is after t may have changed since. Checked 2026-10-21 on the
server: 28 of 30 Benzinga bodies already in the store were found, text similarity median 0.95.

Bars (/v2/stocks/bars, feed=sip): consolidated 1-minute bars incl. pre/post market, from 2016; the free plan cannot
ask for the last 15 minutes. 200 requests per minute, drawn here from the shared ``ratelimit.ALPACA`` budget.

    python -m catalyst_data.ingestion.collectors.alpaca news-poll --db /opt/catalyst/archive/alpaca_news.sqlite3 [--minutes 90]
    python -m catalyst_data.ingestion.collectors.alpaca news-backfill --db ... --dates dates.txt [--symbols-file ...]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

import requests

from catalyst_data.dedup import outbox as dedup_outbox
from catalyst_data.dedup.links import EvidenceRecord, normalize_identity_url
from catalyst_data.ingestion import (
    coverage,
    ledger,
    ratelimit,
    resilient,
    ticker_identity,
)
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval import evidence_fts_projection
from catalyst_data.storage import body_versions, temporal_evidence

SOURCE = "alpaca_news"
http_get: Callable[..., Any] = requests.get
PAGE = 50
COVERAGE_POLICY_VERSION = "alpaca_news_coverage_v1"
POLL_LOCK_WAIT_SECONDS = 15
POLL_RETRY_FALLBACK_DAYS = 3  # widest gap one poll refills

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT, headline TEXT NOT NULL, summary TEXT,
    content TEXT, url TEXT, author TEXT, source TEXT, symbols_json TEXT NOT NULL, first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_created ON news(created_at);
CREATE TABLE IF NOT EXISTS news_symbols (id INTEGER NOT NULL, symbol TEXT NOT NULL, PRIMARY KEY (id, symbol));
CREATE INDEX IF NOT EXISTS idx_news_symbols ON news_symbols(symbol);
CREATE TABLE IF NOT EXISTS news_versions (
    id INTEGER NOT NULL, updated_at TEXT NOT NULL, seen_at TEXT NOT NULL, content_chars INTEGER,
    PRIMARY KEY (id, updated_at)
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _headers() -> dict:
    return {"APCA-API-KEY-ID": os.environ.get("APCA_API_KEY_ID", "").strip().strip('"'),
            "APCA-API-SECRET-KEY": os.environ.get("APCA_API_SECRET_KEY", "").strip().strip('"')}


def configured() -> bool:
    return all(_headers().values())


def _poll_retry_path(db_path: str | Path) -> Path:
    return Path(f"{Path(db_path).resolve()}.news-poll-retry.json")


def _poll_retry_marker_exists(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def _write_poll_retry(path: Path, started_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({"schema_version": 1, "started_at": _stamp(started_at)}, sort_keys=True))
        try:
            # Link is atomic and does not replace an older pending window if
            # two poll processes initialize the marker at once.
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _poll_retry_minutes(path: Path, requested_minutes: int, now: datetime) -> tuple[int, str | None]:
    created = not path.exists()
    if created:
        try:
            _write_poll_retry(path, now - timedelta(minutes=requested_minutes))
        except OSError:
            return requested_minutes, "retry_marker_unavailable"
    try:
        record = json.loads(path.read_text())
        if record.get("schema_version") != 1 or not isinstance(record.get("started_at"), str):
            raise ValueError
        started_at = timefields.stored(record["started_at"])
        if started_at.tzinfo is None:
            raise ValueError
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return requested_minutes, "invalid_retry_marker"
    if created:
        return requested_minutes, None
    retry_boundary = now - timedelta(days=POLL_RETRY_FALLBACK_DAYS)
    if started_at < retry_boundary:
        return requested_minutes, "outside_auto_window"
    minutes = math.ceil((now - started_at).total_seconds() / 60)
    if minutes <= 0:
        return requested_minutes, "invalid_retry_marker"
    return max(requested_minutes, minutes), None


def base() -> str:
    return os.environ.get("APCA_DATA_URL", "https://data.alpaca.markets").strip().strip('"').rstrip("/")


def call(path: str, params: dict) -> dict:
    """One Alpaca request (retries, Retry-After, breaker: resilient)."""
    resp = resilient.get("alpaca", base() + path, http=http_get, bucket=ratelimit.ALPACA, headers=_headers(),
                         params=params, timeout=30)
    return resp.json()


def text(content: str | None) -> str:
    content = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", content or "", flags=re.S | re.I)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", content)).split())


# ---------------------------------------------------------------------------------------------- news

def news_pages(start: str, end: str, symbols: Iterable[str] | None = None, *, content: bool = True,
               on_cursor: Callable[[str | None], None] | None = None):
    """Every article created in [start, end), oldest first, following page tokens."""
    params = {"start": start, "end": end, "limit": PAGE, "sort": "asc", "include_content": str(content).lower()}
    if symbols:
        params["symbols"] = ",".join(symbols)
    token = None
    seen_tokens = set()
    while True:
        if token is not None:
            if token in seen_tokens:
                raise ValueError("repeated Alpaca news pagination cursor")
            seen_tokens.add(token)
        j = call("/v1beta1/news", {**params, **({"page_token": token} if token else {})})
        if not isinstance(j, dict):
            raise ValueError("invalid Alpaca news response object")
        news = j.get("news")
        if not isinstance(news, list):
            raise ValueError("Alpaca news response has no valid news list")
        for item in news:
            if (not isinstance(item, dict) or "id" not in item or isinstance(item.get("id"), bool)
                    or not isinstance(item.get("id"), (str, int)) or not str(item["id"]).strip()
                    or not isinstance(item.get("created_at"), str) or not item["created_at"].strip()
                    or (item.get("updated_at") is not None and not isinstance(item.get("updated_at"), str))
                    or (item.get("symbols") is not None and
                        (not isinstance(item["symbols"], list) or
                         any(not isinstance(symbol, str) for symbol in item["symbols"])))):
                raise ValueError("invalid Alpaca news article")
        next_token = j.get("next_page_token")
        if next_token is not None and (not isinstance(next_token, str) or not next_token.strip()):
            raise ValueError("invalid Alpaca news pagination cursor")
        if next_token is not None and (next_token == token or next_token in seen_tokens):
            raise ValueError("repeated Alpaca news pagination cursor")
        if on_cursor is not None:
            on_cursor(hashlib.sha256(next_token.encode("utf-8")).hexdigest() if next_token else None)
        yield news
        token = next_token
        if not token:
            return


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    body_versions.ensure_schema(conn)
    temporal_evidence.ensure_schema(conn)
    evidence_fts_projection.ensure_schema(conn)
    ledger.ensure_ledger(conn)
    conn.commit()
    return conn


def _append_body_version(
    conn,
    *,
    document_id: str,
    title: str,
    body: str,
    observed_at: str,
    first_seen_at: str,
    published_at: str | None,
    provider_updated_at: str | None,
    published_raw: str | None,
    url: str | None,
    dedup_url: str | None,
    publisher: str | None,
    symbols: list[str],
    is_backfill: bool,
) -> str:
    """Store a full version and its additive temporal/FTS projections."""
    observed_value = timefields.stored(observed_at)
    first_seen_value = timefields.stored(first_seen_at)
    published_value = timefields.stored_or_none(published_at)
    updated_value = timefields.stored_or_none(provider_updated_at)
    try:
        version_id = body_versions.put_version(
            conn,
            source_id=SOURCE,
            document_id=document_id,
            title=title,
            body=body,
            observed_at=observed_value,
            first_seen_at=first_seen_value,
            published_at=published_value,
            provider_updated_at=updated_value,
        )
    except body_versions.BodyVersionError as exc:
        raise body_versions.BodyVersionError(
            f"{exc} (source_id={SOURCE}, document_id={document_id})"
        ) from exc
    stored_published_at = conn.execute(
        "SELECT published_at FROM body_versions WHERE version_id=?", (version_id,)
    ).fetchone()[0]
    if stored_published_at is None:
        raise ValueError("Alpaca news article has no validated created_at publication time")
    record = EvidenceRecord(
        record_id=document_id,
        provider="alpaca",
        provider_record_id=document_id,
        url=dedup_url,
        body_fingerprint=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        fingerprint_version="sha256-full-body-v1",
        body_fingerprint_confirmed=True,
        title=title,
    )
    dedup_outbox.enqueue_candidate(
        conn,
        record,
        source_id=SOURCE,
        source_version_id=version_id,
        provider_record_id_kind="article",
        corrected_at=stored_published_at,
        time_evidence={
            "kind": "source_field",
            "field": "published_at",
            "version_id": version_id,
        },
    )
    canonical_url = normalize_identity_url(url)
    if canonical_url is None:
        return version_id

    evidence_id = "web:" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:32]
    publisher_value = (publisher or "").strip()
    is_benzinga = publisher_value.casefold() == "benzinga"
    publisher_family = "benzinga.com" if is_benzinga else None
    origin = "publisher:benzinga.com" if is_benzinga else None
    published_value = timefields.external(
        published_raw, source="alpaca", field="created_at"
    )
    stored_version = conn.execute(
        "SELECT observed_at,published_at,provider_updated_at FROM body_versions WHERE version_id=?",
        (version_id,),
    ).fetchone()
    for symbol in symbols:
        try:
            ticker = ticker_identity.normalize_ticker(symbol)
        except (TypeError, ValueError):
            continue
        existing = conn.execute(
            "SELECT 1 FROM temporal_evidence_map WHERE version_id=? AND ticker=? AND kind='body'",
            (version_id, ticker),
        ).fetchone()
        if existing:
            continue
        temporal_evidence.register_version(
            conn,
            version_id=version_id,
            ticker=ticker,
            kind="body",
            evidence_id=evidence_id,
            evidence_role="evidence" if is_benzinga else "lead",
            association_observed_at=timefields.stored(observed_at),
            eligible_at=timefields.stored_or_none(stored_version[1]),
            published_at_source="api",
            published_at_precision=published_value.precision if stored_version[1] else "unknown",
            api_published=published_raw, url=url, canonical_url=canonical_url,
            publisher=publisher_value or None,
            publisher_family=publisher_family,
            provider="alpaca",
            source_class="wire" if is_benzinga else "news",
            origin=origin,
            is_backfill=is_backfill,
            time_flags=list(published_value.flags),
        )
    evidence_fts_projection.project_evidence_text(
        conn,
        [evidence_fts_projection.EvidenceText(
            evidence_id=evidence_id,
            version_id=version_id,
            source_id=SOURCE,
            document_id=document_id,
            provider="alpaca",
            kind="body",
            evidence_class="body_evidence",
            title=title,
            text=body,
            origin=origin,
            provenance="versioned",
            observed_at=stored_version[0],
            published_at=stored_version[1],
            provider_updated_at=stored_version[2],
        )],
    )
    return version_id


def store_news(conn, items: list[dict], seen: str, *, is_backfill: bool = False) -> int:
    """Insert new articles; record every new (id, updated_at); refresh the stored body when the article changed."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_versions'"
    ).fetchone() is None:
        body_versions.ensure_schema(conn)
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='temporal_evidence_map'"
    ).fetchone() is None:
        temporal_evidence.ensure_schema(conn)
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_text_projection'"
    ).fetchone() is None:
        evidence_fts_projection.ensure_schema(conn)
    added = 0
    for x in items:
        body = x.get("content") or ""
        row = (int(x["id"]), timefields.external_stamp(x["created_at"], source="alpaca", field="created_at"),
               timefields.external_stamp(x.get("updated_at"), source="alpaca", field="updated_at"), x.get("headline") or "", x.get("summary"), body,
               x.get("url"), x.get("author"), x.get("source"), json.dumps(sorted(x.get("symbols") or [])), seen)
        prior = conn.execute(
            "SELECT created_at,updated_at,headline,content,first_seen_at,url,source,symbols_json "
            "FROM news WHERE id=?",
            (row[0],),
        ).fetchone()
        # The old current body is still observable when an archive is first
        # upgraded. Record it now; never pretend we observed it at first_seen.
        if prior and (prior[3] or "").strip() and (
            prior[2], prior[3], prior[0], prior[1]
        ) != (row[3], body, row[1], row[2]):
            _append_body_version(
                conn, document_id=str(row[0]), title=prior[2], body=prior[3],
                observed_at=seen, first_seen_at=prior[4], published_at=prior[0],
                provider_updated_at=prior[1], published_raw=prior[0], url=prior[5],
                dedup_url=prior[5],
                publisher=prior[6], symbols=json.loads(prior[7] or "[]"), is_backfill=True,
            )
        if body.strip():
            first_seen_at = prior[4] if prior else seen
            _append_body_version(
                conn, document_id=str(row[0]), title=row[3], body=body,
                observed_at=seen, first_seen_at=first_seen_at, published_at=row[1],
                provider_updated_at=row[2], published_raw=x.get("created_at"), url=row[6],
                dedup_url=prior[5] if prior else row[6],
                publisher=row[8], symbols=list(x.get("symbols") or []), is_backfill=is_backfill,
            )
        cur = conn.execute("INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        added += cur.rowcount
        if not cur.rowcount:  # known article: keep the newest version's text, first_seen_at stays
            conn.execute("UPDATE news SET updated_at = ?, headline = ?, summary = ?, content = ?, symbols_json = ? "
                         "WHERE id = ? AND COALESCE(updated_at, '') < COALESCE(?, '')",
                         (row[2], row[3], row[4], row[5], row[9], row[0], row[2]))
        conn.executemany("INSERT OR IGNORE INTO news_symbols VALUES (?, ?)", [(row[0], s) for s in x.get("symbols") or []])
        if x.get("updated_at"):
            conn.execute("INSERT OR IGNORE INTO news_versions VALUES (?, ?, ?, ?)",
                         (row[0], x["updated_at"], seen, len(text(body))))
    return added


def _timestamp(value: str) -> datetime:
    return timefields.stored(value)


def _coverage_subjects(symbols: list[str] | None) -> list[tuple[str, str]]:
    if symbols:
        return [("ticker", symbol) for symbol in sorted(set(symbols))]
    return [("feed", "alpaca_news")]


def _backfill_subjects(symbols: list[str] | None) -> tuple[str, tuple[str, ...]]:
    """Return one stable new intake key and exact legacy short-list keys to honor."""
    if not symbols:
        return "news:backfill", ("news:backfill",)
    canonical = sorted(set(symbols))
    if len(canonical) <= 3:
        subject = "news:backfill:" + ",".join(canonical)
        legacy = "news:backfill:" + ",".join(symbols)
        return subject, tuple(dict.fromkeys((subject, legacy)))
    encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
    subject = f"news:backfill:symbols:{digest}"
    return subject, (subject,)


def _coverage_attempts(conn, *, source_id: str, logical_task_id: str, start: str, end: str,
                       symbols: list[str] | None,
                       products: set[str] | frozenset[str] = frozenset({"headlines", "full_text"})) -> dict[tuple[str, str, str], coverage.Attempt]:
    """Start selected product partitions for this exact provider request."""
    coverage.ensure_schema(conn)
    # The typed subject/product and requested time bounds carry the semantic scope.
    # These transport options do not narrow the universe after pagination exhausts.
    query = {}
    attempts = {}
    for subject_kind, subject_id in _coverage_subjects(symbols):
        for product in ("headlines", "full_text"):
            if product not in products:
                continue
            key = (product, subject_kind, subject_id)
            attempts[key] = coverage.start_attempt(
                conn, logical_task_id=logical_task_id, attempt_id=uuid4().hex,
                source_id=source_id, product=product, subject_kind=subject_kind,
                subject_id=subject_id, query_spec=query,
                coverage_policy_version=COVERAGE_POLICY_VERSION,
                requested_from=start, requested_until=end,
            )
    return attempts


def _record_coverage(conn, attempts, *, status: str | dict, start: str, end: str,
                     enumerated: bool | None, cursor: str | None,
                     expected: dict[tuple[str, str, str], set[str]] | None,
                     acquired: dict[tuple[str, str, str], set[str]] | None,
                     reason: str | None = None) -> None:
    # Alpaca timestamps are second-precision and the API end is exclusive, so
    # the closed coverage interval ends one representable second before that end.
    coverage_end = _timestamp(end) - timedelta(seconds=1) if enumerated else None
    coverage_begin = _timestamp(start) if enumerated else None
    for key, attempt in attempts.items():
        product = key[0]
        scope_status = status.get(key, "ERROR") if isinstance(status, dict) else status
        fields = dict(
            coverage_from=coverage_begin.isoformat(timespec="microseconds") if coverage_begin else None,
            coverage_until=coverage_end.isoformat(timespec="microseconds") if coverage_end else None,
            enumeration_complete=enumerated,
            cursor=cursor,
            status=scope_status if scope_status in {"OK", "EMPTY", "NOT_FETCHED"} else "ERROR",
            reason=reason if scope_status == "NOT_FETCHED" else None if scope_status in {"OK", "EMPTY"} else "ERROR",
        )
        if product == "full_text":
            if expected is not None and acquired is not None:
                fields.update(expected_targets=sorted(expected.get(key, set())),
                              acquired_targets=sorted(acquired.get(key, set())))
        coverage.record_coverage(conn, coverage_id=uuid4().hex, attempt_id=attempt.attempt_id,
                                  recorded_at=_stamp(_now()), **fields)


def _record_disabled(conn, *, source_id: str, start: str, end: str,
                     symbols: list[str] | None, subject: str, session: str) -> None:
    logical_task_id = uuid4().hex
    attempts = _coverage_attempts(conn, source_id=source_id, logical_task_id=logical_task_id,
                                  start=start, end=end, symbols=symbols)
    _record_coverage(conn, attempts, status="NOT_FETCHED", start=start, end=end,
                     enumerated=None, cursor=None, expected=None, acquired=None, reason="DISABLED")
    ledger.record_attempt(conn, subject=subject, session_date=session, source=SOURCE,
                          status="DISABLED", headlines=0, bodies=0, commit=False)
    conn.commit()


def _record_unrefilled_gap(conn, marker: Path, window_start: datetime) -> bool:
    """A pending retry marker older than the automatic window cannot be refilled by a poll. Record the span nobody
    fetched as NOT_FETCHED/deferred (a backfill's job) so the hole is visible instead of being a marker that blocks
    every later poll."""
    try:
        started_at = timefields.stored(json.loads(marker.read_text())["started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if started_at >= window_start:
        return False
    start, end = _stamp(started_at), _stamp(window_start)
    attempts = _coverage_attempts(conn, source_id="alpaca_news:news:poll", logical_task_id=uuid4().hex,
                                  start=start, end=end, symbols=None)
    _record_coverage(conn, attempts, status="NOT_FETCHED", start=start, end=end,
                     enumerated=None, cursor=None, expected=None, acquired=None, reason="deferred")
    conn.commit()
    return True


def poll(conn, *, minutes: int = 90, log=print) -> dict:
    with resilient.task("alpaca_news:news:poll", request_coverage="complete") as work:
        result = _poll(conn, minutes=minutes, log=log, work=work)
        work.discovery_complete(result["status"] in ("OK", "EMPTY"))
        work.finish(result["status"])
        return result


def _poll(conn, *, minutes: int, log, work) -> dict:
    """The last ``minutes`` of the whole wire (overlapping runs are de-duplicated by id)."""
    end = _now().replace(microsecond=0)
    start = end - timedelta(minutes=minutes)
    start_s, end_s = _stamp(start), _stamp(end)
    subject, session = "news:poll", end.date().isoformat()
    logical_task_id = uuid4().hex
    source_id = "alpaca_news:news:poll"
    key = ("full_text", "feed", "alpaca_news")
    expected = {key: set()}
    acquired = {key: set()}
    cursor = None
    attempts = {}
    try:
        attempts = _coverage_attempts(conn, source_id=source_id, logical_task_id=logical_task_id,
                                      start=start_s, end=end_s, symbols=None)
        n = got = 0
        targets = set()
        completed = False

        def track_cursor(value):
            nonlocal cursor
            cursor = value

        for page in news_pages(start_s, end_s, on_cursor=track_cursor):
            seen = _stamp(_now())
            got += len(page)
            for item in page:
                target = str(item["id"])
                expected[key].add(target)
                work.expect_target(f"alpaca:{item['id']}:{item.get('updated_at') or 'unversioned'}")
                if text(item.get("content")):
                    acquired[key].add(target)
                    targets.add(f"alpaca:{item['id']}:{item.get('updated_at') or 'unversioned'}")
            n += store_news(conn, page, seen)
        completed = True
        status = "OK" if got else "EMPTY"
        _record_coverage(conn, attempts, status=status, start=start_s, end=end_s,
                         enumerated=completed, cursor=cursor, expected=expected, acquired=acquired)
        ledger.record_attempt(conn, subject=subject, session_date=session, source=SOURCE, status=status,
                              headlines=got, bodies=n, commit=False)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        log(f"alpaca news poll: {type(exc).__name__}")
        conn.rollback()
        status, n, got = "ERROR", 0, 0
        try:
            attempts = _coverage_attempts(conn, source_id=source_id, logical_task_id=logical_task_id,
                                          start=start_s, end=end_s, symbols=None)
            _record_coverage(conn, attempts, status=status, start=start_s, end=end_s,
                             enumerated=False, cursor=cursor, expected=expected,
                             acquired={key: set() for key in expected})
            ledger.record_attempt(conn, subject=subject, session_date=session, source=SOURCE, status=status,
                                  headlines=0, bodies=0, commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if status in {"OK", "EMPTY"}:
        for target in targets:
            work.acquire_target(target)
    return {"status": status, "seen": got, "new": n}


def _same_requested_window(stored: str, expected: str) -> bool:
    """Compare coverage bounds after storage normalization, not as raw strings."""
    try:
        return coverage._timestamp(stored, "requested") == coverage._timestamp(expected, "requested")
    except coverage.CoverageError:
        return False


def _headline_coverage_complete(snap: coverage.CoverageSnapshot) -> bool:
    fact = snap.fact
    return bool(fact is not None and fact.enumeration_complete is True and fact.status in {"OK", "EMPTY"} and
                fact.coverage_from and fact.coverage_until)


def _full_text_coverage_complete(snap: coverage.CoverageSnapshot) -> bool:
    fact = snap.fact
    if fact is None or not _headline_coverage_complete(snap):
        return False
    if fact.status == "EMPTY":
        return fact.expected_target_count == 0 and fact.acquired_target_count == 0 and not snap.targets
    if (fact.expected_target_count is None or fact.acquired_target_count is None or
            fact.expected_target_count <= 0 or fact.acquired_target_count != fact.expected_target_count):
        return False
    return (len(snap.targets) == fact.expected_target_count and
            all(target.acquired is True for target in snap.targets))


def _backfill_coverage_complete(conn, day: str, symbols: list[str] | None) -> bool:
    """True when this New York day already has complete headline and body facts for every requested subject."""
    session = date.fromisoformat(day)
    start = datetime(session.year, session.month, session.day, tzinfo=timefields.NY)
    requested_from, requested_until = _stamp(start), _stamp(start + timedelta(days=1))
    needed = {(product, kind, subject)
              for kind, subject in _coverage_subjects(symbols)
              for product in ("headlines", "full_text")}
    coverage.ensure_schema(conn)
    matched: dict[tuple[str, str, str], list[coverage.CoverageSnapshot]] = {key: [] for key in needed}
    for snap in coverage.latest_coverage(conn):
        attempt = snap.attempt
        key = (attempt.product, attempt.subject_kind, attempt.subject_id)
        if (attempt.source_id != "alpaca_news:backfill" or key not in matched or
                not _same_requested_window(attempt.requested_from, requested_from) or
                not _same_requested_window(attempt.requested_until, requested_until)):
            continue
        matched[key].append(snap)
    if any(not snaps for snaps in matched.values()):
        return False
    for key, snaps in matched.items():
        complete = _full_text_coverage_complete if key[0] == "full_text" else _headline_coverage_complete
        if any(not complete(snap) for snap in snaps):
            return False
    return True


def backfill(conn, days: list[str], symbols: list[str] | None = None, *,
             products: set[str] | frozenset[str] = frozenset({"headlines", "full_text"}), log=print) -> dict:
    """Whole New York days (00:00-24:00 ET as UTC bounds).

    Skip a day only when headline and full-text coverage for every requested
    subject is already complete. An intake-log OK or EMPTY row is not coverage.
    """
    ny = timefields.NY
    counts = {"OK": 0, "EMPTY": 0, "ERROR": 0, "skipped": 0}
    requested_symbols = sorted(set(symbols)) if symbols else None
    selected_products = frozenset(products)
    if not selected_products or not selected_products <= {"headlines", "full_text"}:
        raise ValueError("products must be a non-empty subset of headlines and full_text")
    subject, _ = _backfill_subjects(symbols)
    for d in days:
        if _backfill_coverage_complete(conn, d, requested_symbols):
            counts["skipped"] += 1
            continue
        with resilient.task("alpaca_news:backfill", request_coverage="complete") as work:
            day = date.fromisoformat(d)
            start = datetime(day.year, day.month, day.day, tzinfo=ny)
            start_s, end_s = _stamp(start), _stamp(start + timedelta(days=1))
            logical_task_id = uuid4().hex
            source_id = "alpaca_news:backfill"
            expected = {}
            acquired = {}
            cursor = None
            attempts = {}
            try:
                attempts = _coverage_attempts(conn, source_id=source_id, logical_task_id=logical_task_id,
                                              start=start_s, end=end_s, symbols=requested_symbols,
                                              products=selected_products)
                ticker_keys = _coverage_subjects(requested_symbols)
                observed = {(product, kind, identity): set()
                            for kind, identity in ticker_keys for product in ("headlines", "full_text")}
                expected = {("full_text", kind, identity): set() for kind, identity in ticker_keys}
                acquired = {key: set() for key in expected}
                got = new = 0
                targets = set()

                def track_cursor(value):
                    nonlocal cursor
                    cursor = value

                for page in news_pages(start_s, end_s, requested_symbols, on_cursor=track_cursor):
                    seen = _stamp(_now())
                    got += len(page)
                    for item in page:
                        item_symbols = set(item.get("symbols") or [])
                        matching = ticker_keys if requested_symbols is None else [
                            pair for pair in ticker_keys if pair[1] in item_symbols
                        ]
                        target = str(item["id"])
                        for kind, identity in matching:
                            observed[("headlines", kind, identity)].add(target)
                            observed[("full_text", kind, identity)].add(target)
                            target_key = ("full_text", kind, identity)
                            expected[target_key].add(target)
                            if text(item.get("content")):
                                acquired[target_key].add(target)
                        version_target = f"alpaca:{item['id']}:{item.get('updated_at') or 'unversioned'}"
                        work.expect_target(version_target)
                        if text(item.get("content")):
                            targets.add(version_target)
                    new += store_news(conn, page, seen, is_backfill=True)
                status = "OK" if got else "EMPTY"
                scope_status = {key: ("OK" if observed[key] else "EMPTY") for key in attempts}
                # A filtered full-text retry that enumerates zero targets cannot
                # establish that previously expected body targets disappeared.
                retryable_empty_body = False
                if selected_products != frozenset({"headlines", "full_text"}):
                    for key in attempts:
                        if key[0] == "full_text" and not observed[key]:
                            scope_status[key] = "ERROR"
                            retryable_empty_body = True
                if retryable_empty_body:
                    status = "ERROR"
                _record_coverage(conn, attempts, status=scope_status, start=start_s, end=end_s,
                                 enumerated=not retryable_empty_body, cursor=cursor,
                                 expected=expected, acquired=acquired)
                ledger.record_attempt(conn, subject=subject, session_date=d, source=SOURCE, status=status,
                                      headlines=got, bodies=new, commit=False)
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                log(f"alpaca backfill {d}: {type(exc).__name__}")
                conn.rollback()
                status, got, new = "ERROR", 0, 0
                try:
                    attempts = _coverage_attempts(conn, source_id=source_id, logical_task_id=logical_task_id,
                                                  start=start_s, end=end_s, symbols=requested_symbols,
                                                  products=selected_products)
                    _record_coverage(conn, attempts, status=status, start=start_s, end=end_s,
                                     enumerated=False, cursor=cursor, expected=expected,
                                     acquired={key: set() for key in expected})
                    ledger.record_attempt(conn, subject=subject, session_date=d, source=SOURCE, status=status,
                                          headlines=0, bodies=0, commit=False)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            if status in {"OK", "EMPTY"}:
                for target in targets:
                    work.acquire_target(target)
            counts[status] += 1
            work.discovery_complete(status in ("OK", "EMPTY"))
            work.finish(status)
    return counts


# ---------------------------------------------------------------------------------------------- bars

def minute_bars(symbols: list[str], day: str) -> dict[str, list[tuple]]:
    """{symbol: [(ts_utc, o, h, l, c, v), ...]} for one New York day incl. pre/post market (04:00-20:00 ET)."""
    ny = timefields.NY
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, 4, tzinfo=ny)
    params = {"symbols": ",".join(symbols), "timeframe": "1Min", "start": _stamp(start),
              "end": _stamp(start + timedelta(hours=16)), "feed": "sip", "adjustment": "raw", "limit": 10000}
    out: dict[str, list[tuple]] = {}
    token = None
    while True:
        j = call("/v2/stocks/bars", {**params, **({"page_token": token} if token else {})})
        for sym, bars in (j.get("bars") or {}).items():
            out.setdefault(sym, []).extend((b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars)
        token = j.get("next_page_token")
        if not token:
            return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("news-poll")
    p.add_argument("--db", required=True)
    p.add_argument("--minutes", type=int, default=90)
    b = sub.add_parser("news-backfill")
    b.add_argument("--db", required=True)
    b.add_argument("--dates", required=True, help="file with one YYYY-MM-DD per line")
    b.add_argument("--symbols-file")
    args = ap.parse_args(argv)
    if args.cmd == "news-backfill":
        import sys

        from catalyst_data.ingestion.input_manifest import require_backfill
        inputs = [args.dates]
        if args.symbols_file:
            inputs.append(args.symbols_file)
        if Path(args.db).exists():
            inputs.append(args.db)
        require_backfill(__file__, list(sys.argv[1:] if argv is None else argv), inputs=inputs,
                         environment=["APCA_DATA_URL"])
    from catalyst_data.ingestion.storage import store
    retry_path: Path | None = None
    poll_minutes = args.minutes if args.cmd == "news-poll" else None
    retry_reason: str | None = None
    retry_now = None
    gap_recorded = False
    if args.cmd == "news-poll" and configured():
        retry_path = _poll_retry_path(args.db)
        retry_now = _now().replace(microsecond=0)
        poll_minutes, retry_reason = _poll_retry_minutes(retry_path, args.minutes, retry_now)
    try:
        lock_options = {"wait_seconds": POLL_LOCK_WAIT_SECONDS} if args.cmd == "news-poll" else {}
        with store.refresh_lock(args.db, **lock_options):
            conn = connect(args.db)
            try:
                if args.cmd == "news-backfill":
                    days = sorted({line.strip() for line in Path(args.dates).read_text().splitlines() if line.strip()})
                    symbols = [line.strip() for line in Path(args.symbols_file).read_text().splitlines() if line.strip()] \
                        if args.symbols_file else None
                if not configured():
                    if args.cmd == "news-poll":
                        end = _now().replace(microsecond=0)
                        _record_disabled(conn, source_id="alpaca_news:news:poll",
                                         start=_stamp(end - timedelta(minutes=args.minutes)), end=_stamp(end),
                                         symbols=None, subject="news:poll", session=end.date().isoformat())
                    else:
                        for day in days:
                            local_day = date.fromisoformat(day)
                            start = datetime(local_day.year, local_day.month, local_day.day, tzinfo=timefields.NY)
                            subject, _ = _backfill_subjects(symbols)
                            _record_disabled(conn, source_id="alpaca_news:backfill",
                                             start=_stamp(start), end=_stamp(start + timedelta(days=1)),
                                             symbols=symbols, subject=subject, session=day)
                    print(json.dumps({"cmd": args.cmd, "status": "DISABLED"}))
                    return 0
                if args.cmd == "news-poll":
                    out = poll(conn, minutes=poll_minutes)
                    if (retry_reason == "outside_auto_window" and out.get("status") in {"OK", "EMPTY"}
                            and retry_path is not None):
                        # The span the poll did not cover ends where its window starts. Taken from the time the poll
                        # was planned, not after it: the time the poll itself takes must not move the bound.
                        gap_recorded = _record_unrefilled_gap(conn, retry_path, retry_now - timedelta(minutes=poll_minutes))
                else:
                    out = backfill(conn, days, symbols)
            finally:
                conn.close()
    except store.RefreshBusy:
        if args.cmd != "news-poll":
            raise
        marker_exists = _poll_retry_marker_exists(retry_path)
        print(json.dumps({"cmd": args.cmd, "status": "BUSY", "retry_pending": marker_exists,
                          **({"retry_reason": retry_reason} if retry_reason else {})}))
        return 75
    if args.cmd == "news-poll" and retry_path is not None:
        marker_exists = _poll_retry_marker_exists(retry_path)
        if out.get("status") in {"OK", "EMPTY"} and marker_exists and retry_reason is None:
            retry_path.unlink(missing_ok=True)
        elif (out.get("status") in {"OK", "EMPTY"} and marker_exists and retry_reason == "outside_auto_window"
              and gap_recorded):
            # Too old for a poll to refill: the gap is on record, so the marker has done its job. Without the record
            # the marker is the only trace of the hole and stays (exit 2 below).
            retry_path.unlink(missing_ok=True)
            print(json.dumps({"cmd": args.cmd, **out, "retry_pending": False, "retry_reason": retry_reason,
                              "gap_recorded": gap_recorded}))
            return 0
        elif out.get("status") in {"OK", "EMPTY"} and marker_exists and retry_reason is not None:
            print(json.dumps({"cmd": args.cmd, "status": "RETRY_PENDING",
                              "poll_status": out.get("status"), "retry_pending": True,
                              "retry_reason": retry_reason}))
            return 2
        elif out.get("status") in {"OK", "EMPTY"} and retry_reason is not None:
            print(json.dumps({"cmd": args.cmd, **out, "retry_pending": False,
                              "retry_reason": retry_reason}))
            return 0
        elif out.get("status") == "ERROR":
            print(json.dumps({"cmd": args.cmd, **out, "retry_pending": marker_exists,
                              **({"retry_reason": retry_reason} if retry_reason else {})}))
            return 1
    print(json.dumps({"cmd": args.cmd, **out}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
