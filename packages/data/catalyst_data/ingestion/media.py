"""Article image selection and resumable image capture for M9 bodies."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

from catalyst_data.ingestion.collectors import fetch
from catalyst_data.ingestion.storage import store
from catalyst_data.storage.connect import connect_readonly
from catalyst_data.storage.runtime_paths import runtime_store_path

HEADLINE_MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS headline_media (
    article_key TEXT PRIMARY KEY,
    resolved_url TEXT,
    image_url TEXT,
    image_source TEXT CHECK (image_source IN ('og','twitter') OR image_source IS NULL),
    status TEXT NOT NULL CHECK (status IN ('ok','no_image','unfetched')),
    reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT,
    next_attempt_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_headline_media_image ON headline_media(status, image_url);
"""
MAX_HEADLINE_ATTEMPTS = 3
RETRY_DELAYS = (300, 1800, 7200)


def cache_path() -> Path:
    return runtime_store_path("media_db")


def open_cache(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(HEADLINE_MEDIA_SCHEMA)
    conn.commit()
    return conn


def image_identity(url: str) -> str:
    """Normalize Yahoo's resize wrapper to the provider image used inside it."""
    parsed = urlparse(url.strip())
    if parsed.hostname and parsed.hostname.lower().endswith("yimg.com") and "mysterio" in parsed.path.lower():
        path = parsed.path
        for _ in range(3):
            decoded = unquote(path)
            match = re.search(r"https?://[^\s]+", decoded, flags=re.I)
            if match:
                inner = match.group(0).rstrip("/\"')>")
                host = (urlparse(inner).hostname or "").lower()
                if "media.zenfs.com/" in inner or "yahoo" not in host:
                    return inner
            if decoded == path:
                break
            path = decoded
    return url.strip()


def is_placeholder(url: str, article_counts: Counter | None = None) -> bool:
    """Reject known generic URLs and image URLs shared by over 20 articles."""
    identity = image_identity(url)
    return fetch.is_placeholder_image(identity) or bool(article_counts and article_counts[identity] > 20)


def headline_image(source_row: dict) -> tuple[str, str] | None:
    """Select a usable image from a raw Polygon or Finnhub article row."""
    provider = str(source_row.get("provider") or source_row.get("source") or "").lower()
    if not provider:
        provider = "polygon" if "image_url" in source_row else "finnhub" if "image" in source_row else ""
    if provider == "polygon":
        url, source = source_row.get("image_url"), "polygon"
    elif provider == "finnhub":
        url, source = source_row.get("image"), "finnhub"
    else:
        return None
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if fetch.is_placeholder_image(url):
        return None
    return url, source


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _headline_key(url: str) -> str:
    from catalyst_data.ingestion.session_news import canonical_url

    return canonical_url(url)


def _archive_image_pairs(path: str | Path | None) -> set[tuple[str, str]]:
    if not path or not Path(path).is_file():
        return set()
    conn = connect_readonly(Path(path).resolve().as_uri()[7:], immutable=False, timeout_ms=10_000)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        seen: set[tuple[str, str]] = set()
        for table, image_fields in (("finnhub_news", ("image", "image_url")),
                                    ("polygon_feed", ("image_url", "image"))):
            if table not in tables:
                continue
            for url, raw_json in conn.execute(f"SELECT url, raw_json FROM {table}"):
                try:
                    raw = json.loads(raw_json or "{}")
                except (TypeError, ValueError):
                    continue
                image = next((raw.get(name) for name in image_fields if raw.get(name)), None)
                if not isinstance(image, str) or not url:
                    continue
                article = _headline_key(url)
                identity = image_identity(image)
                if article and identity and (article, identity) not in seen:
                    seen.add((article, identity))
    finally:
        conn.close()
    return seen


def image_use_pairs(cache_db: str | Path | None = None,
                    archive_db: str | Path | None = None) -> set[tuple[str, str]]:
    """Return distinct (article, normalized image) pairs across feeds and cache."""
    pairs = _archive_image_pairs(archive_db or os.environ.get("CATALYST_HEADLINE_ARCHIVE_DB"))
    path = Path(cache_db) if cache_db is not None else cache_path()
    if not path.is_file():
        return pairs
    conn = connect_readonly(path.resolve().as_uri()[7:], immutable=False, timeout_ms=10_000)
    try:
        try:
            rows = conn.execute(
                "SELECT article_key,image_url FROM headline_media WHERE status='ok' AND image_url IS NOT NULL"
            )
        except sqlite3.OperationalError:
            return pairs
        seen: set[tuple[str, str]] = set()
        for article, image in rows:
            identity = image_identity(image)
            pair = (_headline_key(article), identity)
            if pair[0] and pair[1] and pair not in seen:
                seen.add(pair)
                pairs.add(pair)
    finally:
        conn.close()
    return pairs


def image_use_counts(cache_db: str | Path | None = None,
                     archive_db: str | Path | None = None) -> Counter:
    """Count distinct image identities across archived feeds and headline cache."""
    return Counter(image for _, image in image_use_pairs(cache_db, archive_db))


def fill_headlines(items: Iterable[dict], cache_db: str | Path | None = None, *,
                   fetcher=None, limit: int | None = None, now: datetime | None = None,
                   force: bool = False, archive_db: str | Path | None = None,
                   shared_images: frozenset[str] | None = None) -> dict[str, int]:
    """Fill uncached headline-only articles; transient outcomes remain resumable.

    With ``shared_images`` (the daily shared-image set, keyed like session_news) an image is generic
    when its key is in that set, and the archive is not scanned; the API passes it."""
    fetcher = fetcher or fetch.Fetcher()
    now = now or datetime.now(timezone.utc)
    now_text = _iso(now)
    path = Path(cache_db) if cache_db is not None else cache_path()
    conn = open_cache(path)
    result = {"checked": 0, "stored": 0, "no_image": 0, "deferred": 0, "skipped": 0}
    candidates = []
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url or item.get("has_body") or (urlparse(url).hostname or "").lower().endswith("sec.gov"):
            result["skipped"] += 1
            continue
        key = _headline_key(url)
        if key:
            candidates.append((key, url))
    if shared_images is None:
        used_pairs = image_use_pairs(path, archive_db)
        counters = Counter(image for _, image in used_pairs)
    else:
        used_pairs, counters = set(), Counter()
    try:
        for key, url in dict(candidates).items():
            row = conn.execute(
                "SELECT status, attempts, next_attempt_at FROM headline_media WHERE article_key=?", (key,)
            ).fetchone()
            if row and row["status"] in ("ok", "no_image"):
                result["skipped"] += 1
                continue
            attempts = int(row["attempts"]) if row else 0
            if attempts >= MAX_HEADLINE_ATTEMPTS:
                result["skipped"] += 1
                continue
            if row and row["next_attempt_at"] and row["next_attempt_at"] > now_text and not force:
                result["skipped"] += 1
                continue
            if limit is not None and result["checked"] >= limit:
                break
            result["checked"] += 1
            try:
                detailed = getattr(fetcher, "fetch_page_detailed", None)
                page = detailed(url) if detailed else fetcher.fetch_page(url)
                if page is None:
                    raise fetch.PermanentPageError("no_page")
                resolved_url, html = page
                image = fetch.page_image_with_source(html, resolved_url)
                if not image:
                    raise fetch.PermanentPageError("no_image")
                image_url, source = image
                identity = image_identity(image_url)
                pair = (key, identity)
                if is_placeholder(image_url) or (
                    _headline_key(identity) in shared_images if shared_images is not None
                    else pair not in used_pairs and counters[identity] >= 20
                ):
                    raise fetch.PermanentPageError("generic_image")
                conn.execute(
                    "INSERT OR REPLACE INTO headline_media "
                    "(article_key,resolved_url,image_url,image_source,status,reason,attempts,checked_at,next_attempt_at) "
                    "VALUES (?,?,?,?, 'ok', NULL, ?, ?, NULL)",
                    (key, resolved_url, image_url, source, attempts + 1, now_text),
                )
                if pair not in used_pairs:
                    used_pairs.add(pair)
                    counters[identity] += 1
                result["stored"] += 1
            except fetch.PermanentPageError as exc:
                reason = str(exc)[:80]
                conn.execute(
                    "INSERT OR REPLACE INTO headline_media "
                    "(article_key,resolved_url,image_url,image_source,status,reason,attempts,checked_at,next_attempt_at) "
                    "VALUES (?,NULL,NULL,NULL,'no_image',?,?,?,NULL)",
                    (key, reason, attempts + 1, now_text),
                )
                result["no_image"] += 1
            except (fetch.HostUnavailable, fetch.TransientPageError) as exc:
                next_attempt = now + timedelta(seconds=RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)])
                conn.execute(
                    "INSERT OR REPLACE INTO headline_media "
                    "(article_key,resolved_url,image_url,image_source,status,reason,attempts,checked_at,next_attempt_at) "
                    "VALUES (?,NULL,NULL,NULL,'unfetched',?,?,?,?)",
                    (key, type(exc).__name__, attempts + 1, now_text, _iso(next_attempt)),
                )
                result["deferred"] += 1
            except Exception as exc:
                # Network adapters can raise requests exceptions not wrapped by Fetcher.
                import requests

                if isinstance(exc, requests.RequestException):
                    next_attempt = now + timedelta(seconds=RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)])
                    conn.execute(
                        "INSERT OR REPLACE INTO headline_media "
                        "(article_key,resolved_url,image_url,image_source,status,reason,attempts,checked_at,next_attempt_at) "
                        "VALUES (?,NULL,NULL,NULL,'unfetched',?,?,?,?)",
                        (key, type(exc).__name__, attempts + 1, now_text, _iso(next_attempt)),
                    )
                    result["deferred"] += 1
                else:
                    raise
            if result["checked"] % 25 == 0:
                conn.commit()
        conn.commit()
    finally:
        conn.close()
    return result


