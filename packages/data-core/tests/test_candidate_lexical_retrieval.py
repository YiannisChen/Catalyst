"""RED/GREEN: pointer-free candidate lexical retrieval over per-build FTS.

Regression coverage for the Q-011 inactive-candidate defect:

- An inactive corpus manifest differs from the active served manifest.
- Its build is ``lexical_ready`` with candidate rows plus per-build FTS rows.
- Candidate-bound lexical retrieval genuinely returns mode_served=fts5,
  is_degraded=false, non-empty results, build-scoped and manifest-bound.
- Wrong build/manifest, current (promoted) manifest, non-ready build, missing
  FTS generation, and count/digest disagreement fail closed.
- Existing active served retrieval (no ``inactive_build_id``) is unchanged.
- Candidate retrieval performs no writes and leaves active state unchanged.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.corpus.streaming_publication import (
    build_candidate_fts,
    ensure_streaming_publication_schema,
)
from catalyst_data.retrieval.fts5 import (
    RetrievalContractError,
    retrieve_lexical,
)
from catalyst_data.storage.sqlite import init_db

ACTIVE_MANIFEST = "f" * 64
ACTIVE_BUILD = "a" * 64
CANDIDATE_MANIFEST = "c" * 64
CANDIDATE_BUILD = "b" * 64
NOW = "2026-01-01T00:00:00Z"
CUTOFF = "2026-02-01T00:00:00Z"

_CHUNK_COLUMNS = (
    "build_id", "chunk_id", "document_id", "chunk_profile_version",
    "section_key", "ordinal", "content_text", "content_hash", "metadata_hash",
    "source_class", "dedup_cluster_id", "cluster_first_available_at",
    "representative_document_id", "available_at", "ticker_associations",
    "eligibility", "status", "boundary_kind", "body_token_start",
    "body_token_end", "body_overlap_tokens", "prefix_token_count",
    "prefix_truncated", "section_parse_degraded", "source_kind", "provider",
    "source_type", "canonical_asset_id", "content_version_id",
    "corpus_document_id", "content_state", "independence_group_id",
    "parse_quality", "created_at", "updated_at",
)


def _hex_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest(conn: sqlite3.Connection, manifest_id: str, *, is_current: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO corpus_manifest "
        "(manifest_id, manifest_json, is_current, created_at) VALUES (?,?,?,?)",
        (manifest_id, json.dumps({"manifest_id": manifest_id}), is_current, NOW),
    )


def _build_row(conn: sqlite3.Connection, build_id: str, manifest_id: str) -> None:
    conn.execute(
        """INSERT INTO corpus_publication_builds (
             build_id, certified_snapshot_identity, header_json, status,
             manifest_id, manifest_json, document_count, chunk_count,
             inventory_digest, created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            build_id, "9" * 64, json.dumps({"manifest_id": manifest_id}),
            "staged", manifest_id, json.dumps({"manifest_id": manifest_id}),
            1, 0, "8" * 64, NOW, NOW,
        ),
    )


def _chunk_row(
    build_id: str,
    chunk_id: str,
    content_text: str,
    *,
    available_at: str = "2026-01-01T09:00:00Z",
) -> dict[str, object]:
    return {
        "build_id": build_id,
        "chunk_id": chunk_id,
        "document_id": "doc:" + chunk_id,
        "chunk_profile_version": "news_v2",
        "section_key": "body",
        "ordinal": "0001",
        "content_text": content_text,
        "content_hash": _hex_digest(content_text),
        "metadata_hash": "0" * 64,
        "source_class": "reported_news",
        "dedup_cluster_id": None,
        "cluster_first_available_at": None,
        "representative_document_id": "doc:" + chunk_id,
        "available_at": available_at,
        "ticker_associations": json.dumps(["AAPL"]),
        "eligibility": "eligible",
        "status": "active",
        "boundary_kind": "document_end",
        "body_token_start": 0,
        "body_token_end": 10,
        "body_overlap_tokens": 0,
        "prefix_token_count": 0,
        "prefix_truncated": 0,
        "section_parse_degraded": 0,
        "source_kind": "news",
        "provider": "fixture",
        "source_type": "fixture_news",
        "canonical_asset_id": None,
        "content_version_id": None,
        "corpus_document_id": "doc:" + chunk_id,
        "content_state": "live",
        "independence_group_id": "group:" + chunk_id,
        "parse_quality": "full",
        "created_at": NOW,
        "updated_at": NOW,
    }


