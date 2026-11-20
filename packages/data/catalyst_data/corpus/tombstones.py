"""Persistence helpers for B3 corpus tombstones."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .reconciliation import TombstoneRecord

TOMBSTONE_REASONS = frozenset({
    "document_removed",
    "eligibility_lost",
    "profile_version_replaced",
    "disappeared_child",
    "dedup_cluster_reassigned",
})


def mark_tombstoned(
    conn: sqlite3.Connection,
    records: Iterable["TombstoneRecord"],
    *,
    active_chunk_ids: set[str] | None = None,
) -> None:
    """Mark prior corpus and embedding rows inactive in the caller transaction."""
    active_chunk_ids = active_chunk_ids or set()
    for record in records:
        if record.chunk_id in active_chunk_ids:
            continue
        conn.execute(
            "UPDATE corpus_chunks SET status = 'tombstoned' WHERE chunk_id = ?",
            (record.chunk_id,),
        )
        conn.execute(
            "UPDATE index_state SET is_tombstone = 1 WHERE chunk_id = ?",
            (record.chunk_id,),
        )
