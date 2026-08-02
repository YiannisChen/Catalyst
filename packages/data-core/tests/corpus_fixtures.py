"""B3 shared corpus fixtures — deterministic splitter oracles, reconciliation
cases, and manifest helpers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

TOKENIZER_REVISION: str = "5617a9f61b028005a4858fdac845db406aefb181"
TOKENIZER_MODEL_ID: str = "BAAI/bge-m3"


def stable_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# ── Independent splitter fixtures and literal contract oracles ─────────────

_BOUNDARY_TITLE = "Boundary Report"
_BOUNDARY_PARAGRAPH_1 = " ".join(["market"] * 300)
_BOUNDARY_PARAGRAPH_2 = " ".join(["apple"] * 300)


def deterministic_boundary_article_fixture() -> dict:
    return {
        "document_id": "poly:boundary",
        "title": _BOUNDARY_TITLE,
        "description": f"{_BOUNDARY_PARAGRAPH_1}\n\n{_BOUNDARY_PARAGRAPH_2}",
        "available_at": "2026-01-01T00:00:00Z",
        "ticker_associations": '["AAPL"]',
    }


EXPECTED_BOUNDARY_CHUNK_TEXTS = [
    'Boundary Report\nBoundary Report\nmarket market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market',
    'Boundary Report\nmarket market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market market\n\napple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple apple',
]
EXPECTED_BOUNDARY_KINDS = ["paragraph", "document_end"]
EXPECTED_BODY_STARTS = [0, 256]
EXPECTED_BODY_ENDS = [304, 604]
EXPECTED_BODY_OVERLAPS = [0, 48]


def long_title_article_fixture() -> dict:
    return {
        "document_id": "poly:long-title",
        "title": " ".join(["title"] * 100),
        "description": " ".join(["market"] * 700),
        "available_at": "2026-01-01T00:00:00Z",
    }


def no_boundary_article_fixture() -> dict:
    return {
        "document_id": "poly:no-boundary",
        "title": "Fallback",
        "description": " ".join(["market"] * 700),
        "available_at": "2026-01-01T00:00:00Z",
    }


def raw_8k_duplicate_item_fixture() -> dict:
    return {
        "document_id": "sec:duplicate:8-K",
        "filing_type": "8-K",
        "raw_text": (
            "Cover page\n"
            "Item 1.01 First Agreement\nFirst body.\n"
            "Item 1.01 Second Agreement\nSecond body.\n"
            "Item 2.03 Direct Financial Obligation\nThird body."
        ),
        "available_at": "2026-01-01T00:00:00Z",
    }


def exhibit_99_1_fixture() -> dict:
    return {
        "document_id": "sec:exhibit:EX-99.1",
        "filing_type": "EX-99.1",
        "raw_text": (
            f"{' '.join(['market'] * 300)}\n\n"
            f"{' '.join(['apple'] * 300)}"
        ),
        "available_at": "2026-01-01T00:00:00Z",
    }


EXPECTED_EXHIBIT_BOUNDARY_KINDS = ["paragraph", "document_end"]


# ── Reconciliation cases ────────────────────────────────────────────────────

@dataclass
class ReconciliationCase:
    name: str
    db: sqlite3.Connection
    active_chunks: dict
    next_manifest_id: str
    expected_to_embed: list[str] = field(default_factory=list)
    expected_metadata_updates: list[str] = field(default_factory=list)
    expected_tombstones: list[str] = field(default_factory=list)


NINE_RECONCILIATION_CASE_NAMES: list[str] = [
    "new_chunk", "unchanged_chunk", "content_change",
    "metadata_only_change", "document_removal", "eligibility_loss",
    "profile_version_replacement", "disappeared_child",
    "dedup_cluster_reassignment",
]


def _make_reconciliation_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS index_state (
            chunk_id TEXT PRIMARY KEY,
            corpus_item_id TEXT NOT NULL,
            source_kind TEXT NOT NULL DEFAULT 'article',
            status TEXT NOT NULL DEFAULT 'active',
            content_hash TEXT,
            metadata_hash TEXT,
            is_tombstone INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS corpus_chunks (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL,
            section_key TEXT NOT NULL,
            ordinal TEXT NOT NULL,
            content_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            metadata_hash TEXT NOT NULL,
            source_class TEXT NOT NULL,
            dedup_cluster_id TEXT,
            cluster_first_available_at TEXT,
            representative_document_id TEXT,
            available_at TEXT NOT NULL,
            ticker_associations TEXT NOT NULL,
            eligibility TEXT NOT NULL,
            manifest_id TEXT,
            status TEXT NOT NULL,
            boundary_kind TEXT NOT NULL,
            body_token_start INTEGER NOT NULL,
            body_token_end INTEGER NOT NULL,
            body_overlap_tokens INTEGER NOT NULL,
            prefix_token_count INTEGER NOT NULL,
            prefix_truncated INTEGER NOT NULL,
            section_parse_degraded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS corpus_tombstones (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            previous_content_hash TEXT,
            previous_metadata_hash TEXT,
            replacement_chunk_id TEXT,
            manifest_id TEXT NOT NULL,
            tombstoned_at TEXT NOT NULL
        );
    """)
    db.commit()
    return db