def _insert_chunk(conn: sqlite3.Connection, record: dict[str, object]) -> None:
    columns = ", ".join(_CHUNK_COLUMNS)
    placeholders = ", ".join(":" + name for name in _CHUNK_COLUMNS)
    conn.execute(
        f"INSERT INTO corpus_build_chunks ({columns}) VALUES ({placeholders})",
        record,
    )


def _lexical_state(
    conn: sqlite3.Connection,
    manifest_id: str,
    build_id: str,
) -> None:
    row = conn.execute(
        "SELECT lexical_row_count, lexical_digest "
        "FROM corpus_publication_builds WHERE build_id=?",
        (build_id,),
    ).fetchone()
    assert row is not None
    conn.execute("DELETE FROM lexical_index_state")
    conn.execute(
        """INSERT INTO lexical_index_state (
             singleton_id, schema_version, corpus_manifest_id, mode_served,
             row_count, built_at, lexical_generation_id, lexical_digest
           ) VALUES (1, '1.0.0', ?, 'fts5', ?, ?, ?, ?)""",
        (manifest_id, int(row[0]), NOW, build_id, row[1]),
    )


def _publication_db(path: Path, *, candidate_ready: bool = True) -> sqlite3.Connection:
    """Production-shaped streaming publication DB.

    Active manifest is current with a published per-build FTS generation.
    Candidate manifest is inactive; its build is lexical_ready with per-build
    FTS rows. ``shared-0001`` deliberately exists in BOTH builds with
    different content to prove every join is build-scoped.
    """
    conn = sqlite3.connect(path)
    init_db(conn)
    ensure_streaming_publication_schema(conn)
    _manifest(conn, ACTIVE_MANIFEST, is_current=1)
    _manifest(conn, CANDIDATE_MANIFEST, is_current=0)
    conn.commit()

    _build_row(conn, ACTIVE_BUILD, ACTIVE_MANIFEST)
    _insert_chunk(conn, _chunk_row(ACTIVE_BUILD, "shared-0001", "ACTIVE AAPL earnings report"))
    _insert_chunk(conn, _chunk_row(ACTIVE_BUILD, "act-0002", "AAPL product launch old guidance"))
    conn.commit()
    build_candidate_fts(conn, build_id=ACTIVE_BUILD)
    conn.execute(
        "UPDATE corpus_publication_builds SET status='published', published_at=? "
        "WHERE build_id=?",
        (NOW, ACTIVE_BUILD),
    )
    _lexical_state(conn, ACTIVE_MANIFEST, ACTIVE_BUILD)

    _build_row(conn, CANDIDATE_BUILD, CANDIDATE_MANIFEST)
    _insert_chunk(
        conn,
        _chunk_row(
            CANDIDATE_BUILD,
            "shared-0001",
            "CANDIDATE AAPL earnings record beat expectations",
        ),
    )
    _insert_chunk(
        conn,
        _chunk_row(
            CANDIDATE_BUILD,
            "cand-0002",
            "AAPL surged on record earnings beat",
        ),
    )
    conn.commit()
    if candidate_ready:
        build_candidate_fts(conn, build_id=CANDIDATE_BUILD)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# A + B + C: inactive candidate genuinely served through per-build FTS
# ---------------------------------------------------------------------------

def test_candidate_inactive_manifest_differs_from_active_served_manifest(tmp_path):
    db = _publication_db(tmp_path / "derivative.db")
    current = db.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current=1"
    ).fetchone()[0]
    served = db.execute(
        "SELECT DISTINCT manifest_id FROM corpus_served_chunks"
    ).fetchall()
    assert current == ACTIVE_MANIFEST
    assert [row[0] for row in served] == [ACTIVE_MANIFEST]
    build = db.execute(
        "SELECT status, lexical_ready, lexical_row_count FROM "
        "corpus_publication_builds WHERE build_id=?",
        (CANDIDATE_BUILD,),
    ).fetchone()
    assert build[0] == "lexical_ready"
    assert int(build[1]) == 1
    assert int(build[2]) >= 1
    fts = db.execute(
        "SELECT COUNT(*) FROM corpus_build_chunks_fts WHERE build_id=?",
        (CANDIDATE_BUILD,),
    ).fetchone()[0]
    assert int(fts) >= 1
    db.close()


