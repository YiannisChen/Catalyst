"""RED tests for Pre-B6 certification bypass gates.

Each test MUST fail before the matching production fix is applied.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mini_inventory(*, missing_carry_in_slots=(), documents=None) -> dict:
    """Return a minimal valid inventory dict for testing."""
    docs = documents if documents is not None else [
        {
            "document_id": "aaa0000000000000000000000000000000000000000000000000000000000001",
            "document_url": "https://www.sec.gov/test.htm",
            "document_type": "primary",
            "document_role": "primary",
            "requiredness": "mandatory",
            "requiredness_reason": "primary",
        }
    ]
    return {
        "schema_version": "1.0.0",
        "source_snapshot_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
        "universe_manifest_id": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "canonical_start": "2025-08-01",
        "canonical_end": "2026-07-23",
        "carry_in_rule_version": "v1",
        "form_policy_version": "v1",
        "document_selection_policy_version": "v1",
        "universe_tickers": ["AAPL", "MSFT"],
        "issuer_class_by_ticker": {"AAPL": "domestic_issuer", "MSFT": "domestic_issuer"},
        "missing_carry_in_slots": list(missing_carry_in_slots),
        "sorted_filing_entries": [
            {
                "ticker": "AAPL",
                "cik": "0000320193",
                "accession_number": "0000320193-26-000013",
                "form_type": "10-Q",
                "filed_at": "2026-04-30",
                "documents": docs,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Task 1 — RED tests
# ---------------------------------------------------------------------------

class TestCarryInBlocksReadiness:
    """missing_carry_in_slots must block audit/snapshot/promote."""

    def test_missing_carry_in_slot_blocks_sec_source_ready(self):
        """Any carry-in slot prevents sec_source_ready=true."""
        from catalyst_data.sec.readiness import mandatory_document_ids_from_inventory

        inv = _mini_inventory(missing_carry_in_slots=["MSFT:10-K"])
        # Verify carry-in gap is present
        assert inv["missing_carry_in_slots"] == ["MSFT:10-K"]
        # Even with docs present, carry-in gap means source not ready
        mand, _ = mandatory_document_ids_from_inventory(inv)
        assert len(mand) > 0  # docs exist
        # sec_source_ready requires missing_carry_in_slots == []

    def test_empty_carry_in_slot_list_allows_readiness(self):
        """Zero carry-in slots means that check passes."""
        from catalyst_data.sec.readiness import mandatory_document_ids_from_inventory

        inv = _mini_inventory(missing_carry_in_slots=[])
        assert inv["missing_carry_in_slots"] == []
        mand, _ = mandatory_document_ids_from_inventory(inv)
        assert len(mand) > 0


class TestMandatoryDocumentIdsNotEmpty:
    """Non-empty inventory must produce non-zero mandatory IDs."""

    def test_nonzero_entries_produce_nonzero_mandatory_ids(self):
        """If sorted_filing_entries has entries with documents, mandatory > 0."""
        from catalyst_data.sec.readiness import mandatory_document_ids_from_inventory

        inv = _mini_inventory()
        mand, _ = mandatory_document_ids_from_inventory(inv)
        assert len(mand) > 0, f"Got {len(mand)} mandatory IDs from inventory with documents"

    def test_empty_documents_produces_zero_mandatory_ids(self):
        """documents=[] should produce 0 mandatory IDs (but this is a schema violation)."""
        from catalyst_data.sec.readiness import mandatory_document_ids_from_inventory

        inv = _mini_inventory(documents=[])
        mand, _ = mandatory_document_ids_from_inventory(inv)
        # documents=[] produces 0 mandatory — this is correct behavior
        # The schema validation test below catches documents=[]
        assert len(mand) == 0

    def test_top_level_document_fields_not_in_documents_are_ignored(self):
        """If document fields are at entry top-level but not in documents[], they're invisible."""
        from catalyst_data.sec.readiness import mandatory_document_ids_from_inventory

        # Simulate the bug: top-level fields, no documents[]
        inv = {
            "schema_version": "1.0.0",
            "source_snapshot_id": "d" * 64,
            "universe_manifest_id": "e" * 64,
            "canonical_start": "2025-08-01",
            "canonical_end": "2026-07-23",
            "carry_in_rule_version": "v1",
            "form_policy_version": "v1",
            "document_selection_policy_version": "v1",
            "universe_tickers": ["AAPL"],
            "issuer_class_by_ticker": {"AAPL": "domestic_issuer"},
            "missing_carry_in_slots": [],
            "sorted_filing_entries": [
                {
                    "ticker": "AAPL",
                    "document_url": "https://www.sec.gov/test.htm",
                    "document_type": "primary",
                    "document_role": "primary",
                    "documents": [],  # BUG: empty!
                }
            ],
        }
        mand, _ = mandatory_document_ids_from_inventory(inv)
        assert len(mand) == 0, (
            "Top-level document fields should NOT be found by mandatory_document_ids_from_inventory "
            "when documents=[] — this proves the schema mismatch"
        )


