"""Round-3 Pre-B6 readiness identity contract tests (RED→GREEN)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from catalyst_data.sec.inventory import (
    build_filing_inventory_manifest,
    compute_inventory_id,
)
from catalyst_data.sec.readiness import (
    SecReadinessError,
    evaluate_sec_readiness,
    load_and_verify_convergence_evidence,
    load_frozen_filing_inventory,
    resolve_run_lineage,
    verify_mandatory_document_checkpoint,
)
from catalyst_data.sec.convergence_identity import compute_convergence_plan_hash
from catalyst_data.coverage_audit import run_pre_b6_sec_readiness_audit, run_b2o_readiness_audit


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _boot(tmp: Path) -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.migrations import run_migrations

    conn = sqlite3.connect(tmp / "t.db")
    init_db(conn)
    run_migrations(conn)
    return conn


def _seed_run(conn, run_id: str, plan_hash: str):
    conn.execute(
        """INSERT OR IGNORE INTO ingestion_runs
           (run_id, started_at, ticker_list_json, source_list_json, status,
            plan_hash, expected_plan_hash, allow_stale_ohlcv, allow_stale_ohlcv_overridden,
            cancel_requested)
           VALUES (?, '2026-01-01T00:00:00Z', '[]', '[]', 'PLANNED', ?, ?, 0, 0, 0)""",
        (run_id, plan_hash, plan_hash),
    )
    conn.execute(
        "UPDATE ingestion_runs SET status='SUCCEEDED' WHERE run_id=?", (run_id,)
    )


def _seed_checkpoint(
    conn,
    *,
    run_id: str,
    cell_id: str,
    endpoint: str,
    ticker: str,
    status: str = "success",
    is_complete: int = 1,
    request_count: int = 1,
    lfid: str | None = None,
    raw_asset_id: str | None = None,
):
    lfid = lfid or _h(f"lf:{cell_id}")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(source_checkpoints)").fetchall()}
    if "checkpoint_id" in cols:
        conn.execute(
            """INSERT INTO source_checkpoints
               (checkpoint_id, run_id, source_type, ticker, date, status,
                logical_fetch_id, request_count, is_complete, raw_asset_id,
                cell_id, window_start, window_end, endpoint_name, provider_profile_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _h(f"ck:{run_id}:{cell_id}")[:32],
                run_id,
                "sec_filings",
                ticker,
                "2025-08-01",
                status,
                lfid,
                request_count,
                is_complete,
                raw_asset_id,
                cell_id,
                "2025-08-01",
                "2025-08-01",
                endpoint,
                "v1",
            ),
        )
    else:
        conn.execute(
            """INSERT INTO source_checkpoints
               (run_id, source_type, ticker, date, status, request_count)
               VALUES (?,?,?,?,?,?)""",
            (run_id, "sec_filings", ticker, "2025-08-01", status, request_count),
        )
    return lfid