def test_candidate_bound_lexical_retrieval_serves_fts5_non_degraded(tmp_path):
    db = _publication_db(tmp_path / "derivative.db")
    result = retrieve_lexical(
        db,
        query="AAPL earnings beat",
        ticker="AAPL",
        cutoff=CUTOFF,
        requested_manifest_id=CANDIDATE_MANIFEST,
        inactive_build_id=CANDIDATE_BUILD,
    )
    assert result.mode_served == "fts5"
    assert result.is_degraded is False
    assert result.fallback_reason is None
    assert len(result.results) >= 1
    contents = {item.content_text for item in result.results}
    assert "CANDIDATE AAPL earnings record beat expectations" in contents
    assert "AAPL surged on record earnings beat" in contents
    # Active-only text must never leak into the candidate arm.
    assert not any("ACTIVE" in (text or "") for text in contents)
    db.close()


def test_candidate_build_scoped_join_prevents_cross_build_duplication(tmp_path):
    """Same chunk_id exists in both builds; join must be build-scoped."""
    db = _publication_db(tmp_path / "derivative.db")
    result = retrieve_lexical(
        db,
        query="CANDIDATE expectations",
        ticker="AAPL",
        cutoff=CUTOFF,
        requested_manifest_id=CANDIDATE_MANIFEST,
        inactive_build_id=CANDIDATE_BUILD,
    )
    chunk_ids = [item.chunk_id for item in result.results]
    # No duplicate chunk ids from cross-build join and no ACTIVE content.
    assert len(chunk_ids) == len(set(chunk_ids))
    for item in result.results:
        if item.chunk_id == "shared-0001":
            assert "CANDIDATE" in (item.content_text or "")
    db.close()


# ---------------------------------------------------------------------------
# E: wrong build / wrong manifest / promoted or non-ready build fail closed
# ---------------------------------------------------------------------------

def test_wrong_build_for_manifest_fails_closed(tmp_path):
    db = _publication_db(tmp_path / "derivative.db")
    with pytest.raises(RetrievalContractError):
        retrieve_lexical(
            db, query="AAPL earnings", ticker="AAPL", cutoff=CUTOFF,
            requested_manifest_id=CANDIDATE_MANIFEST,
            inactive_build_id=ACTIVE_BUILD,
        )
    db.close()


def test_wrong_manifest_for_build_fails_closed(tmp_path):
    db = _publication_db(tmp_path / "derivative.db")
    with pytest.raises(RetrievalContractError):
        retrieve_lexical(
            db, query="AAPL earnings", ticker="AAPL", cutoff=CUTOFF,
            requested_manifest_id=ACTIVE_MANIFEST,
            inactive_build_id=CANDIDATE_BUILD,
        )
    db.close()


def test_current_manifest_candidate_build_fails_closed(tmp_path):
    db = _publication_db(tmp_path / "derivative.db")
    # If the candidate manifest were current it would be active-serving state.
    db.execute(
        "UPDATE corpus_manifest SET is_current=0 WHERE is_current=1"
    )
    db.execute(
        "UPDATE corpus_manifest SET is_current=1 WHERE manifest_id=?",
        (CANDIDATE_MANIFEST,),
    )
    db.commit()
    with pytest.raises(RetrievalContractError):
        retrieve_lexical(
            db, query="AAPL earnings", ticker="AAPL", cutoff=CUTOFF,
            requested_manifest_id=CANDIDATE_MANIFEST,
            inactive_build_id=CANDIDATE_BUILD,
        )
    db.close()


def test_non_ready_build_fails_closed(tmp_path):
    db = _publication_db(tmp_path / "derivative.db")
    db.execute(
        "UPDATE corpus_publication_builds SET status='reconciliation_ready', "
        "lexical_ready=0 WHERE build_id=?",
        (CANDIDATE_BUILD,),
    )
    db.commit()
    with pytest.raises(RetrievalContractError):
        retrieve_lexical(
            db, query="AAPL earnings", ticker="AAPL", cutoff=CUTOFF,
            requested_manifest_id=CANDIDATE_MANIFEST,
            inactive_build_id=CANDIDATE_BUILD,
        )
    db.close()


