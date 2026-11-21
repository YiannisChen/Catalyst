"""Read-only session news aggregation for B2."""
from __future__ import annotations

import base64
import html
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from catalyst_data.canonical.temporal import build_temporal_identity, utc_iso_z
from catalyst_data.ingestion import categorize_llm, media
from catalyst_data.ingestion.display_text import normalize_display_text
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval import web_bodies
from catalyst_data.retrieval.web_bodies import (
    names_subject,
    subject_matcher,
    tickers_json_list,
)
from catalyst_data.storage.runtime_paths import runtime_store_path

_TRACKING = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "cmpid", "ref", "src", "source", "feed", "from")


def canonical_url(url: str) -> str:
    """Normalize the article identity: lowercase host, no fragment, no trailing slash, and no
    tracking parameters. Other query parameters stay: some feeds identify articles only by them
    (finnhub.io/api/news?id=...)."""
    from urllib.parse import parse_qsl, urlencode

    parts = urlsplit(str(url or "").strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not k.lower().startswith(_TRACKING)))
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def _read_only(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    from catalyst_data.storage.connect import connect_readonly
    conn = connect_readonly(resolved, timeout_ms=10_000, immutable=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _utc(value: str | None) -> str | None:
    parsed = timefields.stored_or_none(value)
    return utc_iso_z(parsed) if parsed is not None else None


def _query_body_articles(bodies: sqlite3.Connection, tables: set[str], *, ticker: str, start: str,
                        cutoff: str, matcher: Any, add: Any) -> None:
    if not {"web_body_chunks", "web_headlines"} <= tables:
        return
    media_table = "web_asset_media" in tables
    media_join = "LEFT JOIN web_asset_media m ON m.asset_id=b.asset_id" if media_table else ""
    media_cols = ", m.image_url, m.image_source" if media_table else ", NULL AS image_url, NULL AS image_source"
    query = ("SELECT DISTINCT b.asset_id,b.chunk_id,b.text,b.canonical_url,b.title,b.publisher,"
             "b.eligible_at" + media_cols + " FROM web_body_chunks b "
             "JOIN web_headlines h ON h.asset_id=b.asset_id " + media_join +
             " WHERE b.ordinal=1 AND h.ticker=? AND b.eligible_at>? AND b.eligible_at<=?")
    for row in bodies.execute(query, (ticker, start, cutoff)):
        title, lead = row["title"] or "", row["text"] or ""
        if not names_subject(ticker, title, lead, matcher):
            continue
        stored_image = ((row["image_url"], row["image_source"])
                        if row["image_url"] and row["image_source"] in ("og", "twitter") else None)
        add(_candidate(source_kind="body", item_id=row["asset_id"], asset_id=row["asset_id"],
                       chunk_id=row["chunk_id"], url=row["canonical_url"], title=title,
                       publisher=row["publisher"] or _publisher(row["canonical_url"]),
                       published_at=row["eligible_at"], summary=None, body_text=lead,
                       stored_image=stored_image))


def _query_finnhub_articles(archive: sqlite3.Connection, tables: set[str], *, ticker: str, start: str,
                            cutoff: str, matcher: Any, add: Any) -> None:
    if "finnhub_news" not in tables:
        return
    for row in archive.execute(
            "SELECT symbol,news_id,published_utc,headline,url,source,summary,raw_json "
            "FROM finnhub_news WHERE symbol=? AND published_utc>? AND published_utc<=?",
            (ticker, timefields.finnhub_read_from(start), cutoff)):
        published = timefields.unskew(row["published_utc"], source="finnhub", publisher=row["source"])
        if not (start < published <= cutoff):
            continue
        raw = _source_json(row["raw_json"])
        title = row["headline"] or ""
        summary = row["summary"] or raw.get("summary")
        mentions = names_subject(ticker, title, str(summary or ""), matcher)
        add(_candidate(source_kind="finnhub", mentions_subject=mentions,
                       item_id=f"finnhub:{ticker}:{row['news_id']}", url=row["url"], title=title,
                       publisher=_publisher(row["url"], row["source"], raw), published_at=published,
                       summary=summary, raw=raw, source="finnhub"))


def _query_polygon_articles(archive: sqlite3.Connection, tables: set[str], *, ticker: str,
                            session_date: str, start: str, cutoff: str, matcher: Any, add: Any) -> None:
    if "polygon_feed" not in tables:
        return
    for row in archive.execute(
            "SELECT * FROM polygon_feed WHERE session_date BETWEEN date(?, '-4 days') AND date(?, '+1 day') "
            "AND instr(tickers_json, '\"' || ? || '\"') > 0",
            (session_date, session_date, ticker)):
        if ticker not in tickers_json_list(row["tickers_json"]):
            continue
        title = row["title"] or ""
        raw = _source_json(row["raw_json"])
        summary = raw.get("description") or raw.get("summary")
        mentions = names_subject(ticker, title, str(summary or ""), matcher)
        published = _utc(row["published_utc"])
        if not published or not (start < published <= cutoff):
            continue
        add(_candidate(source_kind="polygon", mentions_subject=mentions, item_id=f"polygon:{row['news_id']}",
                       url=row["url"], title=title, publisher=_publisher(row["url"], raw=raw),
                       published_at=published, summary=summary, raw=raw, source="polygon"))


def _short(value: str | None) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text if len(text) <= 300 else text[:297].rstrip() + "..."


def _source_json(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _publisher(url: str, source: str | None = None, raw: dict | None = None) -> str:
    if raw and isinstance(raw.get("publisher"), dict):
        name = raw["publisher"].get("name")
        if name:
            return str(name)
    if source and source.strip():
        return source.strip()
    return web_bodies.publisher_for_url(url) or ""


def _article_key(url: str) -> str:
    return canonical_url(url)


def _image_key(url: str) -> str:
    return canonical_url(media.image_identity(url))


@lru_cache(maxsize=8)
def _shared_image_urls(db_path: str, db_size: int, db_mtime_ns: int,
                       media_path: str, media_size: int, media_mtime_ns: int) -> frozenset[str]:
    """Return image URLs shared by more than 20 distinct feed/cache articles."""
    del db_size, db_mtime_ns, media_size, media_mtime_ns  # included in cache key for invalidation
    counts: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    conn = _read_only(db_path)
    try:
        tables = _tables(conn)
        if "finnhub_news" in tables:
            for row in conn.execute("SELECT url, raw_json FROM finnhub_news"):
                raw = _source_json(row["raw_json"])
                image_url = raw.get("image") or raw.get("image_url")
                if isinstance(image_url, str) and image_url.strip():
                    pair = (_image_key(image_url), _article_key(row["url"] or ""))
                    if pair[0] and pair[1] and pair not in seen:
                        seen.add(pair)
                        counts[pair[0]] += 1
        if "polygon_feed" in tables:
            for row in conn.execute("SELECT url, raw_json FROM polygon_feed"):
                raw = _source_json(row["raw_json"])
                image_url = raw.get("image_url") or raw.get("image")
                if isinstance(image_url, str) and image_url.strip():
                    pair = (_image_key(image_url), _article_key(row["url"] or ""))
                    if pair[0] and pair[1] and pair not in seen:
                        seen.add(pair)
                        counts[pair[0]] += 1
    finally:
        conn.close()
    if media_path and Path(media_path).is_file():
        conn = _read_only(media_path)
        try:
            if "headline_media" in _tables(conn):
                for row in conn.execute(
                    "SELECT article_key,image_url FROM headline_media WHERE status='ok' AND image_url IS NOT NULL"
                ):
                    pair = (_image_key(row["image_url"]), _article_key(row["article_key"]))
                    if pair[0] and pair[1] and pair not in seen:
                        seen.add(pair)
                        counts[pair[0]] += 1
        finally:
            conn.close()
    return frozenset(url for url, count in counts.items() if count > 20)


_FROZEN_SHARED: dict[tuple[str, str], frozenset[str]] | None = None


@contextmanager
def frozen_shared_images():
    """Compute the shared-image set once per database pair while the block runs.

    The set is rebuilt from ~500k archive rows whenever either file changes (about a minute), and collectors and the
    headline-media fill themselves change those files all day. A batch job that asks for many tickers in a row
    (daily categorisation and headline images) would pay that minute per ticker. It only decides which images are
    shown as generic placeholders, never which articles exist, so a set that is a few minutes old is fine there."""
    global _FROZEN_SHARED
    outer = _FROZEN_SHARED
    _FROZEN_SHARED = {} if outer is None else outer
    try:
        yield
    finally:
        _FROZEN_SHARED = outer


def _shared_for(path: str | Path, media_db: str | Path | None = None) -> frozenset[str]:
    if _FROZEN_SHARED is not None:
        key = (str(path), str(media_db))
        if key not in _FROZEN_SHARED:
            _FROZEN_SHARED[key] = _shared_for_current(path, media_db)
        return _FROZEN_SHARED[key]
    return _shared_for_current(path, media_db)


def _shared_for_current(path: str | Path, media_db: str | Path | None = None) -> frozenset[str]:
    resolved = Path(path).expanduser().resolve()
    stat = resolved.stat()
    media_path = Path(media_db) if media_db is not None else media.cache_path()
    if media_path.is_file():
        media_path = media_path.expanduser().resolve()
        media_stat = media_path.stat()
        wal_path = Path(str(media_path) + "-wal")
        wal_stat = wal_path.stat() if wal_path.exists() else None
        media_size = media_stat.st_size + (wal_stat.st_size if wal_stat else 0)
        media_mtime = max(media_stat.st_mtime_ns, wal_stat.st_mtime_ns if wal_stat else 0)
    else:
        media_path, media_size, media_mtime = Path(""), 0, 0
    signature = (str(resolved), stat.st_size, stat.st_mtime_ns,
                 str(media_path) if media_size else "", media_size, media_mtime)
    return _shared_image_urls(*signature)


def write_shared_images_file(
    archive_db: str | Path, media_db: str | Path | None, out_path: str | Path
) -> dict[str, Any]:
    urls = sorted(_shared_for_current(archive_db, media_db))
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "shared_images:v1",
               "generated_at": utc_iso_z(datetime.now(timezone.utc)),
               "count": len(urls), "urls": urls}
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return payload


@lru_cache(maxsize=8)
def _read_shared_images(path: str, mtime_ns: int, size: int) -> frozenset[str]:
    del mtime_ns, size
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return frozenset()
    urls = payload.get("urls") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "generated_at", "count", "urls"}
        or payload.get("schema_version") != "shared_images:v1"
        or not isinstance(payload.get("generated_at"), str)
        or not payload["generated_at"].endswith("Z")
        or not isinstance(payload.get("count"), int)
        or not isinstance(urls, list)
        or payload["count"] != len(urls)
        or any(not isinstance(url, str) for url in urls)
    ):
        return frozenset()
    return frozenset(urls)


