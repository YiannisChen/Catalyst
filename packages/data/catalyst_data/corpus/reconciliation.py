"""Corpus reconciliation against persisted chunk and embedding state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class TombstoneRecord:
    chunk_id: str
    document_id: str
    reason: str
    previous_content_hash: str | None = None
    previous_metadata_hash: str | None = None
    replacement_chunk_id: str | None = None


@dataclass
class ReconciliationResult:
    to_embed: list[str] = field(default_factory=list)
    to_update_metadata: list[str] = field(default_factory=list)
    tombstones: list[TombstoneRecord] = field(default_factory=list)


def _index_document_column(conn: sqlite3.Connection) -> str:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(index_state)")}
    if "corpus_item_id" in columns:
        return "corpus_item_id"
    if "document_id" in columns:
        return "document_id"
    raise sqlite3.OperationalError("index_state lacks corpus_item_id/document_id")


def _stored_state(conn: sqlite3.Connection) -> dict[str, dict]:
    stored: dict[str, dict] = {}
    cursor = conn.execute(
        "SELECT chunk_id, document_id, content_hash, metadata_hash, "
        "chunk_profile_version, status, eligibility, dedup_cluster_id "
        "FROM corpus_chunks"
    )
    columns = [item[0] for item in cursor.description]
    for raw_row in cursor:
        row = dict(zip(columns, raw_row))
        stored[row["chunk_id"]] = row

    document_column = _index_document_column(conn)
    query = (
        f"SELECT chunk_id, {document_column} AS document_id, status, "
        "content_hash, metadata_hash FROM index_state WHERE is_tombstone = 0"
    )
    cursor = conn.execute(query)
    columns = [item[0] for item in cursor.description]
    for raw_row in cursor:
        row = dict(zip(columns, raw_row))
        stored.setdefault(row["chunk_id"], {
            "chunk_id": row["chunk_id"],
            "document_id": row["document_id"],
            "content_hash": row["content_hash"],
            "metadata_hash": row["metadata_hash"],
            "chunk_profile_version": "unknown",
            "status": row["status"],
            "eligibility": "eligible",
            "dedup_cluster_id": None,
        })
    return stored


def reconcile(
    conn: sqlite3.Connection,
    active_chunks: dict[str, dict],
    manifest_id: str,
    *,
    commit: bool = True,
) -> ReconciliationResult:
    """Compute deltas and persist tombstones without stealing transactions."""
    stored = _stored_state(conn)
    result = ReconciliationResult()
    active_by_doc: dict[str, list[tuple[str, dict]]] = {}
    for chunk_id, chunk in active_chunks.items():
        active_by_doc.setdefault(chunk.get("document_id", ""), []).append((chunk_id, chunk))

    tombstones: dict[str, TombstoneRecord] = {}
    for chunk_id, active in active_chunks.items():
        previous = stored.get(chunk_id)
        if previous is None:
            result.to_embed.append(chunk_id)
            continue
        if active.get("eligibility") == "ineligible":
            tombstones[chunk_id] = TombstoneRecord(
                chunk_id, active.get("document_id", ""), "eligibility_lost",
                previous.get("content_hash"), previous.get("metadata_hash"),
            )
            continue
        old_cluster = previous.get("dedup_cluster_id")
        new_cluster = active.get("dedup_cluster_id")
        if old_cluster != new_cluster and (old_cluster is not None or new_cluster is not None):
            tombstones[chunk_id] = TombstoneRecord(
                chunk_id, active.get("document_id", ""), "dedup_cluster_reassigned",
                previous.get("content_hash"), previous.get("metadata_hash"),
            )
            result.to_embed.append(chunk_id)
            continue
        if active.get("content_hash") != previous.get("content_hash"):
            result.to_embed.append(chunk_id)
        elif active.get("metadata_hash") != previous.get("metadata_hash"):
            result.to_update_metadata.append(chunk_id)

    for chunk_id, previous in stored.items():
        if chunk_id in active_chunks:
            continue
        document_id = previous.get("document_id", "")
        replacements = active_by_doc.get(document_id, [])
        reason = "document_removed"
        replacement_id = None
        if replacements:
            old_profile = previous.get("chunk_profile_version")
            profile_replacements = [
                item for item in replacements
                if old_profile not in {None, "", "unknown"}
                and item[1].get("chunk_profile_version") != old_profile
            ]
            if profile_replacements:
                reason = "profile_version_replaced"
                replacement_id = sorted(item[0] for item in profile_replacements)[0]
            else:
                reason = "disappeared_child"
        tombstones[chunk_id] = TombstoneRecord(
            chunk_id, document_id, reason,
            previous.get("content_hash"), previous.get("metadata_hash"), replacement_id,
        )

    result.to_embed = sorted(set(result.to_embed))
    result.to_update_metadata = sorted(set(result.to_update_metadata))
    result.tombstones = [tombstones[key] for key in sorted(tombstones)]
    now = datetime.now(timezone.utc).isoformat()
    for item in result.tombstones:
        conn.execute(
            """INSERT OR IGNORE INTO corpus_tombstones
               (chunk_id, document_id, reason, previous_content_hash,
                previous_metadata_hash, replacement_chunk_id, manifest_id,
                tombstoned_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.chunk_id, item.document_id, item.reason,
                item.previous_content_hash, item.previous_metadata_hash,
                item.replacement_chunk_id, manifest_id, now,
            ),
        )
    if commit:
        conn.commit()
    return result
