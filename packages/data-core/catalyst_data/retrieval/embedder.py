"""Corpus_chunks embedder interface (fake vectors only for unit tests)."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Sequence

import numpy as np

from catalyst_data.corpus.streaming_publication import served_chunks_relation

# Production SQL template for the selected corpus generation.
EMBED_ROWS_SQL = """
SELECT chunk_id, content_text, content_hash, metadata_hash, status, manifest_id
FROM {chunks_relation}
WHERE manifest_id = ?
  AND status IN ('pending_embedding', 'embedded', 'metadata_only', 'active')
  {tombstone_clause}
ORDER BY chunk_id
""".strip()


def fetch_embed_rows(conn: sqlite3.Connection, corpus_manifest_id: str) -> list[tuple]:
    chunks_relation = served_chunks_relation(conn)
    tombstone_clause = (
        "AND chunk_id NOT IN (SELECT chunk_id FROM corpus_tombstones)"
        if chunks_relation == "corpus_chunks"
        else ""
    )
    sql = EMBED_ROWS_SQL.format(
        chunks_relation=chunks_relation,
        tombstone_clause=tombstone_clause,
    )
    return list(conn.execute(sql, (corpus_manifest_id,)))


def fake_vector_for_tests(seed: str, dim: int = 1024) -> np.ndarray:
    """Test-only deterministic vectors — never used by source_bundle export."""
    buf = bytearray()
    counter = 0
    while len(buf) < dim * 4:
        buf.extend(
            hashlib.sha256(seed.encode() + counter.to_bytes(4, "big")).digest()
        )
        counter += 1
    raw = np.frombuffer(bytes(buf[: dim * 4]), dtype="<u4").astype(np.float32)
    return raw / np.float32(np.iinfo(np.uint32).max)


def plan_embedding_work(
    rows: Sequence[tuple],
    *,
    prior_content_hashes: dict[str, str] | None = None,
) -> list[str]:
    prior = prior_content_hashes or {}
    to_embed = []
    for chunk_id, _text, content_hash, _mh, _status, _mid in rows:
        if prior.get(chunk_id) != content_hash:
            to_embed.append(chunk_id)
    return to_embed