def _shared_images_from_file(path: str | Path) -> frozenset[str]:
    try:
        target = Path(path).expanduser().resolve()
        stat = target.stat()
    except OSError:
        return frozenset()
    return _read_shared_images(str(target), stat.st_mtime_ns, stat.st_size)


def _usable_image(url: str | None, shared: frozenset[str]) -> bool:
    if not url or media.is_placeholder(url):
        return False
    return _image_key(url) not in shared


def _cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode() + b"==").decode())
        offset = value["offset"]
        if not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_cursor") from exc


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}, separators=(",", ":")).encode()).decode().rstrip("=")


def _candidate(
    *, source_kind: str, item_id: str, url: str, title: str, publisher: str,
    published_at: str, summary: str | None, raw: dict[str, Any] | None = None,
    source: str | None = None, body_text: str | None = None, asset_id: str | None = None,
    chunk_id: str | None = None, stored_image: tuple[str, str] | None = None,
    mentions_subject: bool = True,
) -> dict[str, Any] | None:
    published = _utc(published_at)
    key = canonical_url(url)
    if not published or not key or not title:
        return None
    raw = raw or {}
    image = media.headline_image({"provider": source_kind, "source": source, **raw})
    images: list[tuple[str, str]] = []
    if stored_image:
        images.append(stored_image)
    if image:
        images.append(image)
    priority = {"body": 3, "polygon": 2, "finnhub": 1}[source_kind]
    return {
        "_key": key,
        "_priority": priority,
        "_images": images,
        "_evidence_aliases": {value for value in (asset_id, chunk_id, item_id) if value},
        "_asset_id": asset_id,
        "id": item_id,
        "title": title.strip(),
        "publisher": publisher,
        "published_at": published,
        "url": url,
        "summary": _short(summary or body_text),
        "image_url": None,
        "image_source": None,
        "category": None,
        "category_source": None,
        "mentions_subject": mentions_subject,
        "has_body": source_kind == "body",
        "used_as_evidence": False,
        "phase": "",
    }