def headlines(ticker: str, *, date: str | None = None, since: str | None = None,
              limit: int | None = None, bodies_db: str | Path | None = None,
              archive_db: str | Path | None = None, cache_db: str | Path | None = None,
              fetcher=None, shared_images_path: str | Path | None = None) -> dict[str, int]:
    """Fill headline images for one session or every trading session since a date."""
    from catalyst_data import trading_calendar
    from catalyst_data.ingestion import session_news
    from catalyst_data.ingestion.collectors import headline_archive, market

    if bool(date) == bool(since):
        raise ValueError("provide exactly one of date or since")
    bodies = (Path(bodies_db) if bodies_db else
              runtime_store_path("bodies_db", headline_archive.REPO))
    archive = (Path(archive_db) if archive_db else
               runtime_store_path("archive_db", headline_archive.REPO))
    end = date or market.last_closed_session()
    sessions = [date] if date else trading_calendar.calendar_trading_days(since, end)
    result = {"sessions": len(sessions), "checked": 0, "stored": 0, "no_image": 0, "deferred": 0, "skipped": 0}
    fetcher = fetcher or fetch.Fetcher()
    shared = (session_news._shared_images_from_file(shared_images_path)
              if shared_images_path is not None else None)
    remaining = limit
    for session_date in sessions:
        cursor = None
        items = []
        while True:
            page = session_news.session_news(ticker.upper(), session_date, bodies_db=bodies,
                                             archive_db=archive, cursor=cursor, limit=100,
                                             media_db=cache_db, shared_images_path=shared_images_path)
            items.extend(item for item in page["items"] if not item["has_body"] and not item.get("image_url"))
            cursor = page["next_cursor"]
            if not cursor:
                break
        fill = fill_headlines(items, cache_db, fetcher=fetcher, limit=remaining, archive_db=archive,
                              shared_images=shared)
        for key in ("checked", "stored", "no_image", "deferred", "skipped"):
            result[key] += fill[key]
        if remaining is not None:
            remaining -= fill["checked"]
            if remaining <= 0:
                break
    return result


