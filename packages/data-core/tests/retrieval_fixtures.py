"""Production-backed fixtures shared by B4 retrieval tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from catalyst_data.corpus.manifest import publish_manifest
from catalyst_data.migrations import run_migrations
from catalyst_data.retrieval.fts5_builder import build_fts5_index
from conftest import _fresh_db_at_version
from db_fixtures import apply_migration_v9

MANIFEST_A = "a" * 64
MANIFEST_B = "b" * 64
FIXED_TIME = "2026-01-02T00:00:00Z"


def fresh_v10_db() -> sqlite3.Connection:
    conn = _fresh_db_at_version(8)
    apply_migration_v9(conn)
    run_migrations(conn)
    publish_manifest(
        conn,
        manifest_id=MANIFEST_A,
        manifest_json=json.dumps({"manifest_id": MANIFEST_A}),
        created_at="2026-01-01T00:00:00Z",
    )
    conn.execute(
        """INSERT INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, ?, 0, ?)""",
        (MANIFEST_B, json.dumps({"manifest_id": MANIFEST_B}), FIXED_TIME),
    )
    conn.commit()
    return conn


def insert_corpus_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    document_id: str | None = None,
    content_text: str = "AAPL earnings",
    available_at: str = "2026-01-01T09:00:00Z",
    eligibility: str = "eligible",
    ticker_associations: tuple[str, ...] = ("AAPL",),
    manifest_id: str = MANIFEST_A,
    status: str = "active",
    source_class: str = "reported_news",
    chunk_profile_version: str = "news_v2",
    ordinal: str = "0001",
) -> None:
    content_hash = hashlib.sha256(content_text.encode()).hexdigest()
    metadata_hash = hashlib.sha256(b"{}").hexdigest()
    conn.execute(
        """INSERT INTO corpus_chunks (
             chunk_id, document_id, chunk_profile_version, section_key, ordinal,
             content_text, content_hash, metadata_hash, source_class,
             available_at, ticker_associations, eligibility, manifest_id, status,
             boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
             prefix_token_count, prefix_truncated, section_parse_degraded,
             created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            chunk_id, document_id or chunk_id, chunk_profile_version, "body", ordinal,
            content_text, content_hash, metadata_hash, source_class, available_at,
            json.dumps(ticker_associations), eligibility, manifest_id, status,
            "paragraph", 0, 10, 0, 0, 0, 0, FIXED_TIME, FIXED_TIME,
        ),
    )


def build_fts5(conn: sqlite3.Connection, manifest_id: str = MANIFEST_A):
    conn.commit()
    return build_fts5_index(conn, manifest_id, clock=lambda: FIXED_TIME)
