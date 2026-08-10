"""T4 served-corpus probe: N/N existence gate against the frozen corpus.

The probe SQL is the manager-approved predicate (status='active',
eligibility='eligible', current manifest, available_at <= cutoff, ticker via
json_each). Expected counts are literal fixtures.

Amendment P5: the T4 gate file is T4_PROBE_TOKEN.txt (never WAVE_TOKEN.txt),
and meta.json carries the full identity/evidence contract.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_eval.post_import.case_pack import SCHEMA_VERSION, CasePackCase
from catalyst_eval.post_import.probe import (
    SCHEMA_VERSION as PROBE_SCHEMA_VERSION,
    run_served_corpus_probe,
    write_probe_evidence,
)

MANIFEST = "a" * 64
GIT_HEAD = "8dd9ee9b5f04e848e3d8248dad6470189af79573"
CODE_REVISION = "bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8"


def _chunk(conn: sqlite3.Connection, *, chunk_id: str, ticker: str, available_at: str,
           status: str = "active", eligibility: str = "eligible",
           manifest_id: str = MANIFEST, source_class: str = "reported_news") -> None:
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
            chunk_id, f"doc:{chunk_id}", "news_v2", "body", "0001",
            "AAPL earnings guidance revenue", "a" * 64, "b" * 64, source_class,
            available_at, json.dumps([ticker]), eligibility, manifest_id, status,
            "paragraph", 0, 10, 0, 0, 0, 0,
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
        ),
    )


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    from catalyst_data.migrations import run_migrations
    from catalyst_data.storage.sqlite import init_db
    init_db(conn)
    run_migrations(conn)
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, ?, 1, '2026-01-01T00:00:00Z')""",
        (MANIFEST, json.dumps({"manifest_id": MANIFEST})),
    )
    conn.commit()
    return conn


def _case(case_id: str, ticker: str, cutoff: str, *, query: str = "q",
          session_date: str = "2025-06-12") -> CasePackCase:
    return CasePackCase(
        schema_version=SCHEMA_VERSION, case_id=case_id, ticker=ticker,
        session_date=session_date, cutoff=cutoff, query=query,
        source_set="fixture", golden={"golden_id": case_id},
    )


def _evidence_kwargs(case_pack_id: str = "d56e0a116ad8488a5d6e3ef88b65f500fa41061aa50eae4e9dca725c88867532"):
    return {
        "db_sha256": "bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40",
        "corpus_manifest_id": MANIFEST,
        "case_pack_id": case_pack_id,
        "case_pack_path": "data/run_reports/post_import/smoke.jsonl",
        "runtime_git_head": GIT_HEAD,
        "index_build_code_revision": CODE_REVISION,
        "snapshot_id": "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
        "source_bundle_id": "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
        "probe_report_id": "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
        "postbuild_readiness_id": "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
        "index_manifest_id": "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083",
        "db_path": "data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db",
        "db_user_version": 13,
        "db_foreign_key_violations": 0,
        "lancedb_dir": "data/lancedb_gold/b6g_8ffae891b4e1",
        "active_table_name": "chunks__staging__b3761f4b943542a8",
        "model_name": "BAAI/bge-m3",
        "model_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "tokenizer_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "dimension": 1024,
        "dtype": "float32",
        "normalization_mode": "l2",
        "embedding_mode": "mock_unit_test",
    }


def test_probe_schema_version_is_literal():
    assert PROBE_SCHEMA_VERSION == "served_corpus_probe_v1"


def test_probe_nn_passes_with_eligible_pre_cutoff_chunks():
    conn = _fresh_db()
    _chunk(conn, chunk_id="c1", ticker="AAPL", available_at="2025-06-01T00:00:00Z")
    _chunk(conn, chunk_id="c2", ticker="NVDA", available_at="2025-10-20T00:00:00Z")
    conn.commit()
    cases = [
        _case("t1", "AAPL", "2025-06-12T20:00:00Z"),
        _case("t2", "NVDA", "2025-10-28T20:00:00Z"),
    ]
    report = run_served_corpus_probe(conn, corpus_manifest_id=MANIFEST, cases=cases)
    assert report.schema_version == "served_corpus_probe_v1"
    assert report.case_count == 2
    assert report.passed_count == 2
    assert report.all_passed is True
    assert {r.case_id: r.count for r in report.per_case} == {"t1": 1, "t2": 1}
    conn.close()