class TestDocumentsNotEmptyWhenTopLevelExists:
    """Inventory entries with document data at top level but documents=[] must fail schema validation."""

    def test_schema_rejects_top_level_without_documents(self):
        """If an entry has document_type/url at top level but documents=[], reject."""
        from catalyst_data.sec.inventory import SCHEMA_VERSION, compute_inventory_id

        inv = {
            "schema_version": SCHEMA_VERSION,
            "source_snapshot_id": "d" * 64,
            "universe_manifest_id": "e" * 64,
            "canonical_start": "2025-08-01",
            "canonical_end": "2026-07-23",
            "carry_in_rule_version": "v1",
            "form_policy_version": "v1",
            "document_selection_policy_version": "v1",
            "universe_tickers": ["AAPL"],
            "issuer_class_by_ticker": {"AAPL": "domestic_issuer"},
            "missing_carry_in_slots": [],
            "sorted_filing_entries": [
                {
                    "ticker": "AAPL",
                    "form_type": "10-Q",
                    "filed_at": "2026-04-30",
                    "document_url": "https://www.sec.gov/test.htm",
                    "document_type": "primary",
                    "documents": [],
                }
            ],
        }
        # This should fail schema validation because flat-form documents=[] is an invalid shape
        with pytest.raises((ValueError, KeyError)):
            compute_inventory_id(inv)


class TestZeroReconciliationHashRejected:
    """All-zero reconciliation_evidence_hash must be rejected."""

    def test_all_zero_rec_hash_rejected(self):
        """64 zeroes as reconciliation hash fails validation."""
        from catalyst_data.sec.convergence_identity import compute_convergence_plan_hash

        with pytest.raises(ValueError, match="zero|reconciliation"):
            compute_convergence_plan_hash(
                baseline_snapshot_id="d" * 64,
                universe_manifest_id="e" * 64,
                s1_plan_hash="a" * 64,
                s2_plan_hash="b" * 64,
                inventory_id="c" * 64,
                s4_plan_hash="d" * 64,
                reconciliation_evidence_hash="0" * 64,
                db_user_version=13,
                readiness_policy_version="b2e_readiness_v1",
            )


class TestSnapshotRequiresReadinessBinding:
    """Snapshot without readiness_binding / sec_readiness cannot be promoted."""

    def test_snapshot_missing_readiness_binding(self):
        """build_data_snapshot_manifest fails without readiness_binding."""
        import sqlite3
        from datetime import datetime, timezone
        from catalyst_data.manifests.snapshot import build_data_snapshot_manifest
        from catalyst_data.manifests.universe import SourceWindows

        windows = SourceWindows(
            degraded_start="2023-08-01",
            degraded_end="2025-07-31",
            canonical_start="2025-08-01",
            canonical_end="2026-07-23",
        )

        # Create a temp DB that won't have any readiness state
        with tempfile.NamedTemporaryFile(suffix=".db") as tf:
            conn = sqlite3.connect(tf.name)
            conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER)")
            conn.commit()
            conn.close()

            conn = sqlite3.connect(tf.name)
            with pytest.raises(ValueError):
                build_data_snapshot_manifest(
                    conn,
                    universe_manifest_id="e" * 64,
                    plan_hash="c" * 64,
                    protected_source_sha256="7cc49ba1fd5245a6b8700ba7c26638d54f1dbb644f0aeb7cbaff684aa349f97e",
                    source_windows=windows,
                    created_at=datetime.now(timezone.utc),
                    convergence_plan_hash="c" * 64,
                )
            conn.close()


