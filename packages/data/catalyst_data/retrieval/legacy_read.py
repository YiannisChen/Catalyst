"""Read-only access to a migrated legacy body cohort, shaped exactly like the old body tables.

The old read paths keep their own scoring, ordering and citability logic; this module only
replaces where their rows come from. Rows are rebuilt from the migrated store and its pinned
catalog, and every value the old tables held is reproduced as stored there (times as whole-second
``...Z`` strings).

The whole cohort is validated once when it is opened: every time in the migrated format, unique
chunks, the catalog and the store naming the same chunks, no missing column. Reads after that
cannot meet data the old tables could not have held, so nothing is skipped or guessed at read
time. Anything that does not validate raises; there is no fallback to the old store."""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ENV = (
    "CATALYST_LEGACY_BODY_STORE",
    "CATALYST_LEGACY_CATALOG_DIR",
    "CATALYST_LEGACY_COHORT",
    "CATALYST_LEGACY_STORE_SHA256",
)
CHUNK_KEYS = ("asset_id", "ordinal", "chunk_id", "text", "content_hash", "body_sha256", "canonical_url",
              "title", "publisher", "source_class", "eligible_at", "fetched_at", "capture_mode")
# The old session SQL, run unchanged on an in-memory copy of the old headline table.
SESSION_SQL = ("SELECT asset_id, MAX(eligible_at) AS t FROM web_headlines WHERE ticker IN ({marks}) "
               "AND eligible_at {op} ? AND eligible_at <= ? GROUP BY asset_id ORDER BY t DESC")
_OLD_TIME = "%Y-%m-%dT%H:%M:%SZ"
_OLD_TIME_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
# The only form the migrated store writes; it maps one-to-one and in order onto the old strings.
_STORED_TIME_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\.000000\+00:00")
# The old catalog's states; any other value is an error, not "not full text".
CONTENT_STATES = frozenset({"FULL_TEXT", "TITLE_ONLY", "METADATA_ONLY"})
_CACHE: dict[tuple[str, ...], "LegacyCohort"] = {}
_CACHE_LOCK = threading.Lock()


class LegacyReadError(RuntimeError):
    """The configured migrated cohort is incomplete or inconsistent."""


def _stored_to_old(value: Any, what: str) -> str:
    match = _STORED_TIME_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise LegacyReadError(f"{what} is not a whole-second UTC time in the migrated format: {value!r}")
    return _old_time(match.group(1) + "Z", what)


def _old_time(value: Any, what: str) -> str:
    if not isinstance(value, str) or _OLD_TIME_RE.fullmatch(value) is None:
        raise LegacyReadError(f"{what} must be YYYY-MM-DDTHH:MM:SSZ: {value!r}")
    try:
        datetime.strptime(value, _OLD_TIME)
    except ValueError as exc:
        raise LegacyReadError(f"{what} is not a calendar time: {value!r}") from exc
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _required(value: Any, what: str) -> Any:
    if value is None or (isinstance(value, str) and not value):
        raise LegacyReadError(f"{what} is missing")
    return value


def configured_reader() -> "LegacyCohort | None":
    """The configured cohort, opened and verified once per process; None when nothing is set."""
    values = tuple(os.environ.get(name) for name in ENV)
    if all(value is None for value in values):
        return None
    if any(not value for value in values):
        raise LegacyReadError("all CATALYST_LEGACY_* settings must be configured together")
    with _CACHE_LOCK:
        if values not in _CACHE:
            _CACHE[values] = open_cohort(values[0], values[1], values[2], store_sha256=values[3])
        return _CACHE[values]