def test_unknown_build_fails_closed(tmp_path):
    db = _publication_db(tmp_path / "derivative.db")
    with pytest.raises(RetrievalContractError):
        retrieve_lexical(
            db, query="AAPL earnings", ticker="AAPL", cutoff=CUTOFF,
            requested_manifest_id=CANDIDATE_MANIFEST,
            inactive_build_id="d" * 64,
        )
    db.close()


def test_missing_fts_generation_fails_closed(tmp_path):
    from catalyst_data.retrieval.fts5 import verify_inactive_lexical_build
    db = _publication_db(tmp_path / "derivative.db")
    # Candidate build claims a lexical generation but its FTS rows are gone.
    db.execute(
        "DELETE FROM corpus_build_chunks_fts WHERE build_id=?",
        (CANDIDATE_BUILD,),
    )
    db.commit()
    with pytest.raises(RetrievalContractError):
        verify_inactive_lexical_build(
            db,
            requested_manifest_id=CANDIDATE_MANIFEST,
            build_id=CANDIDATE_BUILD,
        )
    db.close()


def test_count_disagreement_fails_closed(tmp_path):
    from catalyst_data.retrieval.fts5 import verify_inactive_lexical_build
    db = _publication_db(tmp_path / "derivative.db")
    db.execute(
        "UPDATE corpus_publication_builds SET lexical_row_count=999 "
        "WHERE build_id=?",
        (CANDIDATE_BUILD,),
    )
    db.commit()
    with pytest.raises(RetrievalContractError):
        verify_inactive_lexical_build(
            db,
            requested_manifest_id=CANDIDATE_MANIFEST,
            build_id=CANDIDATE_BUILD,
        )
    db.close()


# ---------------------------------------------------------------------------
# F: active served retrieval remains unchanged
# ---------------------------------------------------------------------------

def test_active_served_retrieval_unchanged_without_build_identity(tmp_path):
    db = _publication_db(tmp_path / "derivative.db")
    result = retrieve_lexical(
        db,
        query="AAPL earnings",
        ticker="AAPL",
        cutoff=CUTOFF,
        requested_manifest_id=ACTIVE_MANIFEST,
    )
    assert result.mode_served == "fts5"
    assert result.is_degraded is False
    assert len(result.results) >= 1
    contents = {item.content_text for item in result.results}
    assert "ACTIVE AAPL earnings report" in contents
    assert not any("CANDIDATE" in (text or "") for text in contents)
    db.close()


# ---------------------------------------------------------------------------
# G: candidate retrieval performs no writes and leaves active state unchanged
# ---------------------------------------------------------------------------

def _active_state(conn: sqlite3.Connection) -> dict[str, object]:
    manifests = conn.execute(
        "SELECT manifest_id, is_current FROM corpus_manifest ORDER BY manifest_id"
    ).fetchall()
    served = conn.execute(
        "SELECT chunk_id, manifest_id FROM corpus_served_chunks ORDER BY chunk_id"
    ).fetchall()
    lexical = conn.execute(
        "SELECT singleton_id, corpus_manifest_id, mode_served, row_count, "
        "lexical_generation_id, lexical_digest FROM lexical_index_state"
    ).fetchall()
    counts = conn.execute(
        "SELECT build_id, COUNT(*) FROM corpus_build_chunks_fts "
        "GROUP BY build_id ORDER BY build_id"
    ).fetchall()
    return {"manifests": manifests, "served": served, "lexical": lexical, "fts": counts}


def test_candidate_retrieval_performs_no_writes(tmp_path):
    db = _publication_db(tmp_path / "derivative.db")
    before = _active_state(db)
    assert db.in_transaction is False
    result = retrieve_lexical(
        db,
        query="AAPL earnings beat",
        ticker="AAPL",
        cutoff=CUTOFF,
        requested_manifest_id=CANDIDATE_MANIFEST,
        inactive_build_id=CANDIDATE_BUILD,
    )
    assert len(result.results) >= 1
    db.commit()  # should be a no-op; no write transaction was opened
    after = _active_state(db)
    assert after == before
    assert db.in_transaction is False
    db.close()