def test_probe_exact_boundary_includes_cutoff_instant():
    conn = _fresh_db()
    _chunk(conn, chunk_id="c1", ticker="AAPL", available_at="2025-06-12T20:00:00Z")
    conn.commit()
    report = run_served_corpus_probe(
        conn, corpus_manifest_id=MANIFEST,
        cases=[_case("t1", "AAPL", "2025-06-12T20:00:00Z")],
    )
    assert report.all_passed is True
    assert report.per_case[0].count == 1
    conn.close()


def test_probe_rejects_post_cutoff_chunk():
    conn = _fresh_db()
    _chunk(conn, chunk_id="c1", ticker="AAPL", available_at="2025-06-13T00:00:00Z")
    conn.commit()
    report = run_served_corpus_probe(
        conn, corpus_manifest_id=MANIFEST,
        cases=[_case("t1", "AAPL", "2025-06-12T20:00:00Z")],
    )
    assert report.all_passed is False
    assert report.per_case[0].count == 0
    conn.close()


def test_probe_fails_closed_on_wrong_ticker():
    conn = _fresh_db()
    _chunk(conn, chunk_id="c1", ticker="MSFT", available_at="2025-06-01T00:00:00Z")
    conn.commit()
    report = run_served_corpus_probe(
        conn, corpus_manifest_id=MANIFEST,
        cases=[_case("t1", "AAPL", "2025-06-12T20:00:00Z")],
    )
    assert report.all_passed is False
    assert report.per_case[0].count == 0
    conn.close()


def test_probe_fails_closed_on_non_active_or_ineligible():
    conn = _fresh_db()
    _chunk(conn, chunk_id="c1", ticker="AAPL", available_at="2025-06-01T00:00:00Z",
           status="metadata_only")
    _chunk(conn, chunk_id="c2", ticker="AAPL", available_at="2025-06-01T00:00:00Z",
           eligibility="ineligible")
    conn.commit()
    report = run_served_corpus_probe(
        conn, corpus_manifest_id=MANIFEST,
        cases=[_case("t1", "AAPL", "2025-06-12T20:00:00Z")],
    )
    assert report.all_passed is False
    assert report.per_case[0].count == 0
    conn.close()


def test_probe_fails_closed_on_wrong_manifest():
    conn = _fresh_db()
    conn.execute(
        """INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, ?, 0, '2026-01-01T00:00:00Z')""",
        ("b" * 64, json.dumps({"manifest_id": "b" * 64})),
    )
    _chunk(conn, chunk_id="c1", ticker="AAPL", available_at="2025-06-01T00:00:00Z",
           manifest_id="b" * 64)
    conn.commit()
    report = run_served_corpus_probe(
        conn, corpus_manifest_id=MANIFEST,
        cases=[_case("t1", "AAPL", "2025-06-12T20:00:00Z")],
    )
    assert report.all_passed is False
    assert report.per_case[0].count == 0
    conn.close()


def test_write_probe_evidence_persists_t4_probe_token_after_nn(tmp_path: Path):
    conn = _fresh_db()
    _chunk(conn, chunk_id="c1", ticker="AAPL", available_at="2025-06-01T00:00:00Z")
    conn.commit()
    cases = [_case("t1", "AAPL", "2025-06-12T20:00:00Z")]
    report = run_served_corpus_probe(conn, corpus_manifest_id=MANIFEST, cases=cases)
    assert report.all_passed
    run_dir = tmp_path / "run"
    write_probe_evidence(report, run_dir=run_dir, **_evidence_kwargs())
    assert (run_dir / "T4_PROBE_TOKEN.txt").read_text().strip() == "T4_PROBE_OK"
    assert not (run_dir / "WAVE_TOKEN.txt").exists()
    probe = json.loads((run_dir / "probe_report.json").read_text())
    assert probe["all_passed"] is True
    assert probe["case_count"] == 1
    assert probe["per_case"][0]["count"] == 1
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["case_pack_id"] == "d56e0a116ad8488a5d6e3ef88b65f500fa41061aa50eae4e9dca725c88867532"
    conn.close()


