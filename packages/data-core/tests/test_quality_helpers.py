"""Tests for Step 2 quality.py write helpers."""

from __future__ import annotations

import sqlite3
import time

import pytest

from catalyst_data.quality import (
    open_ingestion_run,
    close_ingestion_run,
    write_source_checkpoint,
    close_stale_runs,
)
from catalyst_data.storage.sqlite import init_db
from catalyst_data.quality import ensure_ingestion_quality_tables


def _make_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_db(conn)
    ensure_ingestion_quality_tables(conn)
    return conn


class TestOpenCloseIngestionRun:
    def test_open_creates_row(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        run_id = open_ingestion_run(
            conn, tickers=["AAPL", "TSLA"], sources=["polygon_news"],
        )

        row = conn.execute(
            "SELECT run_id, status, success_count, fail_count "
            "FROM ingestion_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        assert row[1] == "running"
        assert row[2] == 0
        conn.close()

    def test_close_sets_counts(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        run_id = open_ingestion_run(
            conn, tickers=["AAPL"], sources=["polygon_news"],
        )
        close_ingestion_run(
            conn, run_id=run_id, success_count=10, fail_count=2,
        )

        row = conn.execute(
            "SELECT status, success_count, fail_count, ended_at "
            "FROM ingestion_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row[0] == "completed"
        assert row[1] == 10
        assert row[2] == 2
        assert row[3] is not None
        conn.close()


class TestSourceCheckpoint:
    def test_idempotent_insert(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        run_id = open_ingestion_run(
            conn, tickers=["AAPL"], sources=["polygon_news"],
        )

        # First write
        write_source_checkpoint(
            conn, run_id=run_id, source_type="polygon_news",
            ticker="AAPL", date="2026-05-01", status="success",
        )

        # Second write (simulated re-run)
        write_source_checkpoint(
            conn, run_id=run_id, source_type="polygon_news",
            ticker="AAPL", date="2026-05-01", status="success",
        )

        count = conn.execute(
            "SELECT COUNT(*) FROM source_checkpoints "
            "WHERE run_id = ? AND ticker = 'AAPL' AND date = '2026-05-01'",
            (run_id,),
        ).fetchone()[0]
        assert count == 1  # Not duplicated
        conn.close()

    def test_stores_error(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        run_id = open_ingestion_run(
            conn, tickers=["AAPL"], sources=["polygon_news"],
        )
        write_source_checkpoint(
            conn, run_id=run_id, source_type="polygon_news",
            ticker="AAPL", date="2026-05-01", status="failed",
            error_class="RateLimitError", retries=2,
        )

        row = conn.execute(
            "SELECT status, error_class, retries FROM source_checkpoints "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row[0] == "failed"
        assert row[1] == "RateLimitError"
        assert row[2] == 2
        conn.close()


class TestCloseStaleRuns:
    def test_marks_old_running_runs(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        # Create a run with a started_at far in the past
        run_id = "run_stale_1"
        conn.execute(
            """INSERT INTO ingestion_runs
               (run_id, started_at, ticker_list_json, source_list_json, status)
               VALUES (?, '2020-01-01T00:00:00', '[]', '[]', 'running')""",
            (run_id,),
        )
        conn.commit()

        count = close_stale_runs(conn, max_age_hours=24)
        assert count == 1

        row = conn.execute(
            "SELECT status FROM ingestion_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row[0] == "interrupted"
        conn.close()

    def test_leaves_recent_runs(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_db(db_path)

        from datetime import datetime, timezone
        recent = datetime.now(timezone.utc).isoformat()
        run_id = "run_recent_1"
        conn.execute(
            """INSERT INTO ingestion_runs
               (run_id, started_at, ticker_list_json, source_list_json, status)
               VALUES (?, ?, '[]', '[]', 'running')""",
            (run_id, recent),
        )
        conn.commit()

        count = close_stale_runs(conn, max_age_hours=24)
        assert count == 0
        conn.close()
