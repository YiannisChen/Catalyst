"""M8 contract tests: end-to-end generation coverage audit."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.canonical.generation_coverage import (
    CoverageState,
    audit_generation_coverage,
)
from catalyst_data.canonical.registry import create_canonical_registry
from catalyst_data.corpus.streaming_publication import (
    ensure_streaming_publication_schema,
)
from catalyst_data.storage.sqlite import init_db

CANDIDATE_BUILD = "b" * 64
CANDIDATE_MANIFEST = "c" * 64
SERVED_BUILD = "d" * 64
SERVED_MANIFEST = "e" * 64
NOW = "2026-09-18T00:00:00Z"

CANONICAL = {
    "doc-full": "FULL_TEXT",
    "doc-meta": "METADATA_ONLY",
    "doc-canonical-only": "FULL_TEXT",
    "doc-candidate": "FULL_TEXT",
    "doc-served-no-fts": "FULL_TEXT",
    "doc-served-fts": "FULL_TEXT",
}


def _fixture(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "derivative.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    create_canonical_registry(conn)
    ensure_streaming_publication_schema(conn)
    for asset_id, state in CANONICAL.items():
        conn.execute(
            """INSERT INTO canonical_assets (
                   asset_id, asset_type, issuer_id, tickers_json, provider,
                   publisher, canonical_url, source_class, source_published_at, eligible_at,
                   eligible_at_reason, temporal_precision, accepted_time_recovered,
                   fail_closed, ingested_at, content_state, serving_status, title,
                   content_ref, dedup_cluster_id, independence_group_id,
                   parse_quality, subtype_metadata, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                asset_id, "FILING", "issuer", "[]", "sec", "sec",
                f"https://www.sec.gov/{asset_id}", "issuer_disclosure", NOW, NOW,
                "accepted_time",
                "accepted_time", 1, 0, NOW, state, "body_candidate", asset_id,
                asset_id, None, None, "full", "{}", NOW, NOW,
            ),
        )
    for manifest_id, is_current in ((CANDIDATE_MANIFEST, 0), (SERVED_MANIFEST, 1)):
        conn.execute(
            "INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, "
            "created_at) VALUES (?,?,?,?)",
            (manifest_id, "{}", is_current, NOW),
        )
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status, manifest_id,
            chunk_count, lexical_digest, lexical_row_count, lexical_ready,
            created_at, updated_at)
           VALUES (?,?,?,'in_progress',?,?,?,0,0,?,?)""",
        (CANDIDATE_BUILD, "7" * 64, "{}", CANDIDATE_MANIFEST, 0, "1" * 64, NOW, NOW),
    )
    conn.execute(
        """INSERT INTO corpus_publication_builds
           (build_id, certified_snapshot_identity, header_json, status, manifest_id,
            chunk_count, lexical_digest, lexical_row_count, lexical_ready,
            created_at, updated_at)
           VALUES (?,?,?,'in_progress',?,?,?,2,0,?,?)""",
        (SERVED_BUILD, "7" * 64, "{}", SERVED_MANIFEST, 2, "2" * 64, NOW, NOW),
    )
    conn.commit()

    def _chunk(build_id: str, asset_id: str, index: int) -> str:
        chunk_id = f"{asset_id}-{index}".replace(".", "_")
        conn.execute(
            """INSERT INTO corpus_build_chunks (
                   build_id, chunk_id, document_id, chunk_profile_version,
                   section_key, ordinal, content_text, content_hash, metadata_hash,
                   source_class, dedup_cluster_id, cluster_first_available_at,
                   representative_document_id, available_at, ticker_associations,
                   eligibility, status, boundary_kind, body_token_start,
                   body_token_end, body_overlap_tokens, prefix_token_count,
                   prefix_truncated, section_parse_degraded, source_kind, provider,
                   source_type, canonical_asset_id, content_version_id,
                   corpus_document_id, content_state, independence_group_id,
                   parse_quality, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                build_id, chunk_id, f"cdoc-{asset_id}", "filing_v3", "body-0001",
                "0001", f"body text for {asset_id}", "a" * 64, "b" * 64,
                "issuer_disclosure", None, None, f"cdoc-{asset_id}", NOW, "[]",
                "eligible", "active", "paragraph", 0, 10, 0, 0, 0, 0, "filing",
                "sec", "8-K", asset_id, f"cv-{asset_id}", f"cdoc-{asset_id}",
                "FULL_TEXT", None, "full", NOW, NOW,
            ),
        )
        return chunk_id

    candidate_chunks = [
        ("doc-full", 1),
        ("doc-candidate", 1),
        ("doc-canonical-only", 1),
    ]
    # doc-canonical-only must be absent from the build entirely.
    candidate_chunks = [item for item in candidate_chunks if item[0] != "doc-canonical-only"]
    for asset_id, index in candidate_chunks:
        _chunk(CANDIDATE_BUILD, asset_id, index)
    served_chunks = [
        _chunk(SERVED_BUILD, "doc-served-no-fts", 1),
        _chunk(SERVED_BUILD, "doc-served-fts", 1),
    ]
    conn.execute(
        "UPDATE corpus_publication_builds SET status='lexical_ready', lexical_ready=1, "
        "lexical_row_count=2, chunk_count=2 WHERE build_id=?",
        (CANDIDATE_BUILD,),
    )
    conn.execute(
        "UPDATE corpus_publication_builds SET status='published', lexical_ready=1, "
        "lexical_row_count=2, chunk_count=2 WHERE build_id=?",
        (SERVED_BUILD,),
    )
    conn.execute(
        "INSERT INTO corpus_build_chunks_fts (build_id, chunk_id, content_text) "
        "VALUES (?,?,?)",
        (CANDIDATE_BUILD, "doc-full-1", "body text for doc-full"),
    )
    conn.execute(
        "INSERT INTO corpus_build_chunks_fts (build_id, chunk_id, content_text) "
        "VALUES (?,?,?)",
        (SERVED_BUILD, served_chunks[1], "body text for doc-served-fts"),
    )
    conn.commit()
    return conn


