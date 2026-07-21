"""Normalized provenance writer — many-to-many entity-to-raw mapping."""
from __future__ import annotations

import sqlite3


def record_provenance(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    entity_version: str,
    raw_asset_id: str,
    normalizer_version: str = "1.0.0",
) -> None:
    """Record a provenance link.  Idempotent via composite PK."""
    conn.execute(
        """INSERT OR IGNORE INTO normalized_provenance
           (entity_type, entity_id, entity_version, raw_asset_id,
            normalizer_version, created_at)
           VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
        (entity_type, entity_id, entity_version, raw_asset_id, normalizer_version),
    )
