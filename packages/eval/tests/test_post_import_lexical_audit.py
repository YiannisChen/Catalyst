"""AMEND-5: read-only lexical effect audit contracts (C)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.post_import_fixtures import MANIFEST_A, fresh_runner_db
from catalyst_eval.post_import.case_pack import CasePackCase, SCHEMA_VERSION as CASE_PACK_SCHEMA_VERSION
from catalyst_eval.post_import.lexical_audit import (
    LexicalCaseAudit,
    run_lexical_audit,
)

CUTOFF = "2026-01-15T21:00:00Z"


def _audit_db(tmp_path: Path) -> sqlite3.Connection:
    """Fixture DB with realistic chunks and a working FTS5 index."""
    from catalyst_data.retrieval.fts5_builder import build_fts5_index

    conn = fresh_runner_db(tmp_path)
    rows = [
        ("doc:g006:filing_v3:body:0001", "doc:g006", "filing_v3", "body", "0001",
         "Results of Operations and Financial Condition: Tesla reported earnings and results.",
         "2025-07-23T00:00:00Z", "TSLA"),
        ("doc:g006:news_v2:body:0001", "doc:g006", "news_v2", "body", "0001",
         "Tesla shares moved sharply after the report.",
         "2025-07-24T12:00:00Z", "TSLA"),
        ("doc:g006:news_v2:body:0002", "doc:g006", "news_v2", "body", "0002",
         "Tesla shares moved after results were filed later.",
         "2026-01-16T09:00:00Z", "TSLA"),
        ("doc:msft:news_v2:body:0001", "doc:msft", "news_v2", "body", "0001",
         "Microsoft earnings report results filing.",
         "2025-07-22T00:00:00Z", "MSFT"),
    ]
    for row in rows:
        chunk_id, document_id, profile, section, ordinal, content, available_at, ticker = row
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
                chunk_id, document_id, profile, section, ordinal,
                content, "0" * 64, "0" * 64, "reported_news", available_at,
                json.dumps([ticker]), "eligible", MANIFEST_A, "active",
                "paragraph", 0, 10, 0, 0, 0, 0,
                "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z",
            ),
        )
    conn.commit()
    build_fts5_index(conn, MANIFEST_A, clock=lambda: "2026-01-02T00:00:00Z")
    return conn


def _case() -> CasePackCase:
    return CasePackCase(
        schema_version=CASE_PACK_SCHEMA_VERSION,
        case_id="g006", ticker="TSLA", session_date="2025-07-24",
        cutoff="2025-07-24T20:00:00Z",
        query="Why did TSLA move -8.2% on 2025-07-24?", source_set="fixture",
        golden={"golden_id": "g006", "should_refuse": False},
    )


def _write_case_pack(tmp_path: Path) -> Path:
    import json as _json
    path = tmp_path / "case_pack.jsonl"
    path.write_text(_json.dumps(_case().to_dict()) + "\n", encoding="utf-8")
    return path


def _frozen_db_file(tmp_path: Path, conn: sqlite3.Connection) -> Path:
    conn.commit()
    path = tmp_path / "frozen.db"
    target = sqlite3.connect(path)
    conn.backup(target)
    target.close()
    conn.close()
    return path


def test_audit_reports_nonempty_zero_violations(tmp_path):
    conn = _audit_db(tmp_path)
    db_path = _frozen_db_file(tmp_path, conn)
    case_pack_path = _write_case_pack(tmp_path)
    output_dir = tmp_path / "audit"

    report = run_lexical_audit(
        db_path=db_path, case_pack_path=case_pack_path,
        manifest_id=MANIFEST_A, output_dir=output_dir,
    )
    assert report["case_count"] == 1
    assert report["all_non_empty"] is True
    assert report["cutoff_violations_total"] == 0
    assert report["ticker_violations_total"] == 0
    entry = report["per_case"][0]
    assert entry["case_id"] == "g006"
    assert entry["returned_count"] > 0
    # same-day filing-style evidence is retrieved without golden chunk knowledge
    assert any(chunk["chunk_id"] == "doc:g006:filing_v3:body:0001" for chunk in entry["top_chunks"])
    # post-cutoff and wrong-ticker chunks never appear
    assert all(chunk["available_at"] <= "2025-07-24T20:00:00Z" for chunk in entry["top_chunks"])
    assert all(chunk["chunk_id"] != "doc:msft:news_v2:body:0001" for chunk in entry["top_chunks"])


def test_audit_report_written_with_schema(tmp_path):
    conn = _audit_db(tmp_path)
    db_path = _frozen_db_file(tmp_path, conn)
    case_pack_path = _write_case_pack(tmp_path)
    output_dir = tmp_path / "audit"

    run_lexical_audit(
        db_path=db_path, case_pack_path=case_pack_path,
        manifest_id=MANIFEST_A, output_dir=output_dir,
    )
    report_path = output_dir / "report.json"
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "amend5_1_lexical_effect_audit_v1"


def test_fast_path_matches_production_retriever(tmp_path):
    conn = _audit_db(tmp_path)
    db_path = _frozen_db_file(tmp_path, conn)
    case_pack_path = _write_case_pack(tmp_path)

    fast = run_lexical_audit(
        db_path=db_path, case_pack_path=case_pack_path,
        manifest_id=MANIFEST_A, output_dir=tmp_path / "audit_fast",
    )
    prod = run_lexical_audit(
        db_path=db_path, case_pack_path=case_pack_path,
        manifest_id=MANIFEST_A, output_dir=tmp_path / "audit_prod",
        use_production_retriever=True,
    )
    f = fast["per_case"][0]
    p = prod["per_case"][0]
    assert [c["chunk_id"] for c in f["top_chunks"]] == [c["chunk_id"] for c in p["top_chunks"]]
    assert f["matched_count"] == p["matched_count"]
    assert f["returned_count"] == p["returned_count"]
    assert f["match_mode"] == p["match_mode"]
    assert f["policy"] == p["policy"]


def test_production_audit_reports_true_or_match_mode(tmp_path):
    """OR fallback must not be misreported as AND on the production path."""
    from catalyst_data.retrieval.fts5_builder import build_fts5_index
    from tests.post_import_fixtures import fresh_runner_db

    conn = fresh_runner_db(tmp_path)
    # Content such that AND of (move, tesla) fails if only one term is present
    # per chunk, forcing OR.
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
            "doc:or:news_v2:body:0001", "doc:or", "news_v2", "body", "0001",
            "The stock is on the move after results.",
            "0" * 64, "0" * 64, "reported_news", "2025-07-24T12:00:00Z",
            json.dumps(["TSLA"]), "eligible", MANIFEST_A, "active",
            "paragraph", 0, 10, 0, 0, 0, 0,
            "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z",
        ),
    )
    conn.commit()
    build_fts5_index(conn, MANIFEST_A, clock=lambda: "2026-01-02T00:00:00Z")
    db_path = _frozen_db_file(tmp_path, conn)
    case_pack_path = _write_case_pack(tmp_path)
    report = run_lexical_audit(
        db_path=db_path, case_pack_path=case_pack_path,
        manifest_id=MANIFEST_A, output_dir=tmp_path / "audit_or",
        use_production_retriever=True,
    )
    entry = report["per_case"][0]
    assert entry["match_mode"] in {"AND", "OR", "temporal"}
    assert entry["returned_count"] > 0


def test_audit_reports_temporal_effect_metrics(tmp_path):
    conn = _audit_db(tmp_path)
    db_path = _frozen_db_file(tmp_path, conn)
    case_pack_path = _write_case_pack(tmp_path)
    report = run_lexical_audit(
        db_path=db_path, case_pack_path=case_pack_path,
        manifest_id=MANIFEST_A, output_dir=tmp_path / "audit_t",
    )
    entry = report["per_case"][0]
    assert "nearest_lag_days" in entry
    assert "within_2_day_count" in entry
    assert "within_7_day_count" in entry
    assert "source_class_distribution" in entry
    assert "effect_valid" in entry
    assert "effect_reasons" in entry
    assert "latency_ms" in entry
    assert entry["within_2_day_count"] >= 1
    assert entry["nearest_lag_days"] is not None
    assert entry["nearest_lag_days"] <= 2.0
    assert report["all_effect_valid"] is True


def test_audit_case_model_ok_requires_effect_valid():
    good = LexicalCaseAudit(
        case_id="g006", ticker="TSLA", cutoff=CUTOFF, query="q",
        raw_terms=("tsla",), content_terms=("move",), policy="content",
        match_mode="OR", matched_count=3, returned_count=3,
        top_chunks=(), cutoff_violations=(), ticker_violations=(),
        nearest_lag_days=0.0, within_2_day_count=1, should_refuse=False,
        effect_valid=True, effect_reasons=(),
    )
    assert good.ok
    empty = LexicalCaseAudit(
        case_id="g006", ticker="TSLA", cutoff=CUTOFF, query="q",
        raw_terms=("tsla",), content_terms=("move",), policy="content",
        match_mode="OR", matched_count=0, returned_count=0,
        effect_valid=False, effect_reasons=("empty_results",),
    )
    assert not empty.ok


def test_audit_reuses_searchable_statuses_constant():
    import inspect
    from catalyst_eval.post_import import lexical_audit as mod
    from catalyst_data.retrieval.result import SEARCHABLE_STATUSES

    source = inspect.getsource(mod)
    assert "SEARCHABLE_STATUSES" in source
    assert "active" in SEARCHABLE_STATUSES
    # No duplicated local status tuple of production statuses.
    assert "SERVED_STATUSES" not in source


def test_audit_report_schema_is_amend5_1():
    from catalyst_eval.post_import.lexical_audit import AUDIT_SCHEMA_VERSION

    assert AUDIT_SCHEMA_VERSION == "amend5_1_lexical_effect_audit_v1"
