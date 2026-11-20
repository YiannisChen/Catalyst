"""Select a previously published corpus and lexical build without deleting builds."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from catalyst_data.corpus.locking import acquire_corpus_lock, release_corpus_lock


def rollback_database(db_path: str | Path, *, to_build_id: str | None = None,
                      expected_current_build_id: str | None = None,
                      lock_held: bool = False) -> dict[str, str]:
    lock_fd = None if lock_held else acquire_corpus_lock(db_path)
    if not lock_held and lock_fd is None:
        raise RuntimeError("corpus generation lock is busy")
    try:
        return _rollback_database_locked(
            db_path, to_build_id=to_build_id, expected_current_build_id=expected_current_build_id
        )
    finally:
        if lock_fd is not None:
            release_corpus_lock(lock_fd)


def _rollback_database_locked(db_path: str | Path, *, to_build_id: str | None = None,
                              expected_current_build_id: str | None = None) -> dict[str, str]:
    path = Path(db_path)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            """SELECT b.build_id
               FROM corpus_manifest m JOIN corpus_publication_builds b
                 ON b.manifest_id=m.manifest_id AND b.status='published'
               WHERE m.is_current=1
               ORDER BY b.published_at DESC,b.created_at DESC,b.build_id COLLATE BINARY DESC
               LIMIT 1"""
        ).fetchone()
        if current is None:
            raise ValueError("no current published corpus build")
        before = str(current[0])
        if expected_current_build_id is not None and before != expected_current_build_id:
            raise ValueError(f"current build changed: expected {expected_current_build_id}, found {before}")

        if to_build_id is None:
            target = conn.execute(
                """SELECT build_id FROM corpus_publication_builds
                   WHERE status='published' AND build_id!=?
                   ORDER BY published_at DESC,created_at DESC,build_id COLLATE BINARY DESC
                   LIMIT 1""",
                (before,),
            ).fetchone()
        else:
            target = conn.execute(
                "SELECT build_id FROM corpus_publication_builds WHERE build_id=? AND status='published'",
                (to_build_id,),
            ).fetchone()
        if target is None:
            raise ValueError("no previous published build" if to_build_id is None
                             else f"target build is not published: {to_build_id}")
        after = str(target[0])
        row = conn.execute(
            """SELECT manifest_id,chunk_count,manifest_json,lexical_expected_digest,
                      lexical_digest,lexical_row_count,lexical_ready
               FROM corpus_publication_builds WHERE build_id=?""",
            (after,),
        ).fetchone()
        if row is None or not row[0]:
            raise ValueError("published target is missing its manifest")
        manifest_id, chunk_count, manifest_json, expected, digest, lexical_rows, lexical_ready = row
        if (not lexical_ready or not expected or expected != digest
                or int(lexical_rows) != int(chunk_count)):
            raise ValueError("published target has incomplete lexical readiness")
        fts_rows = conn.execute(
            "SELECT COUNT(*) FROM corpus_build_chunks_fts WHERE build_id=?", (after,)
        ).fetchone()[0]
        if int(fts_rows) != int(lexical_rows):
            raise ValueError("published target FTS row count does not match its build record")
        if conn.execute(
            "SELECT 1 FROM corpus_manifest WHERE manifest_id=?", (manifest_id,)
        ).fetchone() is None:
            raise ValueError("published target manifest is missing")

        lexical_columns = {row[1] for row in conn.execute("PRAGMA table_info(lexical_index_state)")}
        if not {"lexical_generation_id", "lexical_digest"} <= lexical_columns:
            raise ValueError("lexical index state does not support published generations")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("UPDATE corpus_manifest SET is_current=0 WHERE is_current=1")
        conn.execute(
            "UPDATE corpus_manifest SET manifest_json=?,is_current=1 WHERE manifest_id=?",
            (manifest_json, manifest_id),
        )
        conn.execute("DELETE FROM lexical_index_state")
        conn.execute(
            """INSERT INTO lexical_index_state
               (singleton_id,schema_version,corpus_manifest_id,mode_served,row_count,built_at,
                lexical_generation_id,lexical_digest)
               VALUES (1,'1.0.0',?,'fts5',?,?,?,?)""",
            (manifest_id, lexical_rows, now, after, digest),
        )
        conn.commit()
        return {"before": before, "after": after}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--to", dest="to_build_id")
    args = parser.parse_args(argv)
    try:
        result = rollback_database(args.db, to_build_id=args.to_build_id)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"rollback failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"current_build_before={result['before']}")
    print(f"current_build_after={result['after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