_LISTING_SUFFIX = re.compile(
    r"\s+[-\u2013\u2014|]\s+[^-\u2013\u2014|]{1,80}\((?:NASDAQ|NYSE|NYSEARCA|NYSEAMERICAN|AMEX|OTC|CBOE)[^)]*\)\s*$",
    re.IGNORECASE,
)
_ELLIPSIS = re.compile(r"(?:\u2026|\.\.\.)\s*$")
_SHORT_SUFFIX = re.compile(r"\s+[-\u2013\u2014|]\s+[^-\u2013\u2014|]{0,40}$")


def story_title(value: str) -> str:
    """A headline without a syndication suffix such as " - Apple (NASDAQ:AAPL)", lowercased.

    A title cut short ("... - Apple\u2026") also loses its cut suffix, so copies of one story match.
    """
    text = html.unescape(value).strip()
    truncated = bool(_ELLIPSIS.search(text))
    text = _ELLIPSIS.sub("", text)
    text = _LISTING_SUFFIX.sub("", text)
    if truncated:
        text = _SHORT_SUFFIX.sub("", text)
    return re.sub(r"\W+", " ", text.casefold()).strip()


def _evidence_matches(item: dict[str, Any], evidence_ids: set[str]) -> bool:
    aliases = item["_evidence_aliases"]
    asset_id = item.get("_asset_id")
    if any(eid in aliases for eid in evidence_ids):
        return True
    if asset_id:
        return any(eid.startswith(asset_id + ":") for eid in evidence_ids)
    return False


