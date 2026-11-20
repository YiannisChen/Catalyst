"""Production-shaped served DB fixtures for the WP-B B7a projection tests.

Builds a current-manifest published build ``BUILD`` (served) plus an older,
non-current published build ``OLD_BUILD`` whose chunk ids overlap the served
build's, so the FTS table holds rows of both builds (the MATCH bridge must
keep only the served build's rows).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

from catalyst_data.corpus.streaming_publication import (
    build_candidate_fts,
    ensure_streaming_publication_schema,
)
from catalyst_data.storage.sqlite import init_db

NOW = "2026-08-01T00:00:00Z"
MANIFEST = "a" * 64
OLD_MANIFEST = "c" * 64
BUILD = "b" * 64
OLD_BUILD = "d" * 64


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk(
    chunk_id: str,
    text: str,
    available_at: str,
    *,
    tickers: Sequence[str] = ("AAPL",),
    document_id: str | None = None,
    source_class: str = "issuer_disclosure",
    profile: str = "filing_v3",
    status: str = "active",
    eligibility: str = "eligible",
    ordinal: str = "0001",
    provider: str = "fixture-provider",
    source_type: str = "filing",
    provider_updated_at: str | None = None,
) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id or f"doc-{chunk_id}",
        "text": text,
        "available_at": available_at,
        "tickers": list(tickers),
        "source_class": source_class,
        "profile": profile,
        "status": status,
        "eligibility": eligibility,
        "ordinal": ordinal,
        "provider": provider,
        "source_type": source_type,
        "provider_updated_at": provider_updated_at,
    }


def _insert_build(
    conn: sqlite3.Connection, *, build_id: str, manifest_id: str, chunks: Iterable[dict]
) -> None:
    rows = list(chunks)
    conn.execute(
        """INSERT INTO corpus_publication_builds (
             build_id, certified_snapshot_identity, header_json, status,
             manifest_id, manifest_json, document_count, chunk_count,
             inventory_digest, created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (build_id, "9" * 64, json.dumps({"manifest_id": manifest_id}), "staged",
         manifest_id, json.dumps({"manifest_id": manifest_id}), len(rows), len(rows),
         "8" * 64, NOW, NOW),
    )
    for spec in rows:
        conn.execute(
            """INSERT INTO corpus_build_chunks (
                 build_id, chunk_id, document_id, chunk_profile_version,
                 section_key, ordinal, content_text, content_hash,
                 metadata_hash, source_class, dedup_cluster_id,
                 cluster_first_available_at, representative_document_id,
                 available_at, ticker_associations, provider_updated_at,
                 eligibility, status,
                 boundary_kind, body_token_start, body_token_end,
                 body_overlap_tokens, prefix_token_count, prefix_truncated,
                 section_parse_degraded, source_kind, provider, source_type,
                 created_at, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (build_id, spec["chunk_id"], spec["document_id"], spec["profile"],
             "item_2_02", spec["ordinal"], spec["text"], _sha(spec["text"]),
             _sha("{}"), spec["source_class"], f"cluster-{spec['chunk_id']}",
             spec["available_at"], spec["document_id"], spec["available_at"],
             json.dumps(spec["tickers"]), spec["provider_updated_at"],
             spec["eligibility"], spec["status"],
             "section", 0, 10, 0, 0, 0, 0, "filing", spec["provider"], spec["source_type"],
             NOW, NOW),
        )
    conn.commit()
    build_candidate_fts(conn, build_id=build_id)
    conn.execute(
        "UPDATE corpus_publication_builds SET status='published', lexical_ready=1, "
        "published_at=? WHERE build_id=?",
        (NOW, build_id),
    )
    conn.commit()


def make_served_db(
    path: Path,
    chunks: Sequence[dict],
    *,
    old_chunks: Sequence[dict] | None = None,
) -> Path:
    """Create the served DB and point ``lexical_index_state`` at ``BUILD``."""
    conn = sqlite3.connect(path)
    try:
        init_db(conn)
        ensure_streaming_publication_schema(conn)
        if old_chunks:
            conn.execute(
                "INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, "
                "created_at) VALUES (?, ?, 0, ?)",
                (OLD_MANIFEST, json.dumps({"manifest_id": OLD_MANIFEST}), NOW),
            )
            _insert_build(conn, build_id=OLD_BUILD, manifest_id=OLD_MANIFEST, chunks=old_chunks)
        conn.execute(
            "INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, "
            "created_at) VALUES (?, ?, 1, ?)",
            (MANIFEST, json.dumps({"manifest_id": MANIFEST}), NOW),
        )
        _insert_build(conn, build_id=BUILD, manifest_id=MANIFEST, chunks=chunks)
        digest, row_count = conn.execute(
            "SELECT lexical_digest, lexical_row_count FROM corpus_publication_builds "
            "WHERE build_id=?",
            (BUILD,),
        ).fetchone()
        conn.execute("DELETE FROM lexical_index_state")
        conn.execute(
            """INSERT INTO lexical_index_state (
                 singleton_id, schema_version, corpus_manifest_id, mode_served,
                 row_count, built_at, lexical_generation_id, lexical_digest
               ) VALUES (1, '1.0.0', ?, 'fts5', ?, ?, ?, ?)""",
            (MANIFEST, int(row_count), NOW, BUILD, digest),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def rich_chunks() -> list[dict]:
    """A varied served build: dates, tickers, terms, classes, statuses, ties."""
    words = ["earnings", "guidance", "tariff", "dividend", "revenue", "buyback"]
    out: list[dict] = []
    days = [
        "2026-07-10", "2026-07-20", "2026-07-26", "2026-07-29", "2026-07-30",
        "2026-07-31", "2026-08-01", "2026-08-02", "2026-08-03", "2026-08-05",
        "2026-08-08", "2026-08-12", "2026-08-20", "2026-09-10",
    ]
    classes = ["issuer_disclosure", "reported_news", "corporate_press_release"]
    n = 0
    for d_index, day in enumerate(days):
        for k in range(3):
            n += 1
            terms = " ".join(words[(d_index + j + k) % len(words)] for j in range(k + 1))
            tickers = ("AAPL",) if k != 2 else ("AAPL", "MSFT")
            if d_index % 5 == 4 and k == 1:
                tickers = ("MSFT",)
            status = "active"
            eligibility = "eligible"
            if n % 11 == 0:
                status = "retired"
            if n % 13 == 0:
                eligibility = "ineligible"
            out.append(
                chunk(
                    f"chunk-{n:03d}",
                    f"AAPL filing text {terms} quarter results",
                    f"{day}T{12 + k:02d}:00:00Z",
                    tickers=tickers,
                    document_id=f"doc-{d_index:02d}",
                    ordinal=f"{k + 1:04d}",
                    source_class=classes[(n + k) % len(classes)],
                    profile="news_v2" if k == 2 else "filing_v3",
                    status=status,
                    eligibility=eligibility,
                )
            )
    return out


def old_build_chunks() -> list[dict]:
    """A non-current build that reuses served chunk ids with other text."""
    return [
        chunk("chunk-001", "AAPL earnings guidance tariff old build", "2026-08-01T12:00:00Z"),
        chunk("chunk-020", "AAPL earnings guidance tariff old build", "2026-08-01T12:00:00Z"),
        chunk("old-only", "AAPL earnings guidance tariff old build", "2026-08-01T12:00:00Z"),
    ]
