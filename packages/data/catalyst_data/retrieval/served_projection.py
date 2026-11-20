"""Served-build projection: a result-identical latency sidecar (WP-B B7a).

Every eligibility statement of the served lexical path reads the
``corpus_served_chunks`` VIEW. Because ``content_text`` sits inline before
``ticker_associations`` and ``available_at``, each such statement walks the
whole build's overflow pages (about 21 s per statement on the served build),
which does not fit the 30 s research-stage budget.

This module copies the served view's *metadata* columns (never
``content_text``) into a small SQLite sidecar once, at dependency-load time,
and verifies the copy against the view before any request is served. The
lexical path then runs its unchanged predicate/ordering SQL generators
against the sidecar and re-reads ``content_text`` from the served DB by
primary key. See ``docs/plans/2026-09-25-wp-b-b7a-served-projection.md``.

The sidecar is never the served DB, is written once through a temporary file
and an atomic rename, and is opened per operation (TSD §16) through
:meth:`ServedProjectionHandle.connect`, which allows TEMP tables (needed by
the two-phase MATCH bridge) while the main schema stays read-only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from .lexical_retriever import resolve_served_build
from .result import SEARCHABLE_STATUSES, RetrievalContractError

SCHEMA_VERSION = "served_projection:v2"
PROJECTION_TABLE = "served_projection_chunks"
FTS_MAP_TABLE = "served_projection_fts_map"
META_TABLE = "served_projection_meta"

#: The projected select list, in order. ``content_text`` is always NULL.
PROJECTED_COLUMNS: tuple[str, ...] = (
    "chunk_id",
    "document_id",
    "available_at",
    "source_class",
    "status",
    "eligibility",
    "chunk_profile_version",
    "manifest_id",
    "ticker_associations",
    "provider_updated_at",
    "content_text",
)

_VIEW_SELECT = (
    "SELECT chunk_id, document_id, available_at, source_class, status, "
    "eligibility, chunk_profile_version, manifest_id, ticker_associations, "
    "provider_updated_at, "
    "NULL AS content_text FROM corpus_served_chunks WHERE manifest_id = ? "
    "ORDER BY chunk_id COLLATE BINARY"
)
_PROJ_SELECT = (
    f"SELECT {', '.join(PROJECTED_COLUMNS)} FROM {PROJECTION_TABLE} "
    "ORDER BY chunk_id COLLATE BINARY"
)
_PROJ_DDL = f"""
CREATE TABLE {PROJECTION_TABLE} (
    chunk_id TEXT NOT NULL PRIMARY KEY COLLATE BINARY,
    document_id TEXT,
    available_at TEXT,
    source_class TEXT,
    status TEXT,
    eligibility TEXT,
    chunk_profile_version TEXT,
    manifest_id TEXT,
    ticker_associations TEXT,
    provider_updated_at TEXT,
    content_text TEXT
)
"""
_FTS_MAP_DDL = f"""
CREATE TABLE {FTS_MAP_TABLE} (
    fts_rowid INTEGER PRIMARY KEY,
    build_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL
)
"""
_META_DDL = f"""
CREATE TABLE {META_TABLE} (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    meta_json TEXT NOT NULL
)
"""


def _fail(reason: str, detail: str) -> RetrievalContractError:
    return RetrievalContractError(f"served_projection_{reason}", detail)


def canonical_row_bytes(row: Iterable[object]) -> bytes:
    """One row of the canonical row digest (B7a §2.1)."""
    return (
        json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


class _RowDigest:
    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self.count = 0

    def update(self, row: Iterable[object]) -> None:
        self._hash.update(canonical_row_bytes(row))
        self.count += 1

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def connect_sidecar(path: str | Path) -> sqlite3.Connection:
    """Open one per-operation sidecar connection.

    ``mode=ro&immutable=1`` keeps the main schema read-only; ``query_only`` is
    deliberately NOT set so the two-phase MATCH bridge can use TEMP tables,
    which live in memory (``temp_store=MEMORY``).
    """
    resolved = Path(path).resolve()
    conn = sqlite3.connect(
        f"file:{resolved}?mode=ro&immutable=1", uri=True, check_same_thread=False
    )
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


@dataclass(frozen=True)
class ServedProjectionHandle:
    """Path, factory and verified identity of one served projection.

    Holds no SQLite connection (TSD §16); every operation calls
    :meth:`connect` and closes what it opened.
    """

    path: Path
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def build_id(self) -> str:
        return str(self.meta["build_id"])

    @property
    def manifest_id(self) -> str:
        return str(self.meta["manifest_id"])

    @property
    def projection_sha256(self) -> str:
        return str(self.meta["projection_sha256"])

    def connect(self) -> sqlite3.Connection:
        return connect_sidecar(self.path)

    def identity(self) -> dict[str, Any]:
        """The fields recorded in retrieval observations and run records."""
        return {
            "build_id": self.build_id,
            "row_count": int(self.meta["row_count"]),
            "projection_sha256": self.projection_sha256,
            "schema_version": str(self.meta["schema_version"]),
        }


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _legacy_branch_rows(conn: sqlite3.Connection, manifest_id: str) -> int:
    """Rows the view's legacy ``corpus_chunks`` branch contributes."""
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "corpus_chunks" not in tables:
        return 0
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM corpus_chunks legacy
            WHERE legacy.manifest_id = ?
              AND EXISTS (
                SELECT 1 FROM corpus_manifest m
                WHERE m.manifest_id = legacy.manifest_id AND m.is_current = 1
              )
              AND NOT EXISTS (
                SELECT 1 FROM corpus_publication_builds b
                JOIN corpus_manifest m
                  ON m.manifest_id = b.manifest_id AND m.is_current = 1
                WHERE b.status = 'published'
              )
            """,
            (manifest_id,),
        ).fetchone()[0]
    )


def _served_fts_index_version(conn: sqlite3.Connection) -> str | None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(lexical_index_state)")
    }
    if "lexical_digest" not in columns:
        return None
    row = conn.execute(
        "SELECT lexical_digest FROM lexical_index_state WHERE singleton_id = 1"
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def build_served_projection(
    served_conn_factory: Callable[[], sqlite3.Connection],
    *,
    expected_manifest_id: str,
    projection_dir: str | Path,
) -> ServedProjectionHandle:
    """Build, verify and atomically publish the served-build sidecar.

    Every verification is fail-closed (B7a §2.1 checks 1–6). Nothing is
    published unless all of them pass.
    """
    started = time.monotonic()
    out_dir = Path(projection_dir)
    if not out_dir.is_dir():
        raise _fail("dir_missing", f"projection_dir {out_dir} is not a directory")

    served = served_conn_factory()
    tmp_path = out_dir / f".served_projection.tmp.{uuid.uuid4().hex}.db"
    write: sqlite3.Connection | None = None
    try:
        # Check 1: the one served build of the expected manifest.
        resolution = resolve_served_build(served, expected_manifest_id=expected_manifest_id)
        build_id = resolution.build_id
        manifest_id = resolution.corpus_manifest_id

        write = sqlite3.connect(tmp_path)
        write.execute("PRAGMA journal_mode=DELETE")
        write.execute(_PROJ_DDL)
        write.execute(_FTS_MAP_DDL)
        write.execute(_META_DDL)

        # Copy the projected select list from the VIEW, digesting it as read.
        view_digest = _RowDigest()
        insert = (
            f"INSERT INTO {PROJECTION_TABLE} ({', '.join(PROJECTED_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in PROJECTED_COLUMNS)})"
        )
        cursor = served.execute(_VIEW_SELECT, (manifest_id,))
        while True:
            batch = cursor.fetchmany(10_000)
            if not batch:
                break
            for row in batch:
                view_digest.update(row)
            write.executemany(insert, batch)

        # Check 2: the view is exactly the build's rows.
        # The view yields only rows of the current manifest: its published
        # branch joins ``is_current`` and its legacy branch is checked below.
        # So one current manifest with exactly one published build means the
        # manifest-filtered copy above is the whole view. (An unfiltered
        # COUNT over the view would materialize every row's content_text.)
        current_manifests = int(
            served.execute(
                "SELECT COUNT(*) FROM corpus_manifest WHERE is_current = 1"
            ).fetchone()[0]
        )
        published_current = int(
            served.execute(
                "SELECT COUNT(*) FROM corpus_publication_builds b "
                "JOIN corpus_manifest m ON m.manifest_id = b.manifest_id "
                "AND m.is_current = 1 WHERE b.status = 'published'"
            ).fetchone()[0]
        )
        if current_manifests != 1 or published_current != 1:
            raise _fail(
                "view_not_build",
                "the served view must be one published build of one current "
                f"manifest: current manifests={current_manifests}, published "
                f"builds of it={published_current}",
            )
        build_total = int(
            served.execute(
                "SELECT COUNT(*) FROM corpus_build_chunks WHERE build_id = ?",
                (build_id,),
            ).fetchone()[0]
        )
        proj_total = int(
            write.execute(f"SELECT COUNT(*) FROM {PROJECTION_TABLE}").fetchone()[0]
        )
        legacy_rows = _legacy_branch_rows(served, manifest_id)
        if not (view_digest.count == build_total == proj_total):
            raise _fail(
                "view_not_build",
                "the served view is not exactly the served build: "
                f"view[manifest]={view_digest.count} build={build_total} "
                f"projection={proj_total}",
            )
        if legacy_rows != 0:
            raise _fail(
                "legacy_branch_active",
                f"the view's legacy branch contributes {legacy_rows} rows",
            )

        # Check 3: the projection re-reads to the same ordered row digest.
        proj_digest = _RowDigest()
        for row in write.execute(_PROJ_SELECT):
            proj_digest.update(row)
        if proj_digest.hexdigest() != view_digest.hexdigest():
            raise _fail(
                "row_digest_mismatch",
                "the projection's ordered row digest differs from the served view",
            )

        # Check 4: storage classes (TEXT everywhere; content_text NULL).
        type_checks = [
            f"SUM(CASE WHEN typeof({col}) <> 'text' THEN 1 ELSE 0 END)"
            for col in PROJECTED_COLUMNS
            if col not in {"content_text", "provider_updated_at"}
        ]
        type_checks.append(
            "SUM(CASE WHEN provider_updated_at IS NOT NULL "
            "AND typeof(provider_updated_at) <> 'text' THEN 1 ELSE 0 END)"
        )
        type_checks.append("SUM(CASE WHEN content_text IS NOT NULL THEN 1 ELSE 0 END)")
        bad = write.execute(
            f"SELECT {', '.join(type_checks)} FROM {PROJECTION_TABLE}"
        ).fetchone()
        if any(int(value or 0) for value in bad):
            raise _fail(
                "storage_class",
                "a projected value is not TEXT (or content_text is not NULL)",
            )

        # FTS map: the served build's FTS rows, by FTS rowid (the rowid MATCH
        # returns). Read from the FTS5 content table (``id`` = rowid, ``c0`` =
        # build_id, ``c1`` = chunk_id): a scan of the virtual table would load
        # every row's content_text across all builds. The layout is checked
        # first and the map is cross-checked against the virtual table below.
        content_columns = [
            str(row[1])
            for row in served.execute(
                "PRAGMA table_info(corpus_build_chunks_fts_content)"
            )
        ]
        if content_columns != ["id", "c0", "c1", "c2"]:
            raise _fail(
                "fts_content_layout",
                f"unexpected FTS content table layout: {content_columns}",
            )
        map_digest = _RowDigest()
        cursor = served.execute(
            "SELECT id, c0, c1 FROM corpus_build_chunks_fts_content "
            "WHERE c0 = ? ORDER BY id",
            (build_id,),
        )
        while True:
            batch = cursor.fetchmany(10_000)
            if not batch:
                break
            for row in batch:
                map_digest.update(row)
            write.executemany(
                f"INSERT INTO {FTS_MAP_TABLE} (fts_rowid, build_id, chunk_id) "
                "VALUES (?, ?, ?)",
                batch,
            )

        write.execute(
            f"CREATE INDEX {FTS_MAP_TABLE}_chunk ON {FTS_MAP_TABLE} (chunk_id)"
        )
        # Lets windowed retrieval start from the dates instead of every match.
        write.execute(
            f"CREATE INDEX {PROJECTION_TABLE}_available ON {PROJECTION_TABLE} (available_at)"
        )
        write.execute("ANALYZE")

        # Check 5: the map re-reads to the same digest, its count is the
        # persisted FTS row count, and it covers exactly the projected chunks.
        reread = _RowDigest()
        for row in write.execute(
            f"SELECT fts_rowid, build_id, chunk_id FROM {FTS_MAP_TABLE} ORDER BY fts_rowid"
        ):
            reread.update(row)
        if reread.hexdigest() != map_digest.hexdigest():
            raise _fail("fts_map_digest_mismatch", "fts_map differs from its source")
        # Cross-check the map against the FTS virtual table itself, by rowid,
        # on a deterministic sample (first, last, and every 997th row).
        sample = write.execute(
            f"SELECT fts_rowid, build_id, chunk_id FROM {FTS_MAP_TABLE} "
            "WHERE fts_rowid IN ("
            f"  SELECT MIN(fts_rowid) FROM {FTS_MAP_TABLE}"
            f"  UNION SELECT MAX(fts_rowid) FROM {FTS_MAP_TABLE}"
            f") OR fts_rowid % 997 = 0"
        ).fetchall()
        for fts_rowid, map_build, map_chunk in sample:
            live = served.execute(
                "SELECT build_id, chunk_id FROM corpus_build_chunks_fts WHERE rowid = ?",
                (fts_rowid,),
            ).fetchone()
            if live is None or (str(live[0]), str(live[1])) != (map_build, map_chunk):
                raise _fail(
                    "fts_map_vtable_mismatch",
                    f"fts_map row {fts_rowid} differs from the FTS virtual table",
                )
        if map_digest.count != int(resolution.lexical_row_count):
            raise _fail(
                "fts_map_count",
                f"fts_map has {map_digest.count} rows; the served build persists "
                f"{resolution.lexical_row_count} FTS rows",
            )
        # The candidate FTS indexes only eligible, searchable-status chunks
        # (``build_candidate_fts``), so a chunk without an FTS row is legal only
        # when it is ineligible or its status is not searchable.
        searchable = ",".join("?" for _ in SEARCHABLE_STATUSES)
        uncovered = write.execute(
            f"""
            SELECT
              (SELECT COUNT(*) FROM {FTS_MAP_TABLE} m
                 WHERE NOT EXISTS (SELECT 1 FROM {PROJECTION_TABLE} c
                                   WHERE c.chunk_id = m.chunk_id)),
              (SELECT COUNT(*) FROM {PROJECTION_TABLE} c
                 WHERE c.status IN ({searchable}) AND c.eligibility = 'eligible'
                   AND NOT EXISTS (SELECT 1 FROM {FTS_MAP_TABLE} m
                                   WHERE m.chunk_id = c.chunk_id))
            """,
            SEARCHABLE_STATUSES,
        ).fetchone()
        if int(uncovered[0]) or int(uncovered[1]):
            raise _fail(
                "fts_map_coverage",
                "fts_map and the projection do not cover the same chunks: "
                f"{int(uncovered[0])} FTS rows without a chunk, "
                f"{int(uncovered[1])} eligible searchable chunks without an FTS row",
            )

        # Check 6: the digest binds definition as well as values.
        view_sql_row = served.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='corpus_served_chunks'"
        ).fetchone()
        if view_sql_row is None:
            raise _fail("view_missing", "corpus_served_chunks view is missing")
        fts_index_version = _served_fts_index_version(served)
        declared = [
            [str(row[1]), str(row[2])]
            for row in write.execute(f"PRAGMA table_info({PROJECTION_TABLE})")
        ]
        projection_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "view_sql": str(view_sql_row[0]),
                    "columns": declared,
                    "schema_version": SCHEMA_VERSION,
                    "row_digest": view_digest.hexdigest(),
                    "fts_map_digest": map_digest.hexdigest(),
                    "fts_index_version": fts_index_version,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        meta: dict[str, Any] = {
            "build_id": build_id,
            "manifest_id": manifest_id,
            "row_count": proj_total,
            "fts_map_count": map_digest.count,
            "row_digest": view_digest.hexdigest(),
            "fts_map_digest": map_digest.hexdigest(),
            "projection_sha256": projection_sha256,
            "schema_version": SCHEMA_VERSION,
            "fts_index_version": fts_index_version,
        }
        write.execute(
            f"INSERT INTO {META_TABLE} (singleton_id, meta_json) VALUES (1, ?)",
            (json.dumps(meta, sort_keys=True, separators=(",", ":")),),
        )
        write.commit()
        write.close()
        write = None
        _fsync_path(tmp_path)
        final_path = out_dir / f"served_projection_{projection_sha256[:16]}.db"
        os.replace(tmp_path, final_path)
        _fsync_path(out_dir)
    except Exception:
        if write is not None:
            write.close()
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        served.close()

    meta["warmup_seconds"] = round(time.monotonic() - started, 3)
    handle = ServedProjectionHandle(path=final_path, meta=MappingProxyType(meta))
    # Re-open through the per-operation factory once: the published file must
    # carry the meta it was verified with.
    check = handle.connect()
    try:
        stored = json.loads(
            check.execute(f"SELECT meta_json FROM {META_TABLE}").fetchone()[0]
        )
    finally:
        check.close()
    if stored.get("projection_sha256") != projection_sha256:
        raise _fail("publish_mismatch", "the published sidecar meta does not match")
    return handle


__all__ = [
    "FTS_MAP_TABLE",
    "META_TABLE",
    "PROJECTED_COLUMNS",
    "PROJECTION_TABLE",
    "SCHEMA_VERSION",
    "ServedProjectionHandle",
    "build_served_projection",
    "canonical_row_bytes",
    "connect_sidecar",
]
