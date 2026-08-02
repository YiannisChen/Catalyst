"""RED tests for Pre-B6 identity gates: inventory drift, root family isolation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Test 1: Different inventory_id => different cell_id => LFID mismatch expected
# ---------------------------------------------------------------------------

class TestInventoryDriftRejected:
    """verify_mandatory_document_checkpoint must reject cells from a different inventory."""

    def test_different_inventory_produces_different_cell_id(self):
        """Two inventories with different IDs produce different cell_ids for same document."""
        from catalyst_data.sec.document_cells import build_document_cell

        cell_a = build_document_cell(
            ticker="AAPL", filed_date="2026-01-01",
            inventory_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            accession_number="0000320193-26-000013",
            document_role="primary", document_file="test.htm",
            document_url="https://www.sec.gov/test.htm",
            filing_id="sec:0000320193:0000320193-26-000013",
            requiredness="mandatory", requiredness_reason="primary",
        )
        cell_b = build_document_cell(
            ticker="AAPL", filed_date="2026-01-01",
            inventory_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            accession_number="0000320193-26-000013",
            document_role="primary", document_file="test.htm",
            document_url="https://www.sec.gov/test.htm",
            filing_id="sec:0000320193:0000320193-26-000013",
            requiredness="mandatory", requiredness_reason="primary",
        )
        assert cell_a["cell_id"] != cell_b["cell_id"], (
            "cell_id must change when inventory_id changes"
        )

    def test_different_inventory_lfid_rejected_by_oracle(self):
        """verify_mandatory_document_checkpoint rejects LFID from different inventory."""
        from catalyst_data.sec.readiness import verify_mandatory_document_checkpoint
        from catalyst_data.sec.document_cells import build_document_cell

        # Build a DB with a synthetic S4 checkpoint and provenance
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(db_path)

        # Minimal schema
        for stmt in [
            "CREATE TABLE source_checkpoints (run_id TEXT, cell_id TEXT, logical_fetch_id TEXT, endpoint_name TEXT, status TEXT, is_complete INTEGER, request_count INTEGER, raw_asset_id TEXT, ticker TEXT, window_start TEXT, window_end TEXT, source_type TEXT, provider_profile_version TEXT)",
            "CREATE TABLE provider_request_attempts (request_id TEXT, run_id TEXT, logical_fetch_id TEXT, endpoint_name TEXT, status TEXT, raw_asset_id TEXT, response_sha256 TEXT, provider TEXT, ticker_or_series TEXT, window_start TEXT, window_end TEXT, request_params_redacted TEXT, attempt_no INTEGER, page_no INTEGER, http_status INTEGER)",
            "CREATE TABLE raw_assets (asset_id TEXT, request_id TEXT, response_sha256 TEXT)",
            "CREATE TABLE normalized_provenance (entity_type TEXT, entity_id TEXT, raw_asset_id TEXT)",
            "CREATE TABLE filing_documents (filing_id TEXT, document_url TEXT, document_type TEXT, document_id TEXT, text TEXT, char_len INTEGER, extraction_status TEXT, content_type TEXT, byte_size INTEGER)",
            "CREATE TABLE corpus_chunks (document_id TEXT, chunk_profile_version TEXT, eligibility TEXT)",
        ]:
            conn.execute(stmt)

        # Build cell with inventory A
        inv_a = "a" * 64
        cell_a = build_document_cell(
            ticker="AAPL", filed_date="2026-01-01",
            inventory_id=inv_a,
            accession_number="0000320193-26-000013",
            document_role="primary", document_file="test.htm",
            document_url="https://www.sec.gov/test.htm",
            filing_id="sec:0000320193:0000320193-26-000013",
            requiredness="mandatory", requiredness_reason="primary",
        )
        doc_id = cell_a["identity_extensions"]["document_id"]
        cell_id_a = cell_a["cell_id"]
        run_id = "b2-test99999999999999999999999999"

        from catalyst_data.ingestion.request_ledger import compute_logical_fetch_id
        lfid = compute_logical_fetch_id(run_id, cell_id_a)

        # Insert S4 checkpoint and provenance with cell_id from inventory A
        request_id = "req" + hashlib.sha256(b"test").hexdigest()[:60]
        raw_id = "raw:" + request_id
        raw_sha = hashlib.sha256(b"body").hexdigest()

        conn.execute("INSERT INTO raw_assets VALUES (?, ?, ?)", (raw_id, request_id, raw_sha))
        conn.execute(
            "INSERT INTO normalized_provenance VALUES ('filing', ?, ?)", (doc_id, raw_id)
        )
        conn.execute(
            "INSERT INTO source_checkpoints VALUES (?, ?, ?, 'sec_document', 'success', 1, 1, ?, 'AAPL', '2026-01-01', '2026-01-01', 'sec_filings', 'v1')",
            (run_id, cell_id_a, lfid, raw_id),
        )
        params = json.dumps({
            "ticker": "AAPL", "window_start": "2026-01-01", "window_end": "2026-01-01",
            "page_no": 1, "endpoint_name": "sec_document",
            "cell_id": cell_id_a, "identity_schema_version": "sec_cell_v2",
            "inventory_id": inv_a, "accession_number": "0000320193-26-000013",
            "document_id": doc_id, "document_role": "primary",
            "document_file": "test.htm", "document_url": "https://www.sec.gov/test.htm",
            "requiredness": "mandatory", "requiredness_reason": "primary",
            "filing_id": "sec:0000320193:0000320193-26-000013",
        })
        conn.execute(
            """INSERT INTO provider_request_attempts VALUES
            (?, ?, ?, 'sec_document', 'SUCCEEDED', ?, ?, 'sec', 'AAPL', '2026-01-01', '2026-01-01', ?, 1, 1, 200)""",
            (request_id, run_id, lfid, raw_id, raw_sha, params),
        )
        conn.execute(
            "INSERT INTO filing_documents VALUES (?, ?, 'primary', ?, 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 200, 'success', 'text/html', 200)",
            ("sec:0000320193:0000320193-26-000013", "https://www.sec.gov/test.htm", doc_id),
        )
        conn.commit()

        # Now try to verify with inventory B (different ID)
        inv_b = "b" * 64
        descriptor = {
            "ticker": "AAPL", "filed_date": "2026-01-01",
            "accession_number": "0000320193-26-000013",
            "document_role": "primary", "document_file": "test.htm",
            "document_url": "https://www.sec.gov/test.htm",
            "filing_id": "sec:0000320193:0000320193-26-000013",
            "requiredness": "mandatory", "requiredness_reason": "primary",
            "provider_profile_version": "v1",
        }
        ok, reason = verify_mandatory_document_checkpoint(
            conn, document_id=doc_id,
            s4_lineage_run_ids=[run_id],
            inventory_id=inv_b,  # DIFFERENT inventory!
            document_descriptor=descriptor,
        )
        # Must fail because inventory_id differs => cell_id differs => LFID mismatch
        assert not ok, f"Expected failure with different inventory, got ok=True"
        assert reason is not None and "lfid" in reason.lower() or "mismatch" in reason.lower() or "cell" in reason.lower(), (
            f"Expected LFID/cell mismatch reason, got: {reason}"
        )

        conn.close()
        import os; os.unlink(db_path)


# ---------------------------------------------------------------------------
# Test 2: Independent root run cannot masquerade as same lineage
# ---------------------------------------------------------------------------

class TestRootFamilyIsolation:
    """Independent root runs must not be accepted as same lineage."""

    def test_different_root_run_must_have_different_cell_ids(self):
        """Two root plans with parent_run_id=NULL produce different cell_ids."""
        from catalyst_data.sec.index_cells import build_index_cell

        cell1 = build_index_cell(
            ticker="AAPL", filed_date="2026-01-01",
            accession_number="0000320193-26-000013",
            cik="0000320193",
        )
        # Different form_policy_version => different cell_id
        cell2 = build_index_cell(
            ticker="AAPL", filed_date="2026-01-01",
            accession_number="0000320193-26-000013",
            cik="0000320193",
            form_policy_version="v2",
        )
        assert cell1["cell_id"] != cell2["cell_id"]

    def test_resolve_run_lineage_rejects_wrong_plan_hash(self):
        """S4 lineage resolution must reject runs with wrong plan_hash."""
        from catalyst_data.sec.readiness import SecReadinessError, resolve_run_lineage

        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE ingestion_runs (run_id TEXT, plan_hash TEXT, expected_plan_hash TEXT, parent_run_id TEXT, status TEXT)")
        conn.execute(
            "INSERT INTO ingestion_runs VALUES ('r1', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', NULL, 'completed')"
        )
        conn.commit()

        lineage = resolve_run_lineage(conn, "r1", expected_plan_hash="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        assert len(lineage) == 1

        # Wrong plan_hash must fail (raises SecReadinessError)
        from catalyst_data.sec.readiness import SecReadinessError
        with pytest.raises(SecReadinessError, match="plan_hash"):
            resolve_run_lineage(conn, "r1", expected_plan_hash="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

        conn.close()
        import os; os.unlink(db_path)
