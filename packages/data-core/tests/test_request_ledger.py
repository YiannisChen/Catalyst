"""B2 — request-attempt ledger tests."""
from __future__ import annotations

import sqlite3
import pytest


class TestRequestLedger:
    def test_insert_attempt(self):
        """Insert one attempt row."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT, BASE_ATTEMPT
        from catalyst_data.ingestion.request_ledger import insert_attempt

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id=BASE_ATTEMPT["run_id"])

        insert_attempt(db, BASE_ATTEMPT)
        row = db.execute(
            "SELECT * FROM provider_request_attempts WHERE request_id = ?",
            (BASE_ATTEMPT["request_id"],),
        ).fetchone()
        assert row is not None
        assert row["status"] == "STARTED"
        db.close()

    def test_request_id_is_unique(self):
        """Duplicate request_id raises IntegrityError."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT, BASE_ATTEMPT
        from catalyst_data.ingestion.request_ledger import insert_attempt

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id=BASE_ATTEMPT["run_id"])

        insert_attempt(db, BASE_ATTEMPT)
        with pytest.raises(sqlite3.IntegrityError):
            insert_attempt(db, BASE_ATTEMPT)
        db.close()

    def test_transition_to_terminal(self):
        """STARTED → SUCCEEDED with http_status and items_count."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT, BASE_ATTEMPT
        from catalyst_data.ingestion.request_ledger import insert_attempt, transition_attempt

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id=BASE_ATTEMPT["run_id"])
        insert_attempt(db, BASE_ATTEMPT)

        transition_attempt(db, BASE_ATTEMPT["request_id"], "SUCCEEDED",
                           http_status=200, items_count=30)
        row = db.execute(
            "SELECT status, http_status, items_count FROM provider_request_attempts WHERE request_id = ?",
            (BASE_ATTEMPT["request_id"],),
        ).fetchone()
        assert row["status"] == "SUCCEEDED"
        assert row["http_status"] == 200
        assert row["items_count"] == 30
        db.close()

    def test_logical_fetch_id_deterministic(self):
        """logical_fetch_id = SHA256(run_id + ':' + cell_id)."""
        from catalyst_data.ingestion.request_ledger import compute_logical_fetch_id

        lf1 = compute_logical_fetch_id("run-001", "cell-abc")
        lf2 = compute_logical_fetch_id("run-001", "cell-abc")
        lf3 = compute_logical_fetch_id("run-002", "cell-abc")
        assert lf1 == lf2
        assert lf1 != lf3
        assert len(lf1) == 64

    def test_page_no_must_be_positive(self):
        """page_no must be >= 1 per CHECK constraint."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT
        from catalyst_data.ingestion.request_ledger import insert_attempt
        import copy

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db)

        bad = copy.deepcopy(dict(BASE_ATTEMPT))
        bad["page_no"] = 0
        with pytest.raises(sqlite3.IntegrityError):
            insert_attempt(db, bad)
        db.close()