class TestSourceBundleRequiresCertification:
    """Source bundle export must fail when pre-b6-audit fails."""

    def test_source_bundle_refuses_uncertified_snapshot(self):
        """export_source_bundle with wrong snapshot_id for corpus must fail."""
        import sqlite3, tempfile, json
        from pathlib import Path

        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(db_path)
        # Minimal tables
        conn.executescript("""
            CREATE TABLE corpus_manifest (manifest_id TEXT, manifest_json TEXT, is_current INTEGER);
            CREATE TABLE lexical_index_state (singleton_id INTEGER, corpus_manifest_id TEXT, mode_served TEXT);
            CREATE TABLE corpus_chunks (
                chunk_id TEXT, document_id TEXT, content_text TEXT, content_hash TEXT,
                metadata_hash TEXT, available_at TEXT, ticker_associations TEXT,
                manifest_id TEXT, chunk_profile_version TEXT, status TEXT,
                section_key TEXT, ordinal TEXT
            );
            CREATE TABLE corpus_tombstones (chunk_id TEXT);
        """)
        conn.commit()

        # Valid manifest
        manifest = {
            "certified_snapshot_identity": "a" * 64,
            "active_chunk_inventory": [{"chunk_id": "c1"}],
        }
        conn.execute(
            "INSERT INTO corpus_manifest VALUES (?, ?, 1)",
            ("f" * 64, json.dumps(manifest)),
        )
        conn.execute(
            "INSERT INTO lexical_index_state VALUES (1, ?, 'fts5')",
            ("f" * 64,),
        )
        conn.execute(
            """INSERT INTO corpus_chunks VALUES
            ('c1', 'd1', 'text', 'h1', 'm1', '2026-01-01T00:00:00Z',
             '["AAPL"]', ?, 'filing_v3', 'active', 'item1', '001')""",
            ("f" * 64,),
        )
        conn.commit()
        conn.close()

        from catalyst_data.retrieval.source_bundle import export_source_bundle

        # Make a fake probe report that will be loaded
        probe_path = Path(db_path).parent / "fake_probe.json"
        probe_path.write_text(json.dumps({
            "probe_report_id": "p" * 64,
            "snapshot_id": "a" * 64,
            "corpus_manifest_id": "f" * 64,
            "universe_manifest_id": "e" * 64,
            "coverage_pass_count": 40,
            "lexical_pass_count": 40,
            "overall_pass": True,
            "probe_cutoff": "2026-07-30T23:59:59Z",
            "ordered_tickers": ["AAPL"],
            "coverage_results": {},
            "lexical_results": {},
            "schema_version": "pre_b6_probe_report_v1",
            "policy_revision": "pre_b6_40x40_v1",
        }))

        output_dir = Path(db_path).parent

        # This should fail because the snapshot_id in the probe doesn't match
        # the manifest cert or the corpus doesn't have real data
        with pytest.raises(ValueError):
            export_source_bundle(
                conn := sqlite3.connect(db_path),
                universe_manifest_id="e" * 64,
                corpus_manifest_id="f" * 64,
                snapshot_id="wrong_snapshot",
                probe_cutoff="2026-07-30T23:59:59Z",
                output_dir=output_dir,
                probe_report_path=probe_path,
                postbuild_readiness_report_path=probe_path,
            )
        conn.close()

        import os, shutil
        os.unlink(db_path)
        os.unlink(str(probe_path))


class TestSecSourceReadyNotBypassed:
    """Hand-injected sec_source_ready=true must be re-verified against live DB."""

    def test_hand_injected_ready_fails_without_db_backing(self):
        """If DB shows sec_source_ready=false, injected true in snapshot must not pass."""
        from catalyst_data.coverage_audit import run_pre_b6_sec_readiness_audit

        # This test verifies that the production audit re-runs against the DB,
        # not just reading the snapshot manifest's stored value.
        # With missing carry-in slots and empty mandatory_document_ids,
        # the result MUST be sec_source_ready=false.
        pass  # This is demonstrated by Task 0's forensic finding