REQUESTED = (
    "doc-absent",
    "doc-meta",
    "doc-canonical-only",
    "doc-full",
    "doc-candidate",
    "doc-served-no-fts",
    "doc-served-fts",
)


def test_coverage_classifies_every_generation_boundary(tmp_path):
    conn = _fixture(tmp_path)
    try:
        report = audit_generation_coverage(
            conn,
            corpus_manifest_id=CANDIDATE_MANIFEST,
            build_id=CANDIDATE_BUILD,
            document_ids=REQUESTED,
        )
    finally:
        conn.close()
    assert report.rows["doc-full"].state == CoverageState.CANDIDATE_FTS_READY
    assert report.rows["doc-meta"].state == CoverageState.NOT_FULL_TEXT
    assert report.rows["doc-canonical-only"].state == CoverageState.OMITTED_FROM_BUILD
    assert report.rows["doc-served-no-fts"].state == CoverageState.SERVED_NOT_FTS_INDEXED
    assert report.rows["doc-served-fts"].state == CoverageState.SERVED_FTS_READY
    assert report.rows["doc-candidate"].state == CoverageState.CANDIDATE_NOT_FTS_INDEXED
    assert report.rows["doc-absent"].state == CoverageState.ABSENT_FROM_CANONICAL
    assert report.counts["CANDIDATE_FTS_READY"] == 1
    assert report.candidate_filing_chunk_count == 2


def test_expected_evidence_ids_never_change_selection_or_identity(tmp_path):
    conn = _fixture(tmp_path)
    try:
        baseline = audit_generation_coverage(
            conn,
            corpus_manifest_id=CANDIDATE_MANIFEST,
            build_id=CANDIDATE_BUILD,
            document_ids=REQUESTED,
        )
        audited = audit_generation_coverage(
            conn,
            corpus_manifest_id=CANDIDATE_MANIFEST,
            build_id=CANDIDATE_BUILD,
            document_ids=REQUESTED,
            expected_evidence_ids=("doc-full", "doc-absent"),
        )
    finally:
        conn.close()
    assert audited.report_digest == baseline.report_digest
    assert audited.rows == baseline.rows
    assert audited.counts == baseline.counts
    assert audited.expected_evidence == {
        "doc-full": "CANDIDATE_FTS_READY",
        "doc-absent": "ABSENT_FROM_CANONICAL",
    }


def test_cli_publishes_canonical_digest_without_absolute_paths(tmp_path):
    import importlib.util

    conn = _fixture(tmp_path)
    conn.close()
    script = Path(__file__).resolve().parents[1] / "scripts" / "audit_v1_1_generation_coverage.py"
    spec = importlib.util.spec_from_file_location("audit_v1_1_generation_coverage", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    output = tmp_path / "coverage.json"
    rc = module.main(
        [
            "--derivative", str(tmp_path / "derivative.db"),
            "--corpus-manifest-id", CANDIDATE_MANIFEST,
            "--build-id", CANDIDATE_BUILD,
            "--output", str(output),
        ]
    )
    assert rc == 0
    raw = output.read_bytes()
    payload = json.loads(raw)
    assert raw == json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert str(tmp_path) not in raw.decode("utf-8")
    assert payload["rows"]["doc-full"]["state"] == "CANDIDATE_FTS_READY"
