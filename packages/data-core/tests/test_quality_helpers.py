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


# ---------------------------------------------------------------------------
# H2b — source_checkpoints schema hardening
# ---------------------------------------------------------------------------

LEGACY_SOURCE_CHECKPOINTS_DDL = """
CREATE TABLE IF NOT EXISTS source_checkpoints (
    run_id               TEXT NOT NULL,
    source_type          TEXT NOT NULL,
    ticker               TEXT NOT NULL,
    date                 TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed', 'skipped')),
    error_class          TEXT,
    retries              INTEGER NOT NULL DEFAULT 0,
    error_message_redacted TEXT, http_status INTEGER, retry_after_seconds REAL, provider_latency_ms REAL, raw_asset_id TEXT, items_count INTEGER, fallback_provider TEXT, fallback_triggered INTEGER DEFAULT 0, empty_reason TEXT,
    PRIMARY KEY (run_id, source_type, ticker, date)
);
"""

EXPECTED_COLUMNS = [
    "run_id", "source_type", "ticker", "date", "status",
    "error_class", "retries", "error_message_redacted", "http_status",
    "retry_after_seconds", "provider_latency_ms", "raw_asset_id", "items_count",
    "fallback_provider",
    "fallback_triggered",
    "empty_reason",
    "logical_fetch_id",
    "request_count",
    "pages_received",
    "items_received",
    "is_complete",
]


class TestSourceCheckpointSchema:
    """H2b — schema hardening: new columns, CHECK removal, reconcile."""

    def test_new_columns_exist(self, tmp_path):
        """After ensure_ingestion_quality_tables, all 18 columns present."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)

        rows = conn.execute("PRAGMA table_info(source_checkpoints)").fetchall()
        columns = [r[1] for r in rows]
        conn.close()

        for col in EXPECTED_COLUMNS:
            assert col in columns, f"Missing column: {col}"

        assert len(columns) == len(EXPECTED_COLUMNS), (
            f"Expected {len(EXPECTED_COLUMNS)} columns, got {len(columns)}: {columns}"
        )

    def test_success_empty_on_legacy_db_after_reconcile(self, tmp_path):
        """Legacy DB with CHECK → reconcile removes it, success_empty works."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        # Only create the basic structure via legacy DDL — DON'T call init_db
        # (init_db would create the new-DDL table first, defeating the test)
        conn.executescript("PRAGMA journal_mode=WAL")
        conn.executescript(LEGACY_SOURCE_CHECKPOINTS_DDL)
        conn.commit()

        # Verify CHECK exists before reconcile
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_checkpoints'"
        ).fetchone()
        assert sql_row is not None and "CHECK (" in (sql_row[0] or "").upper(), (
            "Legacy DDL should contain CHECK"
        )

        # Run ensure (which calls reconcile)
        ensure_ingestion_quality_tables(conn)

        # Verify CHECK is gone
        sql_row2 = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_checkpoints'"
        ).fetchone()
        assert sql_row2 is not None and "CHECK (" not in (sql_row2[0] or "").upper(), (
            "CHECK should be gone after reconcile"
        )

        # Write success_empty — should not raise
        write_source_checkpoint(
            conn, run_id="run_test", source_type="polygon_news",
            ticker="AAPL", date="2026-05-01", status="success_empty",
        )
        row = conn.execute(
            "SELECT status FROM source_checkpoints WHERE run_id = 'run_test'"
        ).fetchone()
        assert row[0] == "success_empty"
        conn.close()

    def test_rejects_invalid_status(self, tmp_path):
        """write_source_checkpoint(status='bogus') → AssertionError."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)

        with pytest.raises(AssertionError, match="Invalid checkpoint status"):
            write_source_checkpoint(
                conn, run_id="run_test", source_type="polygon_news",
                ticker="AAPL", date="2026-05-01", status="bogus",
            )
        conn.close()

    def test_error_message_redacted(self, tmp_path):
        """write_source_checkpoint with error_message_redacted containing secret."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)

        write_source_checkpoint(
            conn, run_id="run_test", source_type="polygon_news",
            ticker="AAPL", date="2026-05-01", status="failed",
            error_message_redacted="API key sk-abc123 was invalid",
        )

        row = conn.execute(
            "SELECT error_message_redacted FROM source_checkpoints WHERE run_id = 'run_test'"
        ).fetchone()
        assert row[0] == "API key sk-abc123 was invalid"
        conn.close()

    def test_reconcile_idempotent(self, tmp_path):
        """Running reconcile twice → no error, CHECK stays gone."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.executescript(LEGACY_SOURCE_CHECKPOINTS_DDL)
        conn.commit()

        # First reconcile
        ensure_ingestion_quality_tables(conn)

        # Second reconcile — should be a no-op
        ensure_ingestion_quality_tables(conn)

        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_checkpoints'"
        ).fetchone()
        assert "CHECK (" not in (sql_row[0] or "").upper()
        conn.close()

    def test_reconcile_preserves_rows(self, tmp_path):
        """Reconcile preserves all existing rows."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.executescript(LEGACY_SOURCE_CHECKPOINTS_DDL)
        conn.commit()

        # Seed 5 rows
        for i in range(5):
            conn.execute(
                "INSERT OR REPLACE INTO source_checkpoints "
                "(run_id, source_type, ticker, date, status, error_class, retries) "
                "VALUES (?, 'polygon_news', 'AAPL', ?, 'success', NULL, 0)",
                (f"run_seed_{i}", f"2026-05-0{i + 1}"),
            )
        conn.commit()

        count_before = conn.execute("SELECT COUNT(*) FROM source_checkpoints").fetchone()[0]
        assert count_before == 5

        ensure_ingestion_quality_tables(conn)

        count_after = conn.execute("SELECT COUNT(*) FROM source_checkpoints").fetchone()[0]
        assert count_after == 5
        conn.close()

    def test_write_checkpoint_with_new_fields(self, tmp_path):
        """Write with raw_asset_id + items_count → stored correctly."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        ensure_ingestion_quality_tables(conn)

        write_source_checkpoint(
            conn, run_id="run_test", source_type="polygon_news",
            ticker="AAPL", date="2026-05-01", status="success",
            raw_asset_id="raw_asset_42", items_count=17,
        )

        row = conn.execute(
            "SELECT raw_asset_id, items_count FROM source_checkpoints WHERE run_id = 'run_test'"
        ).fetchone()
        assert row[0] == "raw_asset_42"
        assert row[1] == 17
        conn.close()