def _seed_s4_doc(
    conn,
    *,
    doc_id: str,
    run_id: str,
    inventory_id: str,
    request_count: int = 1,
    endpoint: str = "sec_document",
    wrong_lineage_run: str | None = None,
):
    rid = _h(f"req:{doc_id}")
    raw_id = f"raw:{rid}"
    lfid = _h(f"lf:{doc_id}")
    cell_id = _h(f"cell:{doc_id}")
    use_run = wrong_lineage_run or run_id
    long_text = ("substantive filing body text for readiness gates " * 8).strip()
    conn.execute(
        "INSERT OR IGNORE INTO filings (filing_id,cik,ticker,form_type,filed_at,accession_number,url) "
        "VALUES (?,?,?,?,?,?,?)",
        (f"f-{doc_id[:8]}", "1", "AAPL", "8-K", "2025-08-01", "acc", "http://x"),
    )
    resp = _h("body")
    conn.execute(
        """INSERT OR IGNORE INTO raw_assets
           (asset_id,ticker,source_type,reference_date,fetched_at,data_version,
            content_raw,response_sha256,request_id,page_no,content_encoding)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            raw_id, "AAPL", "sec_filings", "2025-08-01", "2025-08-01T00:00:00Z", "v2",
            b"x", resp, rid, 1, "identity",
        ),
    )
    params = json.dumps(
        {
            "document_id": doc_id,
            "inventory_id": inventory_id,
            "document_role": "primary_doc",
            "document_file": "a.htm",
            "document_url": "http://x/a.htm",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        """INSERT OR IGNORE INTO provider_request_attempts
           (request_id,run_id,logical_fetch_id,source_type,provider,endpoint_name,
            ticker_or_series,window_start,window_end,attempt_no,page_no,
            request_fingerprint,started_at,status,request_params_redacted)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rid, use_run, lfid, "sec_filings", "sec", endpoint, "AAPL",
            "2025-08-01", "2025-08-01", 1, 1, "a" * 64, "t", "STARTED", params,
        ),
    )
    conn.execute(
        """UPDATE provider_request_attempts SET status='SUCCEEDED', raw_asset_id=?,
           response_sha256=?, completed_at=?
           WHERE request_id=?""",
        (raw_id, resp, "2025-08-01T00:00:01Z", rid),
    )
    # additional attempts (same lfid) for request_count alignment
    for i in range(2, request_count + 1):
        rid_i = _h(f"req:{doc_id}:{i}")
        conn.execute(
            """INSERT INTO provider_request_attempts
               (request_id,run_id,logical_fetch_id,source_type,provider,endpoint_name,
                ticker_or_series,window_start,window_end,attempt_no,page_no,
                request_fingerprint,started_at,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rid_i, use_run, lfid, "sec_filings", "sec", endpoint, "AAPL",
                "2025-08-01", "2025-08-01", i, 1, "c" * 64, "t", "STARTED",
            ),
        )
        conn.execute(
            """UPDATE provider_request_attempts SET status='HTTP_ERROR',
               completed_at=?, http_status=500 WHERE request_id=?""",
            ("2025-08-01T00:00:02Z", rid_i),
        )
    conn.execute(
        """INSERT OR REPLACE INTO filing_documents
           (filing_id,document_url,document_type,text,extraction_status,document_id)
           VALUES (?,?,?,?,?,?)""",
        (f"f-{doc_id[:8]}", "http://x/a.htm", "primary_doc", long_text, "success", doc_id),
    )
    conn.execute(
        """INSERT OR IGNORE INTO normalized_provenance
           (entity_type,entity_id,entity_version,raw_asset_id,normalizer_version,created_at)
           VALUES ('filing',?,?,?,'v1','t')""",
        (doc_id, "e" * 64, raw_id),
    )
    _seed_checkpoint(
        conn,
        run_id=use_run,
        cell_id=cell_id,
        endpoint=endpoint,
        ticker="AAPL",
        request_count=request_count,
        lfid=lfid,
        raw_asset_id=raw_id,
    )
    conn.commit()
    return cell_id, lfid, raw_id


def _seed_canonical_s4_doc(
    conn,
    *,
    run_id: str,
    inventory_id: str,
    wrong_lineage_run: str | None = None,
):
    from catalyst_data.sec.document_cells import build_document_cell
    from catalyst_data.sec.materialize import materialize_sec_document

    use_run = wrong_lineage_run or run_id
    cell = build_document_cell(
        ticker="AAPL",
        filed_date="2025-08-01",
        inventory_id=inventory_id,
        accession_number="0000320193-25-000001",
        document_role="primary_doc",
        document_file="aapl.htm",
        document_url="https://www.sec.gov/aapl.htm",
        filing_id="filing-aapl-2025-08-01",
        requiredness="mandatory",
        requiredness_reason="primary",
    )
    result = materialize_sec_document(
        conn,
        run_id=use_run,
        cell=cell,
        fetch=lambda _url: (
            200,
            (
                "<html><body><p>"
                + "substantive filing body text for readiness gates " * 20
                + "</p></body></html>"
            ).encode(),
            "text/html",
        ),
    )
    assert result.status == "success"
    _seed_checkpoint(
        conn,
        run_id=use_run,
        cell_id=cell["cell_id"],
        endpoint="sec_document",
        ticker="AAPL",
        request_count=result.request_count,
        lfid=result.logical_fetch_id,
        raw_asset_id=result.raw_asset_id,
    )
    conn.commit()
    ext = cell["identity_extensions"]
    descriptor = {
        **ext,
        "ticker": cell["subject"],
        "filed_date": cell["window_start"],
        "provider_profile_version": cell["provider_profile_version"],
    }
    return cell, descriptor, result


def _allow_test_corruption(conn) -> None:
    """Disable immutability guards only in a disposable DB corruption test."""
    for trigger in (
        "trg_request_attempt_identity_guard",
        "trg_request_attempt_transition_guard",
        "trg_b2o_checkpoint_cell_identity_update",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _40_tickers():
    return [f"T{i:02d}" for i in range(40)]


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


def _complete_periodic_entries(*, omit: str | None = None):
    spec, _ = _ratified_inventory_args()
    entries = []
    for ticker in spec.tickers:
        issuer_class = spec.companies[ticker]["issuer_class"]
        forms = ["20-F"] if issuer_class == "foreign_private_issuer" else ["10-Q", "10-K"]
        for form in forms:
            slot = f"{ticker}:{form}"
            if slot == omit:
                continue
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


def _write_inventory(tmp: Path, *, docs: list[dict] | None = None) -> Path:
    entries = _complete_periodic_entries()
    if docs:
        entries.append(
            {
                "ticker": "AAPL",
                "accession_number": "acc",
                "filed_at": "2025-08-15",
                "form_type": "8-K",
                "documents": docs,
            }
        )
    _, universe_args = _ratified_inventory_args()
    inv = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="u" * 64,
        filing_entries=entries,
        **universe_args,
    )
    path = tmp / "inv.json"
    path.write_text(json.dumps(inv))
    return path


def _write_evidence(tmp: Path, **overrides) -> Path:
    # all identity hashes must be lowercase hex
    base = dict(
        baseline_snapshot_id="b" * 64,
        universe_manifest_id="a" * 64,
        s1_plan_hash="1" * 64,
        s2_plan_hash="2" * 64,
        inventory_id="c" * 64,
        s4_plan_hash="4" * 64,
        reconciliation_evidence_hash="d" * 64,
        db_user_version=13,
        readiness_policy_version="b2e_readiness_v1",
    )
    base.update(overrides)
    h = compute_convergence_plan_hash(
        baseline_snapshot_id=base["baseline_snapshot_id"],
        universe_manifest_id=base["universe_manifest_id"],
        s1_plan_hash=base["s1_plan_hash"],
        s2_plan_hash=base["s2_plan_hash"],
        inventory_id=base["inventory_id"],
        s4_plan_hash=base["s4_plan_hash"],
        reconciliation_evidence_hash=base["reconciliation_evidence_hash"],
        db_user_version=int(base["db_user_version"]),
        readiness_policy_version=str(base["readiness_policy_version"]),
    )
    base["convergence_plan_hash"] = h
    path = tmp / "conv.json"
    path.write_text(json.dumps(base))
    return path, h, base


def test_pre_b6_audit_without_s1_s2_s4_identities_rejects(tmp_path: Path):
    inv = _write_inventory(tmp_path)
    ev, _, _ = _write_evidence(tmp_path, inventory_id=json.loads(inv.read_text())["inventory_id"])
    # rewrite evidence inventory to match
    inv_body = json.loads(inv.read_text())
    ev, _, _ = _write_evidence(tmp_path, inventory_id=inv_body["inventory_id"])
    with pytest.raises(SecReadinessError, match="s1/s2/s4|terminal"):
        run_b2o_readiness_audit(
            str(tmp_path / "missing.db"),
            universe_manifest={"tickers": _40_tickers(), "runtime_manifest_id": "u" * 64},
            terminal_run_id="x",
            pre_b6_sec=True,
            filing_inventory_path=str(inv),
            convergence_evidence_path=str(ev),
            # missing s1/s2/s4
        )


def test_s1_incomplete_blocks_source_readiness(tmp_path: Path):
    conn = _boot(tmp_path)
    s1h, s2h, s4h = "1" * 64, "2" * 64, "4" * 64
    _seed_run(conn, "s1", s1h)
    _seed_run(conn, "s2", s2h)
    _seed_run(conn, "s4", s4h)
    tickers = _40_tickers()
    # only seed 39
    for t in tickers[:39]:
        _seed_checkpoint(
            conn, run_id="s1", cell_id=_h(f"s1:{t}"), endpoint="sec_submissions", ticker=t
        )
    inv_id = "i" * 64
    doc = "a" * 64
    _seed_s4_doc(conn, doc_id=doc, run_id="s4", inventory_id=inv_id)
    index_cell = _h("idx1")
    _seed_checkpoint(
        conn, run_id="s2", cell_id=index_cell, endpoint="sec_filing_index", ticker="AAPL"
    )
    conn.commit()
    rep = evaluate_sec_readiness(
        conn,
        mandatory_document_ids=[doc],
        s1_lineage_run_ids=["s1"],
        s2_lineage_run_ids=["s2"],
        s4_lineage_run_ids=["s4"],
        s1_tickers=tickers,
        s2_index_cell_ids=[index_cell],
        inventory_id=inv_id,
        inventory={"inventory_id": inv_id, "missing_carry_in_slots": [], "sorted_filing_entries": []},
    )
    assert rep.s1_complete is False
    assert rep.sec_source_ready is False
    conn.close()


def test_s2_incomplete_blocks_source_readiness(tmp_path: Path):
    conn = _boot(tmp_path)
    s1h, s2h, s4h = "1" * 64, "2" * 64, "4" * 64
    _seed_run(conn, "s1", s1h)
    _seed_run(conn, "s2", s2h)
    _seed_run(conn, "s4", s4h)
    tickers = _40_tickers()
    for t in tickers:
        _seed_checkpoint(
            conn, run_id="s1", cell_id=_h(f"s1:{t}"), endpoint="sec_submissions", ticker=t
        )
    inv_id = "i" * 64
    doc = "a" * 64
    _seed_s4_doc(conn, doc_id=doc, run_id="s4", inventory_id=inv_id)
    # missing index cells
    rep = evaluate_sec_readiness(
        conn,
        mandatory_document_ids=[doc],
        s1_lineage_run_ids=["s1"],
        s2_lineage_run_ids=["s2"],
        s4_lineage_run_ids=["s4"],
        s1_tickers=tickers,
        s2_index_cell_ids=[_h("missing-index")],
        inventory_id=inv_id,
        inventory={"inventory_id": inv_id, "missing_carry_in_slots": [], "sorted_filing_entries": []},
    )
    assert rep.s2_complete is False
    assert rep.sec_source_ready is False
    conn.close()


def test_mandatory_doc_missing_checkpoint_blocks(tmp_path: Path):
    conn = _boot(tmp_path)
    inv_id = "c" * 64
    _seed_run(conn, "s4", "4" * 64)
    cell, descriptor, _ = _seed_canonical_s4_doc(
        conn, run_id="s4", inventory_id=inv_id
    )
    doc = cell["identity_extensions"]["document_id"]
    conn.execute("DELETE FROM source_checkpoints")
    conn.commit()
    ok, reason = verify_mandatory_document_checkpoint(
        conn, document_id=doc, s4_lineage_run_ids=["s4"], inventory_id=inv_id,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert reason == "missing_checkpoint"
    conn.close()


def test_checkpoint_request_count_mismatch_blocks(tmp_path: Path):
    conn = _boot(tmp_path)
    inv_id = "i" * 64
    _seed_run(conn, "s4", "4" * 64)
    cell, descriptor, _ = _seed_canonical_s4_doc(
        conn, run_id="s4", inventory_id=inv_id
    )
    doc = cell["identity_extensions"]["document_id"]
    # corrupt request_count on checkpoint
    conn.execute("UPDATE source_checkpoints SET request_count=99 WHERE endpoint_name='sec_document'")
    conn.commit()
    ok, reason = verify_mandatory_document_checkpoint(
        conn, document_id=doc, s4_lineage_run_ids=["s4"], inventory_id=inv_id,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert reason == "request_count_mismatch"
    conn.close()


def test_duplicate_successful_s4_attempt_blocks(tmp_path: Path):
    conn = _boot(tmp_path)
    _seed_run(conn, "s4", "4" * 64)
    cell, descriptor, result = _seed_canonical_s4_doc(
        conn, run_id="s4", inventory_id="c" * 64
    )
    original_request_id, response_sha256 = conn.execute(
        """SELECT request_id, response_sha256
           FROM provider_request_attempts
           WHERE logical_fetch_id=? AND status='SUCCEEDED'""",
        (result.logical_fetch_id,),
    ).fetchone()
    duplicate_request_id = _h("duplicate-success")
    conn.execute(
        """INSERT INTO provider_request_attempts (
               request_id, run_id, logical_fetch_id, source_type, provider,
               endpoint_name, ticker_or_series, window_start, window_end,
               attempt_no, page_no, request_fingerprint,
               request_params_redacted, started_at, status
           )
           SELECT ?, run_id, logical_fetch_id, source_type, provider,
                  endpoint_name, ticker_or_series, window_start, window_end,
                  2, page_no, ?, request_params_redacted, started_at, 'STARTED'
           FROM provider_request_attempts WHERE request_id=?""",
        (duplicate_request_id, "f" * 64, original_request_id),
    )
    conn.execute(
        """UPDATE provider_request_attempts
           SET status='SUCCEEDED', raw_asset_id=?, response_sha256=?,
               completed_at='2025-08-01T00:00:02Z'
           WHERE request_id=?""",
        (result.raw_asset_id, response_sha256, duplicate_request_id),
    )
    ok, reason = verify_mandatory_document_checkpoint(
        conn,
        document_id=cell["identity_extensions"]["document_id"],
        s4_lineage_run_ids=["s4"],
        inventory_id="c" * 64,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert reason == "duplicate_or_mismatched_successful_attempt"
    conn.close()


def test_checkpoint_from_wrong_s4_lineage_blocks(tmp_path: Path):
    conn = _boot(tmp_path)
    inv_id = "i" * 64
    _seed_run(conn, "s4-good", "4" * 64)
    _seed_run(conn, "s4-bad", "9" * 64)
    cell, descriptor, _ = _seed_canonical_s4_doc(
        conn,
        run_id="s4-good",
        inventory_id=inv_id,
        wrong_lineage_run="s4-bad",
    )
    doc = cell["identity_extensions"]["document_id"]
    ok, reason = verify_mandatory_document_checkpoint(
        conn, document_id=doc, s4_lineage_run_ids=["s4-good"], inventory_id=inv_id,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert reason == "wrong_s4_lineage"
    conn.close()


def test_wrong_logical_fetch_document_identity_blocks(tmp_path: Path):
    conn = _boot(tmp_path)
    inv_id = "c" * 64
    _seed_run(conn, "s4", "4" * 64)
    cell, descriptor, result = _seed_canonical_s4_doc(
        conn, run_id="s4", inventory_id=inv_id
    )
    doc = cell["identity_extensions"]["document_id"]
    params = json.loads(
        conn.execute(
            "SELECT request_params_redacted FROM provider_request_attempts "
            "WHERE logical_fetch_id=?",
            (result.logical_fetch_id,),
        ).fetchone()[0]
    )
    params["document_id"] = "f" * 64
    _allow_test_corruption(conn)
    conn.execute(
        "UPDATE provider_request_attempts SET request_params_redacted=? "
        "WHERE logical_fetch_id=?",
        (json.dumps(params), result.logical_fetch_id),
    )
    conn.commit()
    ok, reason = verify_mandatory_document_checkpoint(
        conn, document_id=doc, s4_lineage_run_ids=["s4"], inventory_id=inv_id,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert "identity" in (reason or "")
    conn.close()


def test_missing_request_params_redacted_blocks(tmp_path: Path):
    conn = _boot(tmp_path)
    _seed_run(conn, "s4", "4" * 64)
    cell, descriptor, result = _seed_canonical_s4_doc(
        conn, run_id="s4", inventory_id="c" * 64
    )
    _allow_test_corruption(conn)
    conn.execute(
        "UPDATE provider_request_attempts SET request_params_redacted='[]' "
        "WHERE logical_fetch_id=?",
        (result.logical_fetch_id,),
    )
    ok, reason = verify_mandatory_document_checkpoint(
        conn,
        document_id=cell["identity_extensions"]["document_id"],
        s4_lineage_run_ids=["s4"],
        inventory_id="c" * 64,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert reason == "invalid_request_params_redacted"


@pytest.mark.parametrize(
    "missing_key",
    [
        "document_id",
        "inventory_id",
        "accession_number",
        "document_role",
        "document_file",
        "document_url",
        "filing_id",
        "identity_schema_version",
    ],
)
def test_missing_frozen_request_identity_key_blocks(
    tmp_path: Path, missing_key: str
):
    conn = _boot(tmp_path)
    _seed_run(conn, "s4", "4" * 64)
    cell, descriptor, result = _seed_canonical_s4_doc(
        conn, run_id="s4", inventory_id="c" * 64
    )
    params = json.loads(
        conn.execute(
            "SELECT request_params_redacted FROM provider_request_attempts "
            "WHERE logical_fetch_id=?",
            (result.logical_fetch_id,),
        ).fetchone()[0]
    )
    params.pop(missing_key)
    _allow_test_corruption(conn)
    conn.execute(
        "UPDATE provider_request_attempts SET request_params_redacted=? "
        "WHERE logical_fetch_id=?",
        (json.dumps(params), result.logical_fetch_id),
    )
    ok, reason = verify_mandatory_document_checkpoint(
        conn,
        document_id=cell["identity_extensions"]["document_id"],
        s4_lineage_run_ids=["s4"],
        inventory_id="c" * 64,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert reason == "missing_request_identity_keys"


def test_raw_attempt_response_sha_mismatch_blocks(tmp_path: Path):
    conn = _boot(tmp_path)
    _seed_run(conn, "s4", "4" * 64)
    cell, descriptor, result = _seed_canonical_s4_doc(
        conn, run_id="s4", inventory_id="c" * 64
    )
    _allow_test_corruption(conn)
    conn.execute(
        "UPDATE provider_request_attempts SET response_sha256=? "
        "WHERE logical_fetch_id=?",
        ("f" * 64, result.logical_fetch_id),
    )
    ok, reason = verify_mandatory_document_checkpoint(
        conn,
        document_id=cell["identity_extensions"]["document_id"],
        s4_lineage_run_ids=["s4"],
        inventory_id="c" * 64,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert reason == "response_sha256_mismatch"


@pytest.mark.parametrize("target", ["attempt", "checkpoint"])
def test_empty_attempt_or_checkpoint_raw_asset_id_blocks(
    tmp_path: Path, target: str
):
    conn = _boot(tmp_path)
    _seed_run(conn, "s4", "4" * 64)
    cell, descriptor, result = _seed_canonical_s4_doc(
        conn, run_id="s4", inventory_id="c" * 64
    )
    table = (
        "provider_request_attempts" if target == "attempt" else "source_checkpoints"
    )
    _allow_test_corruption(conn)
    conn.execute(
        f"UPDATE {table} SET raw_asset_id=NULL WHERE logical_fetch_id=?",
        (result.logical_fetch_id,),
    )
    ok, reason = verify_mandatory_document_checkpoint(
        conn,
        document_id=cell["identity_extensions"]["document_id"],
        s4_lineage_run_ids=["s4"],
        inventory_id="c" * 64,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert "raw" in (reason or "")


def test_arbitrary_checkpoint_cell_id_with_same_lfid_blocks(tmp_path: Path):
    conn = _boot(tmp_path)
    _seed_run(conn, "s4", "4" * 64)
    cell, descriptor, result = _seed_canonical_s4_doc(
        conn, run_id="s4", inventory_id="c" * 64
    )
    _allow_test_corruption(conn)
    conn.execute(
        "UPDATE source_checkpoints SET cell_id=? WHERE logical_fetch_id=?",
        ("f" * 64, result.logical_fetch_id),
    )
    ok, reason = verify_mandatory_document_checkpoint(
        conn,
        document_id=cell["identity_extensions"]["document_id"],
        s4_lineage_run_ids=["s4"],
        inventory_id="c" * 64,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert reason == "missing_checkpoint"


def test_duplicate_matching_s4_checkpoint_across_lineage_blocks(tmp_path: Path):
    conn = _boot(tmp_path)
    _seed_run(conn, "s4-root", "4" * 64)
    _seed_run(conn, "s4-leaf", "4" * 64)
    cell, descriptor, result = _seed_canonical_s4_doc(
        conn, run_id="s4-leaf", inventory_id="c" * 64
    )
    _seed_checkpoint(
        conn,
        run_id="s4-root",
        cell_id=cell["cell_id"],
        endpoint="sec_document",
        ticker="AAPL",
        lfid=result.logical_fetch_id,
        raw_asset_id=result.raw_asset_id,
    )
    ok, reason = verify_mandatory_document_checkpoint(
        conn,
        document_id=cell["identity_extensions"]["document_id"],
        s4_lineage_run_ids=["s4-leaf", "s4-root"],
        inventory_id="c" * 64,
        document_descriptor=descriptor,
    )
    assert ok is False
    assert reason == "duplicate_checkpoint"


def test_missing_carry_in_slot_from_frozen_inventory_blocks(tmp_path: Path):
    conn = _boot(tmp_path)
    _, universe_args = _ratified_inventory_args()
    inv = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="u" * 64,
        filing_entries=_complete_periodic_entries(omit="AAPL:10-Q"),
        **universe_args,
    )
    assert inv["missing_carry_in_slots"] == ["AAPL:10-Q"]
    # carry-in enters identity
    inv2 = dict(inv)
    inv2["missing_carry_in_slots"] = []
    inv2["inventory_id"] = compute_inventory_id(inv2)
    assert inv2["inventory_id"] != inv["inventory_id"]

    rep = evaluate_sec_readiness(
        conn,
        mandatory_document_ids=["a" * 64],
        inventory=inv,
        submissions_complete=True,
        index_complete=True,
        s4_lineage_run_ids=["s4"],
    )
    assert rep.sec_source_ready is False
    assert "AAPL:10-Q" in rep.missing_carry_in_slots
    conn.close()


def test_forged_stored_convergence_plan_hash_rejects(tmp_path: Path):
    path, real_h, base = _write_evidence(tmp_path)
    data = json.loads(path.read_text())
    data["convergence_plan_hash"] = "f" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(SecReadinessError, match="forged|stale"):
        load_and_verify_convergence_evidence(path)


def test_convergence_missing_component_rejects(tmp_path: Path):
    path, _, base = _write_evidence(tmp_path)
    data = json.loads(path.read_text())
    del data["s4_plan_hash"]
    path.write_text(json.dumps(data))
    with pytest.raises(SecReadinessError, match="missing keys"):
        load_and_verify_convergence_evidence(path)


def test_convergence_uppercase_non_hex_rejects(tmp_path: Path):
    path, _, base = _write_evidence(tmp_path)
    data = json.loads(path.read_text())
    data["s1_plan_hash"] = "A" * 64  # uppercase
    # also fix stored hash so only format fails
    del data["convergence_plan_hash"]
    path.write_text(json.dumps(data))
    with pytest.raises(SecReadinessError, match="lowercase hex"):
        load_and_verify_convergence_evidence(path)


def test_pre_b6_cli_cannot_call_legacy_builder(tmp_path: Path):
    """CLI-level: snapshot command refuses Pre-B6 flags; pre-b6-snapshot is required."""
    from catalyst_data.b2o import main

    # Using snapshot with --pre-b6 must fail closed
    code = main(
        [
            "snapshot",
            "--db", str(tmp_path / "x.db"),
            "--universe-manifest", str(tmp_path / "u.json"),
            "--plan", str(tmp_path / "p.json"),
            "--protected-source-sha256", "p" * 64,
            "--output", str(tmp_path / "out.json"),
            "--terminal-run-id", "r1",
            "--pre-b6",
            "--convergence-evidence", str(tmp_path / "c.json"),
        ]
    )
    assert code == 2


def test_inventory_tamper_id_recomputation(tmp_path: Path):
    _, universe_args = _ratified_inventory_args()
    inv = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="u" * 64,
        filing_entries=_complete_periodic_entries(),
        **universe_args,
    )
    path = tmp_path / "inv.json"
    inv["missing_carry_in_slots"] = ["MSFT:10-K"]  # tamper without recompute
    path.write_text(json.dumps(inv))
    with pytest.raises(SecReadinessError, match="inventory_id mismatch"):
        load_frozen_filing_inventory(path)


def test_resolve_lineage_plan_hash_must_match(tmp_path: Path):
    conn = _boot(tmp_path)
    _seed_run(conn, "r1", "1" * 64)
    conn.commit()
    with pytest.raises(SecReadinessError, match="plan_hash drift"):
        resolve_run_lineage(conn, "r1", expected_plan_hash="2" * 64)
    lin = resolve_run_lineage(conn, "r1", expected_plan_hash="1" * 64)
    assert lin == ["r1"]
    conn.close()
