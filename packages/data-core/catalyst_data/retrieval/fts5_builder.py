"""FTS5 index builder — atomic build with BEGIN IMMEDIATE, rejection of
active caller transactions, and no broad exception handling.

Contract: §0.1 (Migration v10 / build_fts5_index)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable


class LexicalIndexBuildError(Exception):
    """Typed error for lexical index build failures."""
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class LexicalIndexBuildResult:
    manifest_id: str
    mode_served: str  # 'fts5' or 'sql_like'
    row_count: int
    fallback_reason: str | None = None


def build_fts5_index(
    conn: sqlite3.Connection,
    requested_manifest_id: str,
    *,
    clock: Callable[[], str],
    failure_injector: Callable[[str], None] | None = None,
) -> LexicalIndexBuildResult:
    """Build (or rebuild) the FTS5 lexical index for a corpus manifest.

    - Rejects conn.in_transaction with LexicalIndexBuildError(code='transaction_active')
    - Opens its own BEGIN IMMEDIATE transaction
    - Deletes old FTS rows, inserts eligible rows, upserts lexical_index_state
    - On any failure, rolls back the transaction (preserving old state)
    - Does not catch broad Exception or non-FTS5 OperationalError
    """
    # Reject active caller transaction
    if conn.in_transaction:
        raise LexicalIndexBuildError(
            code="transaction_active",
            message="build_fts5_index requires no active transaction on conn",
        )

    # Validate manifest is current
    current = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current = 1"
    ).fetchone()
    if current is None or current[0] != requested_manifest_id:
        raise LexicalIndexBuildError(
            code="manifest_not_current",
            message=f"Manifest {requested_manifest_id} is not current",
        )

    # Count eligible rows (outside transaction — read-only)
    eligible_count = conn.execute(
        """SELECT COUNT(*) FROM corpus_chunks
           WHERE manifest_id = ?
             AND status IN ('active', 'pending_embedding', 'embedded', 'metadata_only')
             AND eligibility = 'eligible'""",
        (requested_manifest_id,),
    ).fetchone()[0]

    # Check if FTS5 was available at migration time
    fts_available = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='corpus_chunks_fts'"
    ).fetchone() is not None

    conn.execute("BEGIN IMMEDIATE")
    with conn:
        if not fts_available:
            now = clock()
            conn.execute("DELETE FROM lexical_index_state")
            conn.execute(
                """INSERT INTO lexical_index_state
                   (singleton_id, schema_version, corpus_manifest_id, mode_served,
                    fallback_reason, row_count, built_at)
                   VALUES (1, '1.0.0', ?, 'sql_like', 'fts5_unavailable', ?, ?)""",
                (requested_manifest_id, eligible_count, now),
            )
            return LexicalIndexBuildResult(
                manifest_id=requested_manifest_id,
                mode_served="sql_like",
                row_count=eligible_count,
                fallback_reason="fts5_unavailable",
            )

        # Delete old FTS rows
        conn.execute("DELETE FROM corpus_chunks_fts")
        if failure_injector is not None:
            failure_injector("after_delete")

        # Insert eligible rows into FTS in bounded batches within this transaction.
        cursor = conn.execute(
            """SELECT manifest_id, chunk_id, content_text FROM corpus_chunks
               WHERE manifest_id = ?
                 AND status IN ('active', 'pending_embedding', 'embedded', 'metadata_only')
                 AND eligibility = 'eligible'""",
            (requested_manifest_id,),
        )

        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for row in rows:
                conn.execute(
                    "INSERT INTO corpus_chunks_fts (manifest_id, chunk_id, content_text) "
                    "VALUES (?, ?, ?)",
                    (row[0], row[1], row[2]),
                )
        if failure_injector is not None:
            failure_injector("after_insert")

        # Upsert lexical_index_state
        now = clock()
        conn.execute("DELETE FROM lexical_index_state")
        if failure_injector is not None:
            failure_injector("before_state_insert")
        conn.execute(
            """INSERT INTO lexical_index_state
               (singleton_id, schema_version, corpus_manifest_id, mode_served,
                row_count, built_at)
               VALUES (1, '1.0.0', ?, 'fts5', ?, ?)""",
            (requested_manifest_id, eligible_count, now),
        )

        return LexicalIndexBuildResult(
            manifest_id=requested_manifest_id,
            mode_served="fts5",
            row_count=eligible_count,
        )
