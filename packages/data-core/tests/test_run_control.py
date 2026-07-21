"""B2 — durable run control tests."""
from __future__ import annotations

import sqlite3
import pytest


class TestRunControl:
    def test_request_cancel_and_check(self):
        """cancel_requested is persisted and readable."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run
        from catalyst_data.ingestion.run_control import request_cancel, is_cancelled

        db = _fresh_db_at_version(8)
        run_id = _seed_ingestion_run(db, run_id="run-cancel", status="RUNNING_EVIDENCE")

        assert not is_cancelled(db, run_id)
        request_cancel(db, run_id)
        assert is_cancelled(db, run_id)
        db.close()

    def test_acquire_lease_success(self):
        """Lease acquired when none held."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run
        from catalyst_data.ingestion.run_control import acquire_lease

        db = _fresh_db_at_version(8)
        run_id = _seed_ingestion_run(db, run_id="run-lease")
        assert acquire_lease(db, run_id, holder="worker-1")
        db.close()

    def test_lease_rejected_when_held(self):
        """Second lease rejected while first is active."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run
        from catalyst_data.ingestion.run_control import acquire_lease

        db = _fresh_db_at_version(8)
        run_id = _seed_ingestion_run(db, run_id="run-lease2")
        assert acquire_lease(db, run_id, holder="worker-1", ttl_seconds=300)
        assert not acquire_lease(db, run_id, holder="worker-2", ttl_seconds=300)
        db.close()

    def test_release_and_reacquire(self):
        """After release, another worker can acquire."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run
        from catalyst_data.ingestion.run_control import acquire_lease, release_lease

        db = _fresh_db_at_version(8)
        run_id = _seed_ingestion_run(db, run_id="run-rel")
        acquire_lease(db, run_id, holder="worker-1")
        release_lease(db, run_id, holder="worker-1")
        assert acquire_lease(db, run_id, holder="worker-3")
        db.close()

    def test_get_next_stage_start_ohlcv(self):
        """PLANNED → next stage is RUNNING_OHLCV."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run
        from catalyst_data.ingestion.run_control import get_next_stage

        db = _fresh_db_at_version(8)
        run_id = _seed_ingestion_run(db, run_id="run-stage", status="pending")
        assert get_next_stage(db, run_id) == "RUNNING_OHLCV"
        db.close()

    def test_get_next_stage_resume_partial(self):
        """PARTIAL → next stage is RUNNING_OHLCV (resume)."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run
        from catalyst_data.ingestion.run_control import get_next_stage

        db = _fresh_db_at_version(8)
        run_id = _seed_ingestion_run(db, run_id="run-partial", status="partial")
        assert get_next_stage(db, run_id) == "RUNNING_OHLCV"
        db.close()

    def test_get_next_stage_evidence_after_ohlcv(self):
        """When all OHLCV cells succeeded → RUNNING_EVIDENCE."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run
        from catalyst_data.ingestion.run_control import get_next_stage

        db = _fresh_db_at_version(8)
        run_id = _seed_ingestion_run(db, run_id="run-ev")
        db.execute(
            """INSERT INTO source_checkpoints (run_id, source_type, ticker, date, status)
               VALUES (?, 'ohlcv', 'AAPL', '2026-01-01', 'success')""",
            (run_id,),
        )
        db.execute(
            """INSERT INTO source_checkpoints (run_id, source_type, ticker, date, status)
               VALUES (?, 'ohlcv', 'AAPL', '2026-01-02', 'success')""",
            (run_id,),
        )
        db.commit()
        assert get_next_stage(db, run_id) == "RUNNING_EVIDENCE"
        db.close()
