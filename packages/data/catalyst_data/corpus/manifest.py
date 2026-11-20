"""Deterministic CorpusManifest construction and atomic publication."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .reconciliation import ReconciliationResult, reconcile
from .updater import persist_active_chunks

INVENTORY_FIELDS = (
    "chunk_id",
    "document_id",
    "chunk_profile_version",
    "section_key",
    "ordinal",
    "content_hash",
    "metadata_hash",
    "available_at",
    "source_class",
    "dedup_cluster_id",
    "cluster_first_available_at",
    "representative_document_id",
    "eligibility",
)


class InjectedReconciliationFailure(RuntimeError):
    """Test-only failure injected between reconciliation and publication."""


def build_manifest(
    *,
    normalization_version: str,
    chunk_profile_versions: dict[str, str],
    source_classifier_version: str,
    certified_snapshot_identity: str,
    active_chunk_inventory: list[dict],
    tokenizer_revision: str,
    embedding_revision: str | None = None,
) -> dict:
    inventory: list[dict] = []
    for item in active_chunk_inventory:
        missing = [field for field in INVENTORY_FIELDS if field not in item]
        if missing:
            raise ValueError(f"active inventory missing fields: {', '.join(missing)}")
        inventory.append({field: item[field] for field in INVENTORY_FIELDS})
    return {
        "normalization_version": normalization_version,
        "chunk_profile_versions": chunk_profile_versions,
        "source_classifier_version": source_classifier_version,
        "certified_snapshot_identity": certified_snapshot_identity,
        "sorted_active_chunk_inventory": sorted(
            inventory, key=lambda item: item["chunk_id"]
        ),
        "tokenizer_revision": tokenizer_revision,
        "tokenizer_model_id": "BAAI/bge-m3",
        "embedding_revision": embedding_revision,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def compute_manifest_id(manifest: dict) -> str:
    identity = {
        "normalization_version": manifest["normalization_version"],
        "chunk_profile_versions": manifest["chunk_profile_versions"],
        "source_classifier_version": manifest["source_classifier_version"],
        "certified_snapshot_identity": manifest["certified_snapshot_identity"],
        "sorted_active_chunk_inventory": manifest["sorted_active_chunk_inventory"],
        "tokenizer_revision": manifest["tokenizer_revision"],
        "embedding_revision_or_null": manifest.get("embedding_revision"),
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def publish_manifest(
    conn: sqlite3.Connection,
    *,
    manifest_id: str,
    manifest_json: str,
    created_at: str | None = None,
) -> None:
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("UPDATE corpus_manifest SET is_current = 0 WHERE is_current = 1")
        conn.execute(
            """INSERT INTO corpus_manifest
               (manifest_id, manifest_json, is_current, created_at)
               VALUES (?, ?, 1, ?)""",
            (manifest_id, manifest_json, created_at),
        )


def reconcile_and_publish(
    conn: sqlite3.Connection,
    *,
    next_manifest_id: str,
    next_manifest_json: str,
    active_chunks: dict[str, dict],
    fail_after_chunk_writes: bool = False,
) -> ReconciliationResult:
    """Reconcile and switch the current manifest in one transaction."""
    created_at = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """INSERT INTO corpus_manifest
               (manifest_id, manifest_json, is_current, created_at)
               VALUES (?, ?, 0, ?)
               ON CONFLICT(manifest_id) DO UPDATE SET
                   manifest_json = excluded.manifest_json,
                   created_at = excluded.created_at""",
            (next_manifest_id, next_manifest_json, created_at),
        )
        result = reconcile(
            conn,
            active_chunks,
            manifest_id=next_manifest_id,
            commit=False,
        )
        persist_active_chunks(conn, active_chunks, result, next_manifest_id)
        if fail_after_chunk_writes:
            raise InjectedReconciliationFailure("injected before manifest publication")
        conn.execute("UPDATE corpus_manifest SET is_current = 0 WHERE is_current = 1")
        conn.execute(
            "UPDATE corpus_manifest SET is_current = 1 WHERE manifest_id = ?",
            (next_manifest_id,),
        )
    return result
