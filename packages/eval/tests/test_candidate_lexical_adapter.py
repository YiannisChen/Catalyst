"""M8-C contract tests: eval-only candidate-FTS retrieval seam."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.retrieval.fts5 import _update_lexical_digest
from catalyst_eval.v1_1.candidate_lexical_adapter import (
    CandidateLexicalAdapter,
    CandidateLexicalAdapterError,
)

BUILD_ID = "b" * 64
MANIFEST_ID = "c" * 64
NOW = "2026-09-01T00:00:00Z"
AVAILABLE_AT = "2025-05-01T00:00:00Z"

CHUNKS = (
    ("chunk-full-0001", "FULL_TEXT", "apple inc announced material agreement"),
    ("chunk-meta-0001", "METADATA_ONLY", "apple inc announced material agreement"),
)


def _digest(rows) -> str:
    digest = hashlib.sha256()
    for chunk_id, content_text in rows:
        _update_lexical_digest(digest, chunk_id, content_text)
    return digest.hexdigest()


def _fixture(tmp_path: Path, *, corrupt_digest: bool = False) -> Path:
    db = tmp_path / "derivative.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    from catalyst_data.corpus.streaming_publication import (
        ensure_streaming_publication_schema,
    )
    from catalyst_data.storage.sqlite import init_db

    init_db(conn)
    ensure_streaming_publication_schema(conn)
    conn.execute(
        "INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, "
        "created_at) VALUES (?,?,0,?)",
        (MANIFEST_ID, "{}", NOW),
    )
    digest = (
        "f" * 64 if corrupt_digest else _digest([(cid, text) for cid, _, text in CHUNKS])
    )
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status, manifest_id,
            chunk_count, lexical_digest, lexical_row_count, lexical_ready,
            created_at, updated_at)
           VALUES (?,?,?,'in_progress',?,0,'',0,0,?,?)""",
        (BUILD_ID, "7" * 64, "{}", MANIFEST_ID, NOW, NOW),
    )
    for chunk_id, state, text in CHUNKS:
        conn.execute(
            """INSERT INTO corpus_build_chunks (
                   build_id, chunk_id, document_id, chunk_profile_version,
                   section_key, ordinal, content_text, content_hash, metadata_hash,
                   source_class, available_at, ticker_associations, eligibility,
                   status, boundary_kind, body_token_start, body_token_end,
                   body_overlap_tokens, prefix_token_count, prefix_truncated,
                   section_parse_degraded, source_kind, provider, source_type,
                   canonical_asset_id, content_version_id, corpus_document_id,
                   content_state, independence_group_id, parse_quality,
                   created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                BUILD_ID, chunk_id, f"cdoc-{chunk_id}", "filing_v3", "body-0001",
                "0001", text, "a" * 64, "b" * 64, "issuer_disclosure", AVAILABLE_AT,
                json.dumps(["AAPL"]), "eligible", "active", "paragraph", 0, 10, 0, 0,
                0, 0, "filing", "sec", "8-K", f"asset-{chunk_id}",
                f"ver-{chunk_id}", f"cdoc-{chunk_id}", state, None, "full", NOW, NOW,
            ),
        )
        conn.execute(
            "INSERT INTO corpus_build_chunks_fts (build_id, chunk_id, content_text) "
            "VALUES (?,?,?)",
            (BUILD_ID, chunk_id, text),
        )
    conn.execute(
        "UPDATE corpus_publication_builds SET status='lexical_ready', "
        "lexical_ready=1, lexical_row_count=?, chunk_count=?, lexical_digest=? "
        "WHERE build_id=?",
        (len(CHUNKS), len(CHUNKS), digest, BUILD_ID),
    )
    conn.commit()
    conn.close()
    return db


def _snapshot(path: Path) -> str:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    payload = {
        "manifest": [
            list(row) for row in conn.execute(
                "SELECT manifest_id, is_current FROM corpus_manifest ORDER BY 1"
            )
        ],
        "chunks": [
            list(row) for row in conn.execute(
                "SELECT chunk_id, content_state FROM corpus_build_chunks ORDER BY 1"
            )
        ],
    }
    conn.close()
    return json.dumps(payload, sort_keys=True)


def test_adapter_returns_production_evidence_contract(tmp_path):
    db = _fixture(tmp_path)
    adapter = CandidateLexicalAdapter(
        db_path=db, corpus_manifest_id=MANIFEST_ID, build_id=BUILD_ID
    )
    try:
        evidence, observation = adapter.retrieve_with_observations(
            "apple inc material agreement",
            ticker="AAPL",
            cutoff="2025-05-02T20:00:00Z",
        )
        assert evidence, "candidate FTS must return the matching chunks"
        assert observation.served_mode == "fts5"
        assert observation.requested_mode == "lexical"
        assert observation.ordered_final_ranked_evidence_ids == tuple(
            item.chunk_id for item in evidence
        )
        states = {item.chunk_id: item.content_state for item in evidence}
        assert states["chunk-full-0001"] == "FULL_TEXT"
        assert states["chunk-meta-0001"] == "METADATA_ONLY"
        assert adapter.citable_evidence_ids(evidence) == ("chunk-full-0001",)
        assert adapter.retrieve(
            "apple inc material agreement",
            ticker="AAPL",
            cutoff="2025-05-02T20:00:00Z",
        ) == evidence
    finally:
        adapter.close()


def test_adapter_never_touches_pointers_or_sidecars(tmp_path):
    db = _fixture(tmp_path)
    before = _snapshot(db)
    before_bytes = db.read_bytes()
    adapter = CandidateLexicalAdapter(
        db_path=db,
        corpus_manifest_id=MANIFEST_ID,
        build_id=BUILD_ID,
        expected_fts_digest=_digest([(cid, text) for cid, _, text in CHUNKS]),
    )
    try:
        for _ in range(2):
            adapter.retrieve(
                "apple inc material agreement",
                ticker="AAPL",
                cutoff="2025-05-02T20:00:00Z",
            )
    finally:
        adapter.close()
    assert _snapshot(db) == before
    assert db.read_bytes() == before_bytes
    assert not (tmp_path / "derivative.db-wal").exists()
    assert not (tmp_path / "derivative.db-shm").exists()


def test_adapter_fails_closed_on_identity_mismatch(tmp_path):
    db = _fixture(tmp_path)
    with pytest.raises(CandidateLexicalAdapterError, match="digest"):
        CandidateLexicalAdapter(
            db_path=db,
            corpus_manifest_id=MANIFEST_ID,
            build_id=BUILD_ID,
            expected_fts_digest="0" * 64,
        )
    corrupt = _fixture(tmp_path / "corrupt", corrupt_digest=True)
    with pytest.raises(CandidateLexicalAdapterError, match="verification failed"):
        CandidateLexicalAdapter(
            db_path=corrupt, corpus_manifest_id=MANIFEST_ID, build_id=BUILD_ID
        )