def _seed_chunk(db, chunk_id, document_id, content_hash, metadata_hash,
                status="embedded", eligibility="eligible",
                chunk_profile_version="news_v2",
                source_class="reported_news",
                ordinal="0001"):
    db.execute("""INSERT OR REPLACE INTO corpus_chunks
        (chunk_id, document_id, chunk_profile_version, section_key,
         ordinal, content_text, content_hash, metadata_hash, source_class,
         available_at, ticker_associations,
         eligibility, manifest_id, status, boundary_kind,
         body_token_start, body_token_end, body_overlap_tokens,
         prefix_token_count, prefix_truncated, section_parse_degraded,
         created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,
                '2026-01-01T00:00:00Z', '["AAPL"]',
                ?, 'm0', ?, 'paragraph',
                0, 10, 0, 5, 0, 0,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (chunk_id, document_id, chunk_profile_version,
         'body', ordinal, 'text', content_hash, metadata_hash, source_class,
         eligibility, status))


def _seed_index(db, chunk_id, document_id, status="embedded",
                content_hash=None, metadata_hash=None):
    db.execute("""INSERT OR REPLACE INTO index_state
        (chunk_id, corpus_item_id, source_kind, status, content_hash,
         metadata_hash, is_tombstone)
        VALUES (?,?,'article',?,?,?,0)""",
        (chunk_id, document_id, status, content_hash, metadata_hash))


def reconciliation_case(name: str) -> ReconciliationCase:
    db = _make_reconciliation_db()

    if name == "new_chunk":
        _seed_index(db, "poly:n1:news_v2:body:0001", "poly:n1", "active", "h1", "mh1")
        return ReconciliationCase(name=name, db=db, next_manifest_id="manifest-new",
            active_chunks={
                "poly:n1:news_v2:body:0001": {
                    "document_id": "poly:n1", "chunk_profile_version": "news_v2",
                    "section_key": "body", "ordinal": "0001",
                    "content_hash": "h1", "metadata_hash": "mh1",
                    "available_at": "2026-01-01T00:00:00Z", "source_class": "reported_news",
                    "dedup_cluster_id": None, "cluster_first_available_at": None,
                    "representative_document_id": None, "eligibility": "eligible"},
                "poly:n2:news_v2:body:0001": {
                    "document_id": "poly:n2", "chunk_profile_version": "news_v2",
                    "section_key": "body", "ordinal": "0001",
                    "content_hash": "h2", "metadata_hash": "mh2",
                    "available_at": "2026-01-01T00:00:00Z", "source_class": "reported_news",
                    "dedup_cluster_id": None, "cluster_first_available_at": None,
                    "representative_document_id": None, "eligibility": "eligible"},
            },
            expected_to_embed=["poly:n2:news_v2:body:0001"])

    elif name == "unchanged_chunk":
        _seed_chunk(db, "poly:u1:news_v2:body:0001", "poly:u1", "h1", "mh1")
        _seed_index(db, "poly:u1:news_v2:body:0001", "poly:u1", "embedded", "h1", "mh1")
        return ReconciliationCase(name=name, db=db, next_manifest_id="manifest-unchanged",
            active_chunks={
                "poly:u1:news_v2:body:0001": {
                    "document_id": "poly:u1", "chunk_profile_version": "news_v2",
                    "section_key": "body", "ordinal": "0001",
                    "content_hash": "h1", "metadata_hash": "mh1",
                    "available_at": "2026-01-01T00:00:00Z", "source_class": "reported_news",
                    "dedup_cluster_id": None, "cluster_first_available_at": None,
                    "representative_document_id": None, "eligibility": "eligible"},
            })

    elif name == "content_change":
        _seed_chunk(db, "poly:c1:news_v2:body:0001", "poly:c1", "oldhash", "mh1")
        _seed_index(db, "poly:c1:news_v2:body:0001", "poly:c1", "embedded", "oldhash", "mh1")
        return ReconciliationCase(name=name, db=db, next_manifest_id="manifest-content",
            active_chunks={
                "poly:c1:news_v2:body:0001": {
                    "document_id": "poly:c1", "chunk_profile_version": "news_v2",
                    "section_key": "body", "ordinal": "0001",
                    "content_hash": "newhash", "metadata_hash": "mh1",
                    "available_at": "2026-01-01T00:00:00Z", "source_class": "reported_news",
                    "dedup_cluster_id": None, "cluster_first_available_at": None,
                    "representative_document_id": None, "eligibility": "eligible"},
            },
            expected_to_embed=["poly:c1:news_v2:body:0001"])

    elif name == "metadata_only_change":
        _seed_chunk(db, "poly:m1:news_v2:body:0001", "poly:m1", "samehash", "oldmeta")
        _seed_index(db, "poly:m1:news_v2:body:0001", "poly:m1", "embedded", "samehash", "oldmeta")
        return ReconciliationCase(name=name, db=db, next_manifest_id="manifest-meta",
            active_chunks={
                "poly:m1:news_v2:body:0001": {
                    "document_id": "poly:m1", "chunk_profile_version": "news_v2",
                    "section_key": "body", "ordinal": "0001",
                    "content_hash": "samehash", "metadata_hash": "newmeta",
                    "available_at": "2026-01-01T00:00:00Z", "source_class": "reported_news",
                    "dedup_cluster_id": None, "cluster_first_available_at": None,
                    "representative_document_id": None, "eligibility": "eligible"},
            },
            expected_metadata_updates=["poly:m1:news_v2:body:0001"])

    elif name == "document_removal":
        _seed_chunk(db, "poly:rem:news_v2:body:0001", "poly:rem", "h1", "mh1")
        _seed_index(db, "poly:rem:news_v2:body:0001", "poly:rem", "embedded", "h1", "mh1")
        return ReconciliationCase(name=name, db=db, next_manifest_id="manifest-rem",
            active_chunks={},
            expected_tombstones=["poly:rem:news_v2:body:0001"])

    elif name == "eligibility_loss":
        _seed_chunk(db, "poly:inel:news_v2:body:0001", "poly:inel", "h1", "mh1")
        _seed_index(db, "poly:inel:news_v2:body:0001", "poly:inel", "embedded", "h1", "mh1")
        return ReconciliationCase(name=name, db=db, next_manifest_id="manifest-inel",
            active_chunks={
                "poly:inel:news_v2:body:0001": {
                    "document_id": "poly:inel", "chunk_profile_version": "news_v2",
                    "section_key": "body", "ordinal": "0001",
                    "content_hash": "h1", "metadata_hash": "mh1",
                    "available_at": "2026-01-01T00:00:00Z", "source_class": "reported_news",
                    "dedup_cluster_id": None, "cluster_first_available_at": None,
                    "representative_document_id": None, "eligibility": "ineligible"},
            },
            expected_tombstones=["poly:inel:news_v2:body:0001"])

    elif name == "profile_version_replacement":
        _seed_chunk(db, "poly:pv:news_v1:body:0001", "poly:pv", "h1", "mh1",
                     chunk_profile_version="news_v1")
        _seed_index(db, "poly:pv:news_v1:body:0001", "poly:pv", "embedded", "h1", "mh1")
        return ReconciliationCase(name=name, db=db, next_manifest_id="manifest-pv",
            active_chunks={
                "poly:pv:news_v2:body:0001": {
                    "document_id": "poly:pv", "chunk_profile_version": "news_v2",
                    "section_key": "body", "ordinal": "0001",
                    "content_hash": "newhash", "metadata_hash": "newmeta",
                    "available_at": "2026-01-01T00:00:00Z", "source_class": "reported_news",
                    "dedup_cluster_id": None, "cluster_first_available_at": None,
                    "representative_document_id": None, "eligibility": "eligible"},
            },
            expected_to_embed=["poly:pv:news_v2:body:0001"],
            expected_tombstones=["poly:pv:news_v1:body:0001"])

    elif name == "disappeared_child":
        _seed_chunk(db, "poly:dc:news_v2:body:0001", "poly:dc", "h1", "mh1")
        _seed_chunk(db, "poly:dc:news_v2:body:0002", "poly:dc", "h2", "mh2", ordinal="0002")
        _seed_index(db, "poly:dc:news_v2:body:0001", "poly:dc", "embedded", "h1", "mh1")
        _seed_index(db, "poly:dc:news_v2:body:0002", "poly:dc", "embedded", "h2", "mh2")
        return ReconciliationCase(name=name, db=db, next_manifest_id="manifest-dc",
            active_chunks={
                "poly:dc:news_v2:body:0001": {
                    "document_id": "poly:dc", "chunk_profile_version": "news_v2",
                    "section_key": "body", "ordinal": "0001",
                    "content_hash": "h1", "metadata_hash": "mh1",
                    "available_at": "2026-01-01T00:00:00Z", "source_class": "reported_news",
                    "dedup_cluster_id": None, "cluster_first_available_at": None,
                    "representative_document_id": None, "eligibility": "eligible"},
            },
            expected_tombstones=["poly:dc:news_v2:body:0002"])

    elif name == "dedup_cluster_reassignment":
        _seed_chunk(db, "poly:dd:news_v2:body:0001", "poly:dd", "h1", "mh1")
        _seed_index(db, "poly:dd:news_v2:body:0001", "poly:dd", "embedded", "h1", "mh1")
        return ReconciliationCase(name=name, db=db, next_manifest_id="manifest-dd",
            active_chunks={
                "poly:dd:news_v2:body:0001": {
                    "document_id": "poly:dd", "chunk_profile_version": "news_v2",
                    "section_key": "body", "ordinal": "0001",
                    "content_hash": "h1", "metadata_hash": "mh1",
                    "available_at": "2026-01-01T00:00:00Z", "source_class": "analysis_opinion",
                    "dedup_cluster_id": "newcluster",
                    "cluster_first_available_at": "2026-01-02T00:00:00Z",
                    "representative_document_id": "poly:rep", "eligibility": "eligible"},
            },
            expected_to_embed=["poly:dd:news_v2:body:0001"],
            expected_tombstones=["poly:dd:news_v2:body:0001"])

    return ReconciliationCase(name=name, db=db, active_chunks={},
                              next_manifest_id="unknown")


# ── Manifest helpers ────────────────────────────────────────────────────────

def inventory_item(chunk_id: str, *, content_hash: str, metadata_hash: str,
                   **overrides) -> dict:
    parts = chunk_id.split(":")
    if len(parts) < 4:
        raise ValueError(f"chunk_id must have >=4 parts: {chunk_id}")
    ordinal = parts[-1]
    section_key = parts[-2]
    chunk_profile_version = parts[-3]
    document_id = ":".join(parts[:-3])
    item = {
        "chunk_id": chunk_id, "document_id": document_id,
        "chunk_profile_version": chunk_profile_version,
        "section_key": section_key, "ordinal": ordinal,
        "content_hash": content_hash, "metadata_hash": metadata_hash,
        "available_at": "2026-01-01T00:00:00Z",
        "source_class": "reported_news",
        "dedup_cluster_id": None, "cluster_first_available_at": None,
        "representative_document_id": None, "eligibility": "eligible",
    }
    item.update(overrides)
    return item


base_params: dict = {
    "normalization_version": "1.0.0",
    "chunk_profile_versions": {"news": "news_v2", "filing": "filing_v3"},
    "source_classifier_version": "1.0.0",
    "certified_snapshot_identity": "snap-abc",
    "tokenizer_revision": TOKENIZER_REVISION,
    "embedding_revision": None,
}
