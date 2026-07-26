"""Apply B3 reconciliation decisions to corpus and index state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .reconciliation import ReconciliationResult
from .tombstones import mark_tombstoned


def persist_active_chunks(
    conn: sqlite3.Connection,
    active_chunks: dict[str, dict],
    result: ReconciliationResult,
    manifest_id: str,
) -> None:
    """Persist active chunks without committing the caller-owned transaction."""
    now = datetime.now(timezone.utc).isoformat()
    to_embed = set(result.to_embed)
    metadata_only = set(result.to_update_metadata)

    for chunk_id in sorted(active_chunks):
        chunk = active_chunks[chunk_id]
        exists = conn.execute(
            "SELECT 1 FROM corpus_chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone() is not None
        if exists and chunk_id in to_embed:
            conn.execute(
                "UPDATE corpus_chunks SET status = 'pending_embedding' WHERE chunk_id = ?",
                (chunk_id,),
            )
        status = (
            "pending_embedding" if chunk_id in to_embed
            else "metadata_only" if chunk_id in metadata_only
            else chunk.get("status", "active")
        )
        conn.execute(
            """INSERT INTO corpus_chunks (
                chunk_id, document_id, chunk_profile_version, section_key,
                ordinal, content_text, content_hash, metadata_hash,
                source_class, dedup_cluster_id, cluster_first_available_at,
                representative_document_id, available_at, ticker_associations,
                eligibility, manifest_id, status, boundary_kind,
                body_token_start, body_token_end, body_overlap_tokens,
                prefix_token_count, prefix_truncated, section_parse_degraded,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            ) ON CONFLICT(chunk_id) DO UPDATE SET
                content_text = excluded.content_text,
                content_hash = excluded.content_hash,
                metadata_hash = excluded.metadata_hash,
                source_class = excluded.source_class,
                dedup_cluster_id = excluded.dedup_cluster_id,
                cluster_first_available_at = excluded.cluster_first_available_at,
                representative_document_id = excluded.representative_document_id,
                available_at = excluded.available_at,
                ticker_associations = excluded.ticker_associations,
                eligibility = excluded.eligibility,
                manifest_id = excluded.manifest_id,
                status = excluded.status,
                boundary_kind = excluded.boundary_kind,
                body_token_start = excluded.body_token_start,
                body_token_end = excluded.body_token_end,
                body_overlap_tokens = excluded.body_overlap_tokens,
                prefix_token_count = excluded.prefix_token_count,
                prefix_truncated = excluded.prefix_truncated,
                section_parse_degraded = excluded.section_parse_degraded,
                updated_at = excluded.updated_at""",
            (
                chunk_id, chunk["document_id"], chunk["chunk_profile_version"],
                chunk["section_key"], chunk["ordinal"], chunk["content_text"],
                chunk["content_hash"], chunk["metadata_hash"], chunk["source_class"],
                chunk.get("dedup_cluster_id"), chunk.get("cluster_first_available_at"),
                chunk.get("representative_document_id"), chunk["available_at"],
                chunk["ticker_associations"], chunk["eligibility"], manifest_id,
                status, chunk["boundary_kind"], chunk["body_token_start"],
                chunk["body_token_end"], chunk["body_overlap_tokens"],
                chunk["prefix_token_count"], chunk["prefix_truncated"],
                chunk["section_parse_degraded"], now, now,
            ),
        )
        updated = conn.execute(
            """UPDATE index_state SET
               corpus_item_id = ?, source_kind = ?, content_hash = ?,
               content_text = ?, status = ?, provider = ?, source_type = ?,
               tickers_json = ?, published_utc = ?, metadata_hash = ?,
               is_tombstone = 0
               WHERE chunk_id = ?""",
            (
                chunk["document_id"], chunk.get("source_kind", "article"),
                chunk["content_hash"], chunk["content_text"],
                "pending" if chunk_id in to_embed else status,
                chunk.get("provider"), chunk.get("source_type"),
                chunk["ticker_associations"], chunk["available_at"],
                chunk["metadata_hash"], chunk_id,
            ),
        ).rowcount
        if not updated:
            conn.execute(
                """INSERT INTO index_state (
                   chunk_id, chunk_level, corpus_item_id, source_kind,
                   content_hash, content_text, status, provider, source_type,
                   tickers_json, published_utc, metadata_hash, is_tombstone
                   ) VALUES (?, 'l2', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    chunk_id, chunk["document_id"], chunk.get("source_kind", "article"),
                    chunk["content_hash"], chunk["content_text"],
                    "pending" if chunk_id in to_embed else status,
                    chunk.get("provider"), chunk.get("source_type"),
                    chunk["ticker_associations"], chunk["available_at"],
                    chunk["metadata_hash"],
                ),
            )
    mark_tombstoned(
        conn,
        result.tombstones,
        active_chunk_ids=set(active_chunks),
    )
