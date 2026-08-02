"""sec_source_ready vs sec_evidence_ready — Pre-B6 behavioral tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from catalyst_data.sec.readiness import (
    SecReadinessError,
    evaluate_sec_readiness,
    load_frozen_filing_inventory,
    mandatory_document_ids_from_inventory,
)
from catalyst_data.sec.convergence_identity import compute_convergence_plan_hash
from catalyst_data.sec.inventory import build_filing_inventory_manifest


def _ratified_inventory_args():
    from catalyst_data.manifests.universe import load_universe_spec

    spec = load_universe_spec(
        Path(__file__).parents[1]
        / "catalyst_data"
        / "manifests"
        / "universe_v1_2025_08.spec.json"
    )
    return spec, {
        "tickers": list(spec.tickers),
        "issuer_class_by_ticker": {
            ticker: spec.companies[ticker]["issuer_class"]
            for ticker in spec.tickers
        },
    }


def _complete_periodic_entries():
    spec, _ = _ratified_inventory_args()
    entries = []
    for ticker in spec.tickers:
        forms = (
            ["20-F"]
            if spec.companies[ticker]["issuer_class"] == "foreign_private_issuer"
            else ["10-Q", "10-K"]
        )
        for form in forms:
            entries.append(
                {
                    "ticker": ticker,
                    "accession_number": f"{ticker}-{form}",
                    "filed_at": "2025-08-15",
                    "form_type": form,
                    "documents": [],
                }
            )
    return entries


def _boot(tmp_path: Path) -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.migrations import run_migrations

    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    run_migrations(conn)
    return conn


def _seed_s4_lineage(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    run_id: str = "s4-run1",
    success: bool = True,
    endpoint: str = "sec_document",
    with_attempt: bool = True,
    request_id: str | None = None,
    with_checkpoint: bool = True,
    inventory_id: str = "c" * 64,
    document_url: str | None = None,
    document_file: str = "a.htm",
    document_role: str = "primary_doc",
) -> str:
    """Seed full S4 provenance chain for a mandatory document."""
    import hashlib

    fid = f"f-{doc_id[:8]}"
    rid = request_id or hashlib.sha256(f"req:{doc_id}".encode()).hexdigest()
    raw_id = f"raw:{rid}"
    lfid = hashlib.sha256(f"lf:{doc_id}".encode()).hexdigest()
    cell_id = hashlib.sha256(f"cell:{doc_id}".encode()).hexdigest()
    long_text = ("substantive filing body text for readiness gates " * 8).strip()
    inv_id = inventory_id
    doc_url = document_url or f"http://{doc_id}"
    conn.execute(
        """INSERT OR IGNORE INTO ingestion_runs
           (run_id, started_at, ticker_list_json, source_list_json, status,
            plan_hash, expected_plan_hash, allow_stale_ohlcv, allow_stale_ohlcv_overridden,
            cancel_requested)
           VALUES (?, '2026-01-01T00:00:00Z', '[]', '[]', 'PLANNED', ?, ?, 0, 0, 0)""",
        (run_id, "p" * 64, "p" * 64),
    )
    # advance status after insert under v2 contract
    conn.execute(
        "UPDATE ingestion_runs SET status='SUCCEEDED' WHERE run_id=?",
        (run_id,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO filings (filing_id, cik, ticker, form_type, filed_at, accession_number, url) "
        "VALUES (?,?,?,?,?,?,?)",
        (fid, "1", "AAPL", "8-K", "2025-08-01", "a", "http://x"),
    )
    resp_sha = hashlib.sha256(b"x").hexdigest()
    conn.execute(
        """INSERT OR IGNORE INTO raw_assets
           (asset_id, ticker, source_type, reference_date, fetched_at, data_version,
            content_raw, response_sha256, request_id, page_no, content_encoding)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            raw_id,
            "AAPL",
            "sec_filings",
            "2025-08-01",
            "2025-08-01T00:00:00Z",
            "v2",
            b"x",
            resp_sha,
            rid,
            1,
            "identity",
        ),
    )
    if with_attempt:
        params = json.dumps(
            {
                "document_id": doc_id,
                "inventory_id": inv_id,
                "document_role": document_role,
                "document_file": document_file,
                "document_url": doc_url,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            """INSERT OR IGNORE INTO provider_request_attempts (
                request_id, run_id, logical_fetch_id, source_type, provider,
                endpoint_name, ticker_or_series, window_start, window_end,
                attempt_no, page_no, request_fingerprint, started_at, status,
                request_params_redacted
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rid,
                run_id,
                lfid,
                "sec_filings",
                "sec",
                endpoint,
                "AAPL",
                "2025-08-01",
                "2025-08-01",
                1,
                1,
                "a" * 64,
                "2025-08-01T00:00:00Z",
                "STARTED",
                params,
            ),
        )
        conn.execute(
            """UPDATE provider_request_attempts SET status=?, raw_asset_id=?, completed_at=?
               WHERE request_id=?""",
            ("SUCCEEDED", raw_id, "2025-08-01T00:00:01Z", rid),
        )
        if with_checkpoint and success:
            ck = hashlib.sha256(f"ck:{run_id}:{cell_id}".encode()).hexdigest()[:32]
            conn.execute(
                """INSERT OR IGNORE INTO source_checkpoints
                   (checkpoint_id, run_id, source_type, ticker, date, status,
                    logical_fetch_id, request_count, is_complete, raw_asset_id,
                    cell_id, window_start, window_end, endpoint_name, provider_profile_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ck, run_id, "sec_filings", "AAPL", "2025-08-01", "success",
                    lfid, 1, 1, raw_id, cell_id, "2025-08-01", "2025-08-01",
                    endpoint, "v1",
                ),
            )
    conn.execute(
        """INSERT OR REPLACE INTO filing_documents
           (filing_id, document_url, document_type, text, extraction_status, document_id)
           VALUES (?,?,?,?,?,?)""",
        (
            fid,
            f"http://{doc_id}",
            "primary_doc",
            long_text if success else "",
            "success" if success else "empty",
            doc_id,
        ),
    )
    if success:
        conn.execute(
            """INSERT OR IGNORE INTO normalized_provenance
               (entity_type, entity_id, entity_version, raw_asset_id, normalizer_version, created_at)
               VALUES ('filing',?,?,?,'v1','2026-01-01T00:00:00Z')""",
            (doc_id, "e" * 64, raw_id),
        )
    conn.commit()
    return run_id


def _add_chunks(conn, doc_id: str, n: int):
    conn.execute(
        """INSERT OR IGNORE INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, '{}', 1, '2026-01-01T00:00:00Z')""",
        ("m" * 64,),
    )
    for i in range(n):
        conn.execute(
            """INSERT INTO corpus_chunks (
                chunk_id, document_id, chunk_profile_version, section_key, ordinal,
                content_text, content_hash, metadata_hash, source_class,
                available_at, ticker_associations, eligibility, manifest_id, status,
                boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
                prefix_token_count, prefix_truncated, section_parse_degraded,
                created_at, updated_at
            ) VALUES (?,?, 'filing_v3', 's', ?,
                'body', ?, ?, 'official_government',
                '2025-08-01T00:00:00Z', '["AAPL"]', 'eligible', ?, 'pending_embedding',
                'document_end', 0, 1, 0, 0, 0, 0,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            (
                f"{doc_id[:8]}_{i:04d}",
                doc_id,
                f"{i:04d}",
                f"{i:064x}"[:64],
                f"{(i+10):064x}"[:64],
                "m" * 64,
            ),
        )
    conn.commit()


def test_empty_mandatory_inventory_cannot_be_source_ready(tmp_path: Path):
    conn = _boot(tmp_path)
    rep = evaluate_sec_readiness(conn, mandatory_document_ids=[])
    assert rep.sec_source_ready is False
    assert rep.sec_evidence_ready is False
    conn.close()


def test_sec_source_ready_true_with_zero_corpus_chunks(tmp_path: Path):
    conn = _boot(tmp_path)
    a, b = "a" * 64, "b" * 64
    run = _seed_s4_lineage(conn, a)
    _seed_s4_lineage(conn, b, run_id=run)
    rep = evaluate_sec_readiness(
        conn, mandatory_document_ids=[a, b], s4_lineage_run_ids=[run],
        submissions_complete=True,
        index_complete=True,
        enforce_checkpoint_oracle=False,
    )
    assert rep.sec_source_ready is True
    assert rep.sec_evidence_ready is False
    assert rep.chunked_count == 0
    conn.close()


def test_sec_evidence_ready_false_with_zero_corpus_chunks(tmp_path: Path):
    conn = _boot(tmp_path)
    a = "a" * 64
    run = _seed_s4_lineage(conn, a)
    rep = evaluate_sec_readiness(
        conn, mandatory_document_ids=[a], s4_lineage_run_ids=[run],
        submissions_complete=True,
        index_complete=True,
    )
    assert rep.sec_evidence_ready is False
    conn.close()


def test_provenance_raw_without_request_attempt_fails(tmp_path: Path):
    conn = _boot(tmp_path)
    a = "a" * 64
    _seed_s4_lineage(conn, a, with_attempt=False)
    rep = evaluate_sec_readiness(
        conn, mandatory_document_ids=[a], s4_lineage_run_ids=["s4-run1"],
        submissions_complete=True,
        index_complete=True,
    )
    assert rep.sec_source_ready is False
    assert a in rep.failed_document_ids
    conn.close()


def test_provenance_wrong_endpoint_fails(tmp_path: Path):
    conn = _boot(tmp_path)
    a = "a" * 64
    run = _seed_s4_lineage(conn, a, endpoint="sec_submissions")
    rep = evaluate_sec_readiness(
        conn, mandatory_document_ids=[a], s4_lineage_run_ids=[run],
        submissions_complete=True,
        index_complete=True,
    )
    assert rep.sec_source_ready is False
    conn.close()


def test_provenance_wrong_run_lineage_fails(tmp_path: Path):
    conn = _boot(tmp_path)
    a = "a" * 64
    _seed_s4_lineage(conn, a, run_id="s4-run-other")
    rep = evaluate_sec_readiness(
        conn, mandatory_document_ids=[a], s4_lineage_run_ids=["s4-run-expected"],
        submissions_complete=True,
        index_complete=True,
    )
    assert rep.sec_source_ready is False
    conn.close()


def test_provenance_exact_s4_lineage_succeeds(tmp_path: Path):
    conn = _boot(tmp_path)
    a = "a" * 64
    run = _seed_s4_lineage(conn, a, run_id="s4-exact")
    rep = evaluate_sec_readiness(
        conn, mandatory_document_ids=[a], s4_lineage_run_ids=[run],
        submissions_complete=True,
        index_complete=True,
        enforce_checkpoint_oracle=False,
    )
    assert rep.sec_source_ready is True
    conn.close()


def test_omitted_inventory_artifact_rejected_by_pre_b6_audit(tmp_path: Path):
    from catalyst_data.coverage_audit import run_b2o_readiness_audit

    conn = _boot(tmp_path)
    conn.execute(
        """INSERT INTO ingestion_runs
           (run_id, started_at, ticker_list_json, source_list_json, status,
            plan_hash, expected_plan_hash, allow_stale_ohlcv, allow_stale_ohlcv_overridden,
            cancel_requested)
           VALUES ('t1','2026-01-01T00:00:00Z','[]','[]','PLANNED',?,?,0,0,0)""",
        ("p" * 64, "p" * 64),
    )
    conn.execute("UPDATE ingestion_runs SET status='SUCCEEDED' WHERE run_id='t1'")
    conn.commit()
    conn.close()
    db = tmp_path / "t.db"
    with pytest.raises(SecReadinessError, match="inventory"):
        run_b2o_readiness_audit(
            str(db),
            universe_manifest={"tickers": ["AAPL"], "runtime_manifest_id": "u" * 64},
            terminal_run_id="t1",
            pre_b6_sec=True,
            filing_inventory_path=str(tmp_path / "missing_inventory.json"),
            s1_terminal_run_id="s1",
            s2_terminal_run_id="s2",
            s4_terminal_run_id="s4",
        )


def test_inventory_id_mismatch_rejected(tmp_path: Path):
    _, universe_args = _ratified_inventory_args()
    inv = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="a" * 64,
        filing_entries=_complete_periodic_entries(),
        **universe_args,
    )
    path = tmp_path / "inv.json"
    path.write_text(json.dumps(inv))
    with pytest.raises(SecReadinessError, match="inventory_id"):
        load_frozen_filing_inventory(path, expected_inventory_id="0" * 64)


def test_legacy_b2o_inventory_path_does_not_attach_sec_readiness(tmp_path: Path):
    from catalyst_data.coverage_audit import run_b2o_readiness_audit

    doc_id = "d" * 64
    _, universe_args = _ratified_inventory_args()
    inv = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="a" * 64,
        filing_entries=[
            *_complete_periodic_entries(),
            {
                "ticker": "AAPL",
                "accession_number": "acc",
                "filed_at": "2025-08-15",
                "form_type": "8-K",
                "documents": [
                    {
                        "document_id": doc_id,
                        "requiredness": "mandatory",
                        "document_role": "primary_doc",
                        "is_primary": True,
                        "document_file": "a.htm",
                        "document_url": "http://x",
                    }
                ],
            }
        ],
        **universe_args,
    )
    event_entry = next(
        entry
        for entry in inv["sorted_filing_entries"]
        if entry["accession_number"] == "acc"
    )
    event_entry["documents"][0]["document_id"] = doc_id
    from catalyst_data.sec.inventory import compute_inventory_id

    inv["inventory_id"] = compute_inventory_id(inv)
    inv_path = tmp_path / "inv.json"
    inv_path.write_text(json.dumps(inv))

    conn = _boot(tmp_path)
    run = _seed_s4_lineage(
        conn,
        doc_id,
        inventory_id=inv["inventory_id"],
        document_url="http://x",
        document_file="a.htm",
        document_role="primary_doc",
    )
    conn.commit()
    conn.close()
    report = run_b2o_readiness_audit(
        str(tmp_path / "t.db"),
        universe_manifest={"tickers": ["AAPL"], "runtime_manifest_id": "a" * 64},
        terminal_run_id=run,
        filing_inventory_path=str(inv_path),
        pre_b6_sec=False,
        s4_lineage_run_ids=[run],
        submissions_complete=True,
        index_complete=True,
    )
    assert "sec_readiness" not in report
    assert report.get("sec_source_ready") is None
    assert report["b2o_readiness"]["overall_readiness"]["status"] == "complete"


def test_zero_mandatory_cannot_pass_snapshot_readiness(tmp_path: Path):
    from catalyst_data.manifests.snapshot import build_data_snapshot_manifest
    from catalyst_data.manifests.universe import SourceWindows

    conn = _boot(tmp_path)
    conv = compute_convergence_plan_hash(
        baseline_snapshot_id="b" * 64,
        universe_manifest_id="u" * 64,
        s1_plan_hash="1" * 64,
        s2_plan_hash="2" * 64,
        inventory_id="i" * 64,
        s4_plan_hash="4" * 64,
        reconciliation_evidence_hash="r" * 64,
    )
    windows = SourceWindows(
        degraded_start="2024-01-01",
        degraded_end="2025-07-31",
        canonical_start="2025-08-01",
        canonical_end="2026-07-23",
    )
    with pytest.raises(ValueError, match="sec_source_ready"):
        build_data_snapshot_manifest(
            conn,
            universe_manifest_id="u" * 64,
            plan_hash=conv,
            protected_source_sha256="p" * 64,
            source_windows=windows,
            created_at=datetime.now(timezone.utc),
            coverage_states={
                "overall_readiness": {"status": "complete"},
                "comparable_gate": {"status": "complete"},
                "sec_source_ready": False,
            },
            convergence_plan_hash=conv,
        )
    conn.close()


def test_data_snapshot_requires_convergence_plan_hash(tmp_path: Path):
    from catalyst_data.manifests.snapshot import build_data_snapshot_manifest
    from catalyst_data.manifests.universe import SourceWindows

    conn = _boot(tmp_path)
    windows = SourceWindows(
        degraded_start="2024-01-01",
        degraded_end="2025-07-31",
        canonical_start="2025-08-01",
        canonical_end="2026-07-23",
    )
    with pytest.raises(TypeError):
        build_data_snapshot_manifest(  # type: ignore[call-arg]
            conn,
            universe_manifest_id="u" * 64,
            plan_hash="1" * 64,
            protected_source_sha256="p" * 64,
            source_windows=windows,
            created_at=datetime.now(timezone.utc),
            coverage_states={
                "overall_readiness": {"status": "complete"},
                "comparable_gate": {"status": "complete"},
                "sec_source_ready": True,
            },
        )
    conn.close()


def test_data_snapshot_rejects_stage_hash_masquerade(tmp_path: Path):
    from catalyst_data.manifests.snapshot import build_data_snapshot_manifest
    from catalyst_data.manifests.universe import SourceWindows

    conn = _boot(tmp_path)
    conv = compute_convergence_plan_hash(
        baseline_snapshot_id="b" * 64,
        universe_manifest_id="u" * 64,
        s1_plan_hash="1" * 64,
        s2_plan_hash="2" * 64,
        inventory_id="i" * 64,
        s4_plan_hash="4" * 64,
        reconciliation_evidence_hash="r" * 64,
    )
    windows = SourceWindows(
        degraded_start="2024-01-01",
        degraded_end="2025-07-31",
        canonical_start="2025-08-01",
        canonical_end="2026-07-23",
    )
    with pytest.raises(ValueError, match="convergence_plan_hash|stage"):
        build_data_snapshot_manifest(
            conn,
            universe_manifest_id="u" * 64,
            plan_hash="1" * 64,
            protected_source_sha256="p" * 64,
            source_windows=windows,
            created_at=datetime.now(timezone.utc),
            coverage_states={
                "overall_readiness": {"status": "complete"},
                "comparable_gate": {"status": "complete"},
                "sec_source_ready": True,
                "s1_plan_hash": "1" * 64,
            },
            convergence_plan_hash=conv,
        )
    man = build_data_snapshot_manifest(
        conn,
        universe_manifest_id="u" * 64,
        plan_hash=conv,
        protected_source_sha256="p" * 64,
        source_windows=windows,
        created_at=datetime.now(timezone.utc),
        coverage_states={
            "overall_readiness": {"status": "complete"},
            "comparable_gate": {"status": "complete"},
            "sec_source_ready": True,
            "s1_plan_hash": "1" * 64,
        },
        convergence_plan_hash=conv,
    )
    assert man.plan_hash == conv
    conn.close()


def test_convergence_plan_hash_binds_all_components():
    base = dict(
        baseline_snapshot_id="b" * 64,
        universe_manifest_id="u" * 64,
        s1_plan_hash="1" * 64,
        s2_plan_hash="2" * 64,
        inventory_id="i" * 64,
        s4_plan_hash="4" * 64,
        reconciliation_evidence_hash="r" * 64,
    )
    h0 = compute_convergence_plan_hash(**base)
    for key in base:
        mutated = dict(base)
        mutated[key] = "f" * 64
        assert compute_convergence_plan_hash(**mutated) != h0


def test_sec_evidence_ready_false_when_doc_b_has_zero_chunks_doc_a_has_ten(tmp_path: Path):
    conn = _boot(tmp_path)
    a, b = "a" * 64, "b" * 64
    run = _seed_s4_lineage(conn, a)
    _seed_s4_lineage(conn, b, run_id=run)
    _add_chunks(conn, a, 10)
    rep = evaluate_sec_readiness(
        conn, mandatory_document_ids=[a, b], s4_lineage_run_ids=[run],
        submissions_complete=True,
        index_complete=True,
        enforce_checkpoint_oracle=False,
    )
    assert rep.sec_source_ready is True
    assert rep.sec_evidence_ready is False
    assert rep.chunked_count == 1
    conn.close()


def test_aggregate_chunk_count_does_not_pass_missing_document(tmp_path: Path):
    conn = _boot(tmp_path)
    a, b = "a" * 64, "b" * 64
    run = _seed_s4_lineage(conn, a)
    _seed_s4_lineage(conn, b, run_id=run)
    _add_chunks(conn, a, 100)
    rep = evaluate_sec_readiness(
        conn, mandatory_document_ids=[a, b], s4_lineage_run_ids=[run],
        submissions_complete=True,
        index_complete=True,
    )
    assert rep.sec_evidence_ready is False
    conn.close()


def test_report_keys_include_sec_source_ready_and_sec_evidence_ready(tmp_path: Path):
    conn = _boot(tmp_path)
    rep = evaluate_sec_readiness(conn, mandatory_document_ids=[])
    assert hasattr(rep, "sec_source_ready")
    assert hasattr(rep, "sec_evidence_ready")
    conn.close()


def test_optional_degraded_excluded_from_mandatory_denominator(tmp_path: Path):
    conn = _boot(tmp_path)
    a = "a" * 64
    run = _seed_s4_lineage(conn, a)
    _add_chunks(conn, a, 1)
    rep = evaluate_sec_readiness(
        conn,
        mandatory_document_ids=[a],
        optional_degraded_ids=["opt" * 16],
        s4_lineage_run_ids=[run],
    )
    assert "opt" * 16 not in rep.missing_document_ids
    assert rep.optional_degraded_ids
    conn.close()


def test_missing_carry_in_slot_blocks_source_ready(tmp_path: Path):
    conn = _boot(tmp_path)
    a = "a" * 64
    run = _seed_s4_lineage(conn, a)
    rep = evaluate_sec_readiness(
        conn,
        mandatory_document_ids=[a],
        missing_carry_in_slots=["10-Q"],
        s4_lineage_run_ids=[run],
    )
    assert rep.sec_source_ready is False
    conn.close()


def test_mandatory_ids_from_inventory_helper():
    inv = {
        "sorted_filing_entries": [
            {
                "documents": [
                    {"document_id": "a" * 64, "requiredness": "mandatory"},
                    {"document_id": "b" * 64, "requiredness": "optional_degraded"},
                ]
            }
        ]
    }
    m, o = mandatory_document_ids_from_inventory(inv)
    assert m == ["a" * 64]
    assert o == ["b" * 64]