def test_t4_meta_is_complete(tmp_path: Path):
    conn = _fresh_db()
    _chunk(conn, chunk_id="c1", ticker="AAPL", available_at="2025-06-01T00:00:00Z")
    conn.commit()
    cases = [_case("t1", "AAPL", "2025-06-12T20:00:00Z")]
    report = run_served_corpus_probe(conn, corpus_manifest_id=MANIFEST, cases=cases)
    run_dir = tmp_path / "run"
    write_probe_evidence(report, run_dir=run_dir, **_evidence_kwargs())
    meta = json.loads((run_dir / "meta.json").read_text())
    required = {
        "schema_version", "task", "phase", "started_at", "completed_at",
        "runtime_git_head", "index_build_code_revision",
        "snapshot_id", "corpus_manifest_id", "source_bundle_id",
        "probe_report_id", "postbuild_readiness_id", "index_manifest_id",
        "db_path", "db_sha256", "db_user_version", "db_foreign_key_violations",
        "lancedb_dir", "active_table_name",
        "model_name", "model_revision", "tokenizer_revision",
        "dimension", "dtype", "normalization_mode",
        "embedding_mode", "case_pack_id", "case_pack_path",
        "case_count", "passed_count", "probe_report_path", "probe_report_sha256",
        "nn_result",
    }
    assert required <= set(meta)
    assert meta["runtime_git_head"] == GIT_HEAD
    assert meta["index_build_code_revision"] == CODE_REVISION
    assert meta["active_table_name"] == "chunks__staging__b3761f4b943542a8"
    assert meta["dimension"] == 1024
    conn.close()


def test_t4_meta_identities_match_evidence_inputs(tmp_path: Path):
    conn = _fresh_db()
    _chunk(conn, chunk_id="c1", ticker="AAPL", available_at="2025-06-01T00:00:00Z")
    conn.commit()
    report = run_served_corpus_probe(
        conn, corpus_manifest_id=MANIFEST,
        cases=[_case("t1", "AAPL", "2025-06-12T20:00:00Z")],
    )
    run_dir = tmp_path / "run"
    kwargs = _evidence_kwargs()
    write_probe_evidence(report, run_dir=run_dir, **kwargs)
    meta = json.loads((run_dir / "meta.json").read_text())
    for key in (
        "corpus_manifest_id", "snapshot_id", "source_bundle_id",
        "probe_report_id", "postbuild_readiness_id", "index_manifest_id",
        "model_revision", "tokenizer_revision",
    ):
        assert meta[key] == kwargs[key]
    assert meta["db_sha256"] == kwargs["db_sha256"]
    conn.close()


def test_write_probe_evidence_never_writes_token_when_nn_fails(tmp_path: Path):
    conn = _fresh_db()
    conn.commit()
    cases = [_case("t1", "AAPL", "2025-06-12T20:00:00Z")]
    report = run_served_corpus_probe(conn, corpus_manifest_id=MANIFEST, cases=cases)
    assert report.all_passed is False
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError, match="N/N"):
        write_probe_evidence(report, run_dir=run_dir, **_evidence_kwargs())
    assert not (run_dir / "T4_PROBE_TOKEN.txt").exists()
    assert not (run_dir / "probe_report.json").exists()
    conn.close()


def test_write_probe_evidence_is_atomic_no_tmp(tmp_path: Path):
    conn = _fresh_db()
    _chunk(conn, chunk_id="c1", ticker="AAPL", available_at="2025-06-01T00:00:00Z")
    conn.commit()
    report = run_served_corpus_probe(
        conn, corpus_manifest_id=MANIFEST,
        cases=[_case("t1", "AAPL", "2025-06-12T20:00:00Z")],
    )
    run_dir = tmp_path / "run"
    write_probe_evidence(report, run_dir=run_dir, **_evidence_kwargs())
    assert not list(run_dir.glob("*.tmp"))
    conn.close()