def session_news(
    ticker: str,
    session_date: str,
    *,
    bodies_db: str | Path,
    archive_db: str | Path,
    cursor: str | None = None,
    limit: int = 30,
    evidence_ids: Iterable[str] | None = None,
    categories_db: str | Path | None = None,
    media_db: str | Path | None = None,
    shared_images_path: str | Path | None = None,
    _normalize_display: bool = True,
) -> dict[str, Any]:
    """Aggregate a ticker's in-window body and feed items using read-only DBs."""
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    identity = build_temporal_identity(session_date)
    start = utc_iso_z(identity.information_window_start_at)
    cutoff = utc_iso_z(identity.cutoff_at)
    session_open = utc_iso_z(identity.session_open_at)
    matcher = subject_matcher(ticker, store_db=bodies_db)
    bodies = _read_only(bodies_db)
    archive = _read_only(archive_db)
    cache_path = Path(media_db) if media_db is not None else media.cache_path()
    shared = (
        _shared_images_from_file(shared_images_path)
        if shared_images_path is not None
        else _shared_for(archive_db, cache_path)
    )
    candidates: dict[str, dict[str, Any]] = {}

    def same_headline_story(left: dict[str, Any], right: dict[str, Any]) -> bool:
        """One story syndicated or re-listed: a matching headline published close together.

        Copies land within two minutes; a long, specific headline reposted by another outlet
        (Fool, then Yahoo) can land hours later. Short generic headlines keep the two-minute rule.
        """
        a, b = left["_story"], right["_story"]
        if len(a) < 20 or len(b) < 20:
            return False
        short, long = sorted((a, b), key=len)
        if not (a == b or (len(short) >= 30 and long.startswith(short))):
            return False
        try:
            gap = timefields.stored(left["published_at"]) - timefields.stored(right["published_at"])
        except (TypeError, ValueError):
            return False
        limit = 6 * 3600 if a == b and len(a) >= 40 else 120
        return abs(gap.total_seconds()) <= limit

    def add(candidate: dict[str, Any] | None) -> None:
        if candidate is None:
            return
        candidate_key = candidate["_key"]
        candidate["_story"] = story_title(candidate["title"])
        existing_key = candidate_key if candidate_key in candidates else None
        if existing_key is None:
            existing_key = next((key for key, item in candidates.items() if same_headline_story(item, candidate)), None)
        existing = candidates.get(existing_key) if existing_key is not None else None
        if existing is None:
            candidates[candidate_key] = candidate
            return
        existing["_images"].extend(candidate["_images"])
        existing["_evidence_aliases"].update(candidate["_evidence_aliases"])
        # The story is shown at its first publication and under its plainest headline.
        try:
            first = min((existing, candidate), key=lambda item: timefields.stored(item["published_at"]))
        except (TypeError, ValueError):
            first = existing
        plain = min(
            (existing["title"], candidate["title"]),
            key=lambda title: (bool(_ELLIPSIS.search(title.strip())), len(title)),
        )
        if candidate["_priority"] > existing["_priority"]:
            candidate["_images"].extend(existing["_images"])
            candidate["_evidence_aliases"].update(existing["_evidence_aliases"])
            del candidates[existing_key]
            candidates[candidate_key] = candidate
        kept = candidates[candidate_key] if candidate_key in candidates else existing
        kept["published_at"] = first["published_at"]
        kept["title"] = plain

    try:
        body_tables = _tables(bodies)
        _query_body_articles(bodies, body_tables, ticker=ticker, start=start, cutoff=cutoff,
                             matcher=matcher, add=add)

        archive_tables = _tables(archive)
        _query_finnhub_articles(archive, archive_tables, ticker=ticker, start=start, cutoff=cutoff,
                                matcher=matcher, add=add)
        _query_polygon_articles(archive, archive_tables, ticker=ticker, session_date=session_date,
                                start=start, cutoff=cutoff, matcher=matcher, add=add)
    finally:
        bodies.close()
        archive.close()

    rows = list(candidates.values())
    rows.sort(key=lambda row: (row["published_at"], row["id"]))
    cached_media: dict[str, sqlite3.Row] = {}
    if cache_path.is_file() and rows:
        cache_conn = _read_only(cache_path)
        try:
            if "headline_media" in _tables(cache_conn):
                keys = list(dict.fromkeys(row["_key"] for row in rows))
                for offset in range(0, len(keys), 500):
                    batch = keys[offset:offset + 500]
                    placeholders = ",".join("?" for _ in batch)
                    cached_media.update({
                        row["article_key"]: row
                        for row in cache_conn.execute(
                            "SELECT article_key,image_url,image_source,status,attempts "
                            f"FROM headline_media WHERE article_key IN ({placeholders})",
                            batch,
                        )
                    })
        finally:
            cache_conn.close()
    labels = categorize_llm.cached(categories_db or categorize_llm.default_db(), [row["_key"] for row in rows])
    all_items: list[dict[str, Any]] = []
    evidence = set(evidence_ids or ())
    counts: Counter[str] = Counter()
    images_pending = 0
    for row in rows:
        category = labels.get(row["_key"])
        row["category"] = category
        row["category_source"] = "llm" if category else None
        category = category or "UNCATEGORIZED"
        row["used_as_evidence"] = _evidence_matches(row, evidence)
        row["phase"] = "PRE_OPEN" if row["published_at"] < session_open else "SESSION"
        cached = cached_media.get(row["_key"])
        if cached and cached["status"] == "ok" and cached["image_url"] and cached["image_source"] in ("og", "twitter"):
            row["_images"].append((cached["image_url"], cached["image_source"]))
        image_priority = {"og": 0, "twitter": 0, "polygon": 1, "finnhub": 2}
        for image_url, image_source in sorted(
            row["_images"], key=lambda item: image_priority.get(item[1], 3)
        ):
            if _usable_image(image_url, shared):
                row["image_url"], row["image_source"] = image_url, image_source
                break
        if not row["has_body"] and not row["image_url"] and (
            cached is None or (cached["status"] == "unfetched" and cached["attempts"] < media.MAX_HEADLINE_ATTEMPTS)
        ):
            images_pending += 1
        counts[category] += 1
        item = {key: value for key, value in row.items() if not key.startswith("_")}
        if _normalize_display:
            item["title"] = normalize_display_text(item.get("title"))
            item["summary"] = normalize_display_text(item.get("summary"))
        all_items.append(item)

    offset = _cursor_offset(cursor)
    page = all_items[offset:offset + limit]
    next_cursor = _encode_cursor(offset + len(page)) if offset + len(page) < len(all_items) else None
    return {
        "window": {
            "information_window_start_at": start,
            "cutoff_at": cutoff,
            "session_open_at": session_open,
        },
        "items": page,
        "counts_by_category": {code: counts.get(code, 0)
                               for code in (*categorize_llm.CODES, "UNCATEGORIZED")},
        "next_cursor": next_cursor,
        "images_pending": images_pending,
    }


def category_inputs(ticker: str, session_date: str, *, bodies_db: str | Path | None = None,
                    archive_db: str | Path | None = None) -> list[dict[str, Any]]:
    """Every in-window item of a session as {key, title, summary}, for classification."""
    from catalyst_data.ingestion.collectors import headline_archive

    bodies_db = bodies_db or runtime_store_path("bodies_db", headline_archive.REPO)
    archive_db = archive_db or runtime_store_path("archive_db", headline_archive.REPO)
    items, cursor = [], None
    while True:
        page = session_news(ticker, session_date, bodies_db=bodies_db, archive_db=archive_db,
                            cursor=cursor, limit=100, _normalize_display=False)
        items += [{"key": canonical_url(i["url"]), "title": i["title"], "summary": i["summary"]} for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            return items