class LegacyCohort:
    """One validated cohort. Safe to share across threads: every SQLite call holds one lock."""

    def __init__(self, conn: sqlite3.Connection, cohort_id: str, store_sha256: str, catalog_sha256: str,
                 membership_sha256: str, membership: dict[str, dict[str, Any]]):
        self._conn = conn
        self._lock = threading.Lock()
        self.cohort_id = cohort_id
        self.store_sha256 = store_sha256
        self.catalog_sha256 = catalog_sha256
        self.membership_sha256 = membership_sha256
        self._chunks: dict[str, dict[str, Any]] = {}
        self._by_asset: dict[str, list[str]] = {}
        self._skipped_non_full_text = 0
        self._load(membership)
        # Opened once and held for the life of the process (the cache never closes a cohort).
        self._headline_conn = self._build_headlines()

    def _load(self, membership: dict[str, dict[str, Any]]) -> None:
        rows = self._conn.execute(
            "SELECT m.chunk_id, m.asset_id, m.ordinal, m.version_id, m.original_body_sha256, b.version_id, "
            "b.title, b.legacy_eligible_at FROM legacy_version_metadata m "
            "LEFT JOIN body_versions b ON b.version_id=m.version_id WHERE m.cohort_id=?", (self.cohort_id,),
        ).fetchall()
        facts: dict[str, set[tuple]] = {}
        for version_id, canonical_url, publisher, source_class in self._conn.execute(
                "SELECT e.version_id, e.canonical_url, e.publisher, e.source_class FROM temporal_evidence_map e "
                "JOIN legacy_version_metadata m ON m.version_id=e.version_id WHERE m.cohort_id=?",
                (self.cohort_id,)):
            facts.setdefault(version_id, set()).add((canonical_url, publisher, source_class))
        if {row[0] for row in rows} != set(membership) or len(rows) != len(membership):
            raise LegacyReadError("catalog membership and the store name different chunks")
        positions: set[tuple[str, int]] = set()
        versions: set[str] = set()
        for chunk_id, asset_id, ordinal, version_id, body_sha, body_version, title, eligible_at in rows:
            if body_version is None:
                raise LegacyReadError(f"body version missing for chunk {chunk_id!r}")
            if type(ordinal) is not int or ordinal < 1:
                raise LegacyReadError(f"ordinal of {chunk_id!r} is not a positive integer: {ordinal!r}")
            if version_id in versions:
                raise LegacyReadError(f"version {version_id!r} backs more than one chunk")
            versions.add(version_id)
            if (asset_id, ordinal) in positions:
                raise LegacyReadError(f"duplicate (asset_id, ordinal) for chunk {chunk_id!r}")
            positions.add((asset_id, ordinal))
            attrs = membership[chunk_id].get("legacy_attributes")
            if not isinstance(attrs, dict):
                raise LegacyReadError(f"membership legacy_attributes invalid for {chunk_id!r}")
            state = _required(attrs.get("content_state"), f"content_state of {chunk_id!r}")
            if state not in CONTENT_STATES:
                raise LegacyReadError(f"content_state of {chunk_id!r} is not one of {sorted(CONTENT_STATES)}: {state!r}")
            version_facts = facts.get(version_id, set())
            if len(version_facts) != 1:
                raise LegacyReadError(f"temporal evidence missing or conflicting for chunk {chunk_id!r}")
            canonical_url, publisher, source_class = next(iter(version_facts))
            self._chunks[chunk_id] = {
                "asset_id": _required(asset_id, f"asset_id of {chunk_id!r}"),
                "ordinal": ordinal,
                "chunk_id": chunk_id,
                "version_id": version_id,
                "body_sha256": _required(body_sha, f"body_sha256 of {chunk_id!r}"),
                "canonical_url": _required(canonical_url, f"canonical_url of {chunk_id!r}"),
                "title": _required(title, f"title of {chunk_id!r}") if title != "" else title,
                "publisher": _required(publisher, f"publisher of {chunk_id!r}"),
                "source_class": _required(source_class, f"source_class of {chunk_id!r}"),
                "eligible_at": _stored_to_old(eligible_at, f"eligible_at of {chunk_id!r}"),
                "fetched_at": _old_time(_required(attrs.get("fetched_at"), f"fetched_at of {chunk_id!r}"),
                                        f"fetched_at of {chunk_id!r}"),
                "capture_mode": _required(attrs.get("capture_mode"), f"capture_mode of {chunk_id!r}"),
                "full_text": state == "FULL_TEXT",
            }
            if state != "FULL_TEXT":
                self._skipped_non_full_text += 1
        assets: dict[str, list[str]] = {}
        for chunk_id, chunk in self._chunks.items():
            assets.setdefault(chunk["asset_id"], []).append(chunk_id)
        for asset_id, ids in assets.items():
            ids.sort(key=lambda chunk_id: self._chunks[chunk_id]["ordinal"])
            if [self._chunks[c]["ordinal"] for c in ids] != list(range(1, len(ids) + 1)):
                raise LegacyReadError(f"asset {asset_id!r} ordinals are not 1..n")
            states = {self._chunks[c]["full_text"] for c in ids}
            if states == {True, False}:
                # Serving part of an asset would hand the old code a body with a gap in it.
                raise LegacyReadError(f"asset {asset_id!r} mixes full-text and other chunks")
            if states == {True}:
                self._by_asset[asset_id] = ids
        # Full-text chunks ordered by their old eligible_at string, for the window scan.
        self._by_time = sorted((c["eligible_at"], chunk_id) for chunk_id, c in self._chunks.items() if c["full_text"])
        self._times = [t for t, _ in self._by_time]

    def _build_headlines(self) -> sqlite3.Connection:
        times: dict[tuple[str, str], str] = {}
        conflicts: set[str] = set()
        for asset_id, ticker, eligible_at, flags in self._conn.execute(
                "SELECT m.asset_id, e.ticker, e.eligible_at, e.time_flags_json FROM legacy_version_metadata m "
                "JOIN temporal_evidence_map e ON e.version_id=m.version_id WHERE m.cohort_id=?", (self.cohort_id,)):
            value = _stored_to_old(eligible_at, f"headline time of {asset_id!r}/{ticker!r}")
            if times.setdefault((asset_id, ticker), value) != value:
                raise LegacyReadError(f"headline time conflicts for asset {asset_id!r} ticker {ticker!r}")
            if flags and "legacy_time_conflict" in flags:
                conflicts.add(asset_id)
        self._time_conflicts = sorted(conflicts)
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        built = False
        try:
            conn.executescript("""
                CREATE TABLE web_headlines(asset_id TEXT NOT NULL, ticker TEXT NOT NULL,
                    eligible_at TEXT NOT NULL, PRIMARY KEY(asset_id, ticker));
                CREATE INDEX idx_web_headlines_ticker_time ON web_headlines(ticker, eligible_at);
            """)
            conn.executemany("INSERT INTO web_headlines VALUES (?,?,?)",
                             [(asset, ticker, when) for (asset, ticker), when in sorted(times.items())])
            built = True
        finally:
            if not built:
                conn.close()
        return conn

    def _texts(self, chunk_ids: Sequence[str]) -> dict[str, str]:
        version_to_chunk = {self._chunks[c]["version_id"]: c for c in chunk_ids}
        out: dict[str, str] = {}
        versions = list(version_to_chunk)
        with self._lock:
            for start in range(0, len(versions), 400):
                batch = versions[start:start + 400]
                for version_id, body in self._conn.execute(
                        "SELECT version_id, body FROM body_versions WHERE version_id IN ("
                        + ",".join("?" for _ in batch) + ")", batch):
                    if body is None:
                        raise LegacyReadError(f"body missing for version {version_id!r}")
                    out[version_to_chunk[version_id]] = body
        if len(out) != len(version_to_chunk):
            raise LegacyReadError("body versions disappeared after the cohort was validated")
        return out

    def _row(self, chunk_id: str, text: str) -> dict[str, Any]:
        chunk = self._chunks[chunk_id]
        values = dict(chunk, text=text, content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest())
        return {key: values[key] for key in CHUNK_KEYS}

    def chunks_for_assets(self, asset_ids: Sequence[str], max_ordinal: int) -> dict[str, list[dict[str, Any]]]:
        """As the old bodies_for_assets: per batch of 400 unique ids, assets in id order, chunks by ordinal."""
        unique = list(dict.fromkeys(asset_ids))
        out: dict[str, list[dict[str, Any]]] = {}
        for start in range(0, len(unique), 400):
            batch = sorted(set(unique[start:start + 400]) & set(self._by_asset), key=lambda a: a.encode("utf-8"))
            ids = [c for a in batch for c in self._by_asset[a] if self._chunks[c]["ordinal"] <= max_ordinal]
            texts = self._texts(ids)
            for chunk_id in ids:
                out.setdefault(self._chunks[chunk_id]["asset_id"], []).append(self._row(chunk_id, texts[chunk_id]))
        return out

    def session_assets(self, tickers: Sequence[str], window_start_at: str, cutoff_at: str,
                       *, include_window_start: bool) -> list[str]:
        if not tickers:
            return []
        sql = SESSION_SQL.format(marks=",".join("?" for _ in tickers), op=">=" if include_window_start else ">")
        with self._lock:
            return [row[0] for row in self._headline_conn.execute(sql, (*tickers, window_start_at, cutoff_at))]

    def session_query_plan(self, tickers: Sequence[str], *, include_window_start: bool) -> list[str]:
        """The plan of the session SQL on the in-memory table, for comparison with the old store."""
        sql = SESSION_SQL.format(marks=",".join("?" for _ in tickers), op=">=" if include_window_start else ">")
        with self._lock:
            return query_plan(self._headline_conn, sql, (*tickers, "", ""))

    def window_asset_rows(self, start_iso: str, end_iso: str) -> list[tuple[Any, ...]]:
        """The old search_local SQL's rows: chunks whose eligible_at string is BETWEEN the bounds, grouped
        by asset in asset_id order, text joined by one space in ordinal order. An asset whose filtered
        chunks disagree on a grouped column raises; the old SQL would have picked one silently."""
        start, end = _old_time(start_iso, "window start"), _old_time(end_iso, "window end")
        lo, hi = bisect.bisect_left(self._times, start), bisect.bisect_right(self._times, end)
        grouped: dict[str, list[str]] = {}
        for _, chunk_id in self._by_time[lo:hi]:
            grouped.setdefault(self._chunks[chunk_id]["asset_id"], []).append(chunk_id)
        result = []
        for asset_id in sorted(grouped, key=lambda value: value.encode("utf-8")):
            ids = sorted(grouped[asset_id], key=lambda c: self._chunks[c]["ordinal"])
            columns = {tuple(self._chunks[c][k] for k in ("title", "publisher", "canonical_url", "source_class",
                                                          "eligible_at")) for c in ids}
            if len(columns) != 1:
                raise LegacyReadError(f"asset {asset_id!r} has inconsistent grouped columns")
            texts = self._texts(ids)
            result.append((asset_id, *columns.pop(), " ".join(texts[c] for c in ids)))
        return result

    def titles_for_chunks(self, chunk_ids: Sequence[str]) -> dict[str, str]:
        """Titles of the full-text chunks that exist, as the old ``SELECT chunk_id, title ... WHERE chunk_id IN``."""
        return {c: self._chunks[c]["title"] for c in dict.fromkeys(chunk_ids)
                if c in self._chunks and self._chunks[c]["full_text"]}

    def audit(self) -> dict[str, Any]:
        return {
            "cohort_id": self.cohort_id,
            "store_sha256": self.store_sha256,
            "catalog_sha256": self.catalog_sha256,
            "membership_sha256": self.membership_sha256,
            "chunks": len(self._chunks),
            "skipped_non_full_text": self._skipped_non_full_text,
            "time_conflict_asset_ids": self._time_conflicts,
            "sqlite_version": sqlite3.sqlite_version,
        }