def backfill(conn: sqlite3.Connection, fetcher=None, limit: int | None = None,
             asset_ids: Iterable[str] | None = None) -> dict[str, int]:
    """Fetch each unprocessed stored body URL once; persist image or a miss."""
    fetcher = fetcher or fetch.Fetcher()
    query = (
        "SELECT b.asset_id, b.canonical_url FROM web_body_chunks b "
        "WHERE b.ordinal=1 AND b.canonical_url NOT LIKE '%sec.gov/%' AND NOT EXISTS (SELECT 1 FROM web_asset_media m WHERE m.asset_id=b.asset_id) "
        "AND NOT EXISTS (SELECT 1 FROM web_asset_media_misses x WHERE x.asset_id=b.asset_id) "
    )
    args: list[object] = []
    if asset_ids is not None:
        asset_ids = list(dict.fromkeys(asset_ids))
        if not asset_ids:
            return {"checked": 0, "stored": 0, "missed": 0, "deferred": 0}
        query += f" AND b.asset_id IN ({','.join('?' for _ in asset_ids)})"
        args.extend(asset_ids)
    query += " ORDER BY b.asset_id"
    sql = query + (" LIMIT ?" if limit is not None else "")
    if limit is not None:
        args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    result = {"checked": 0, "stored": 0, "missed": 0, "deferred": 0}
    for i, (asset_id, canonical_url) in enumerate(rows, 1):
        result["checked"] += 1
        if i % 50 == 0:
            conn.commit()  # keep progress if the run is interrupted
        reason = "no_page"
        try:
            page = fetcher.fetch_page(canonical_url)
            if page is None:
                reason = "no_page"
            else:
                final_url, html = page
                image = fetch.page_image_with_source(html, final_url)
                if image:
                    conn.execute(
                        "INSERT OR REPLACE INTO web_asset_media "
                        "(asset_id, canonical_url, image_url, image_source, checked_at) VALUES (?,?,?,?,?)",
                        (asset_id, final_url, image[0], image[1], _now()),
                    )
                    conn.execute("DELETE FROM web_asset_media_misses WHERE asset_id=?", (asset_id,))
                    result["stored"] += 1
                    continue
                reason = "no_image"
        except fetch.HostUnavailable:
            # Host or relay down for now: not a miss, try again on the next run.
            result["deferred"] += 1
            continue
        except Exception as exc:
            reason = f"fetch_error:{type(exc).__name__}"
        conn.execute(
            "INSERT OR REPLACE INTO web_asset_media_misses (asset_id, checked_at, reason) VALUES (?,?,?)",
            (asset_id, _now(), reason),
        )
        result["missed"] += 1
    conn.commit()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backfill_parser = subparsers.add_parser("backfill", help="capture images for stored article bodies")
    backfill_parser.add_argument("--bodies-db", required=True, type=Path)
    backfill_parser.add_argument("--limit", type=int)
    headlines_parser = subparsers.add_parser("headlines", help="capture images for headline-only news items")
    headlines_parser.add_argument("--ticker", required=True)
    dates = headlines_parser.add_mutually_exclusive_group(required=True)
    dates.add_argument("--date")
    dates.add_argument("--since")
    headlines_parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.command == "backfill":
        conn = store.open_writer(args.bodies_db)
        try:
            print(backfill(conn, limit=args.limit))
        finally:
            conn.close()
    else:
        print(json.dumps(headlines(args.ticker, date=args.date, since=args.since, limit=args.limit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
