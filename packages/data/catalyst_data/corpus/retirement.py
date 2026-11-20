"""Explicitly retire and purge unused corpus generations."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from catalyst_data.corpus.streaming_publication import (
    ensure_streaming_publication_schema,
)

_INCOMPLETE_STATUSES = (
    "staging", "staging_ready", "manifest_ready", "reconciliation_ready", "lexical_ready",
)


def retire_and_purge_build(conn: sqlite3.Connection, build_id: str, *, reason: str) -> bool:
    """Mark a non-current generation retiring before deleting its per-build rows."""
    conn.execute("SAVEPOINT retire_corpus_build")
    try:
        row = conn.execute(
            """SELECT b.status,b.manifest_id,m.is_current
               FROM corpus_publication_builds b LEFT JOIN corpus_manifest m ON m.manifest_id=b.manifest_id
               WHERE b.build_id=?""",
            (build_id,),
        ).fetchone()
        if row is None:
            conn.execute("RELEASE retire_corpus_build")
            return False
        status, manifest_id, is_current = row
        if is_current:
            raise ValueError(f"cannot retire current corpus build: {build_id}")
        if status not in {"published", *_INCOMPLETE_STATUSES}:
            raise ValueError(f"cannot retire corpus build in status {status!r}: {build_id}")
        retired_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("UPDATE corpus_publication_builds SET status='retiring',updated_at=? WHERE build_id=?",
                     (retired_at, build_id))
        conn.execute(
            """INSERT OR REPLACE INTO corpus_retired_builds(build_id,prior_status,retired_at,reason)
               VALUES (?,?,?,?)""",
            (build_id, status, retired_at, reason),
        )
        for table in (
            "corpus_build_chunks_fts", "corpus_build_chunks", "corpus_build_documents",
            "corpus_build_source_documents", "corpus_build_deltas", "corpus_build_fts_batches",
        ):
            conn.execute(f"DELETE FROM {table} WHERE build_id=?", (build_id,))
        if manifest_id:
            conn.execute("DELETE FROM corpus_manifest WHERE manifest_id=? AND is_current=0", (manifest_id,))
        conn.execute("DELETE FROM corpus_publication_builds WHERE build_id=? AND status='retiring'", (build_id,))
        conn.execute("RELEASE retire_corpus_build")
        return True
    except BaseException:
        conn.execute("ROLLBACK TO retire_corpus_build")
        conn.execute("RELEASE retire_corpus_build")
        raise


def prune_corpus_builds(conn: sqlite3.Connection, *, previous_current_build_id: str | None,
                        keep_published: int = 2,
                        reason: str = "generation retention") -> list[str]:
    """Keep current plus its pre-cutover current; purge all other published and incomplete builds."""
    if keep_published != 2:
        raise ValueError("corpus retention keeps exactly two published generations")
    ensure_streaming_publication_schema(conn)
    current = conn.execute(
        """SELECT b.build_id FROM corpus_manifest m JOIN corpus_publication_builds b
             ON b.manifest_id=m.manifest_id WHERE m.is_current=1 AND b.status='published' LIMIT 1"""
    ).fetchone()
    current_id = None if current is None else str(current[0])
    keep = set()
    if current_id is not None:
        keep.add(current_id)
    if previous_current_build_id is not None:
        previous = conn.execute(
            "SELECT status FROM corpus_publication_builds WHERE build_id=?", (previous_current_build_id,)
        ).fetchone()
        if previous is not None and previous[0] == "published":
            keep.add(previous_current_build_id)
    published = [str(row[0]) for row in conn.execute(
        "SELECT build_id FROM corpus_publication_builds WHERE status='published'"
    )]
    retire = [build_id for build_id in published if build_id not in keep]
    retire.extend(incomplete_build_ids(conn))
    retired = []
    for build_id in dict.fromkeys(retire):
        if retire_and_purge_build(conn, build_id, reason=reason):
            retired.append(build_id)
    conn.commit()
    return retired


def incomplete_build_ids(conn: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in conn.execute(
        "SELECT build_id FROM corpus_publication_builds WHERE status IN ("
        + ",".join("?" for _ in _INCOMPLETE_STATUSES) + ")",
        _INCOMPLETE_STATUSES,
    )]


def purge_incomplete_builds(conn: sqlite3.Connection, *, reason: str = "failed build cleanup") -> list[str]:
    ensure_streaming_publication_schema(conn)
    retired = []
    for build_id in incomplete_build_ids(conn):
        if retire_and_purge_build(conn, build_id, reason=reason):
            retired.append(build_id)
    conn.commit()
    return retired