def query_plan(conn: sqlite3.Connection, sql: str, args: Sequence[Any]) -> list[str]:
    return [row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, tuple(args))]


def open_cohort(store_path: str | Path, catalog_dir: str | Path, cohort_id: str,
                *, store_sha256: str) -> LegacyCohort:
    store_path, catalog_dir = Path(store_path), Path(catalog_dir)
    try:
        actual_store_sha = _file_sha256(store_path)
        catalog_bytes = (catalog_dir / "catalog.json").read_bytes()
        membership_bytes = (catalog_dir / "membership.jsonl").read_bytes()
    except OSError as exc:
        raise LegacyReadError("legacy store or catalog file is unavailable") from exc
    if actual_store_sha != store_sha256:
        raise LegacyReadError("legacy store sha256 mismatch")
    wal = Path(str(store_path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        # immutable=1 ignores the WAL, and the hash covers only the main file.
        raise LegacyReadError("legacy store has a non-empty WAL sidecar")
    catalog_sha = hashlib.sha256(catalog_bytes).hexdigest()
    membership_sha = hashlib.sha256(membership_bytes).hexdigest()
    conn = None
    try:
        conn = sqlite3.connect(f"{store_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True,
                               check_same_thread=False)
        values = conn.execute(
            "SELECT DISTINCT cohort_manifest_sha256, membership_sha256 FROM legacy_version_metadata "
            "WHERE cohort_id=?", (cohort_id,)).fetchall()
        if len(values) != 1:
            raise LegacyReadError("cohort manifest hashes are missing or inconsistent")
        if values[0] != (catalog_sha, membership_sha):
            raise LegacyReadError("legacy catalog or membership sha256 mismatch")
        membership: dict[str, dict[str, Any]] = {}
        for line in membership_bytes.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("chunk_id"), str):
                raise LegacyReadError("membership line is not an object with a chunk_id")
            if record["chunk_id"] in membership:
                raise LegacyReadError(f"membership repeats chunk {record['chunk_id']!r}")
            membership[record["chunk_id"]] = record
        return LegacyCohort(conn, cohort_id, actual_store_sha, catalog_sha, membership_sha, membership)
    except Exception as exc:
        if conn is not None:
            conn.close()
        if isinstance(exc, LegacyReadError):
            raise
        raise LegacyReadError("invalid migrated legacy cohort") from exc
