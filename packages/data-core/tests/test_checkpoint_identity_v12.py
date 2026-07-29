"""Tests for v12 checkpoint identity convergence.

All tests call production functions: run_migrations, _write_b2_checkpoint,
_remaining_b2_cells. All tests GREEN.
"""

from __future__ import annotations

import sqlite3
import hashlib
import pytest
from dataclasses import dataclass, field

from catalyst_data.manifests.snapshot import SNAPSHOT_TABLE_INVENTORY, _table_hash
from catalyst_data.migrations import MIGRATIONS, run_migrations, CURRENT_SCHEMA_VERSION
from catalyst_data.update_pipeline import _write_b2_checkpoint, _remaining_b2_cells


# Helpers

def _cid(ticker: str, endpoint: str) -> str:
    return hashlib.sha256(f"b2-{ticker}-{endpoint}-2026-07-23".encode()).hexdigest()


def _init_v11_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE source_checkpoints (
            run_id               TEXT NOT NULL,
            source_type          TEXT NOT NULL,
            ticker               TEXT NOT NULL,
            date                 TEXT NOT NULL,
            status               TEXT NOT NULL,
            error_class          TEXT,
            retries              INTEGER NOT NULL DEFAULT 0,
            error_message_redacted TEXT,
            http_status          INTEGER,
            retry_after_seconds  REAL,
            provider_latency_ms  REAL,
            raw_asset_id         TEXT,
            items_count          INTEGER,
            fallback_provider    TEXT,
            fallback_triggered   INTEGER DEFAULT 0,
            empty_reason         TEXT,
            logical_fetch_id     TEXT,
            request_count        INTEGER NOT NULL DEFAULT 0,
            pages_received       INTEGER NOT NULL DEFAULT 0,
            items_received       INTEGER NOT NULL DEFAULT 0,
            is_complete          INTEGER NOT NULL DEFAULT 0,
            cell_id              TEXT,
            window_start         TEXT,
            window_end           TEXT,
            endpoint_name        TEXT,
            provider_profile_version TEXT,
            PRIMARY KEY (run_id, source_type, ticker, date)
        )
    """)
    conn.execute("CREATE INDEX idx_source_checkpoints_run ON source_checkpoints(run_id)")
    conn.execute("""
        CREATE UNIQUE INDEX idx_source_checkpoints_run_cell_id
        ON source_checkpoints(run_id, cell_id) WHERE cell_id IS NOT NULL
    """)
    # ingestion_runs for lineage queries
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id TEXT PRIMARY KEY, parent_run_id TEXT, plan_hash TEXT,
            expected_plan_hash TEXT, status TEXT, started_at TEXT, ended_at TEXT
        )
    """)
    for trigger_sql in [
        """CREATE TRIGGER trg_checkpoint_v2_insert_guard
           BEFORE INSERT ON source_checkpoints
           WHEN NEW.logical_fetch_id IS NOT NULL
              AND (NEW.is_complete NOT IN (0, 1) OR NEW.request_count < 0
                   OR NEW.pages_received < 0 OR NEW.items_received < 0)
        BEGIN SELECT RAISE(ABORT, 'checkpoint_v2_contract'); END""",
        """CREATE TRIGGER trg_checkpoint_v2_update_guard
           BEFORE UPDATE ON source_checkpoints
           WHEN (NEW.request_count < 0 OR NEW.pages_received < 0
                 OR NEW.items_received < 0 OR NEW.is_complete NOT IN (0, 1))
        BEGIN SELECT RAISE(ABORT, 'checkpoint_v2_contract'); END""",
    ]:
        conn.execute(trigger_sql)
    conn.execute("PRAGMA user_version = 11")
    conn.commit()


def _make_minimal_plan(cells: list[dict]) -> object:
    """Create a minimal UpdatePlan-like object for _remaining_b2_cells testing."""
    @dataclass
    class MinimalPlan:
        stages: dict = field(default_factory=dict)

    plan = MinimalPlan()
    plan.stages = {"evidence": {"cells": cells}}
    return plan


RUN_ID = "b2-test-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
DATE = "2026-07-23"
SRC_FMP = "fmp_fundamentals"


# Migration tests

class TestV12Migration:
    def test_current_schema_version_matches_registry(self):
        assert CURRENT_SCHEMA_VERSION == max(migration.version for migration in MIGRATIONS)

    def test_defaults_preserved(self):
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        conn.execute("""
            INSERT INTO source_checkpoints
            (checkpoint_id, run_id, source_type, ticker, date, status)
            VALUES ('dflt', 'r', 't', 'T', '2026-01-01', 's')
        """)
        row = conn.execute("""
            SELECT retries, fallback_triggered, request_count, pages_received,
                   items_received, is_complete
            FROM source_checkpoints WHERE checkpoint_id='dflt'
        """).fetchone()
        assert row == (0, 0, 0, 0, 0, 0), f"Defaults: {row}"
        conn.close()


class TestV12SnapshotIdentity:
    @staticmethod
    def _checkpoint_rows() -> list[tuple[str, str, str]]:
        return [
            ("checkpoint-income", "income_statement", "success"),
            ("checkpoint-balance", "balance_sheet", "success"),
            ("checkpoint-cash", "cash_flow", "failed"),
        ]

    @staticmethod
    def _snapshot_hash(rows: list[tuple[str, str, str]]) -> str:
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        for checkpoint_id, endpoint_name, status in rows:
            cell_id = _cid("AAPL", endpoint_name)
            conn.execute(
                """INSERT INTO source_checkpoints (
                       checkpoint_id, run_id, source_type, ticker, date, status,
                       cell_id, window_start, window_end, endpoint_name,
                       provider_profile_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id,
                    RUN_ID,
                    SRC_FMP,
                    "AAPL",
                    DATE,
                    status,
                    cell_id,
                    DATE,
                    DATE,
                    endpoint_name,
                    "v1",
                ),
            )
        spec = SNAPSHOT_TABLE_INVENTORY["source_checkpoints"]
        digest = _table_hash(
            conn,
            "source_checkpoints",
            tuple(spec["key"]),
            tuple(spec["columns"]),
        )
        conn.close()
        return digest

    def test_checkpoint_snapshot_key_is_surrogate_identity(self):
        spec = SNAPSHOT_TABLE_INVENTORY["source_checkpoints"]
        assert spec["key"] == ("checkpoint_id",)
        assert "checkpoint_id" in spec["columns"]

    def test_checkpoint_snapshot_hash_is_insertion_order_independent(self):
        rows = self._checkpoint_rows()
        assert self._snapshot_hash(rows) == self._snapshot_hash(list(reversed(rows)))

    def test_checkpoint_snapshot_hash_changes_with_outcome(self):
        rows = self._checkpoint_rows()
        changed = [
            (checkpoint_id, endpoint, "success" if endpoint == "cash_flow" else status)
            for checkpoint_id, endpoint, status in rows
        ]
        assert self._snapshot_hash(rows) != self._snapshot_hash(changed)

    def test_row_count_preserved(self):
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        conn.execute("INSERT INTO source_checkpoints (run_id, source_type, ticker, date, status) VALUES ('r','t','T','2026-01-01','s')")
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM source_checkpoints").fetchone()[0]
        run_migrations(conn)
        assert conn.execute("SELECT COUNT(*) FROM source_checkpoints").fetchone()[0] == before
        conn.close()

    def test_idempotent(self):
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        assert run_migrations(conn) == CURRENT_SCHEMA_VERSION
        assert run_migrations(conn) == CURRENT_SCHEMA_VERSION
        conn.close()

    def test_rollback_restores_v11(self):
        """Real rollback: inject failure mid-migration, verify v11 intact."""
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        conn.execute("INSERT INTO source_checkpoints (run_id, source_type, ticker, date, status) VALUES ('r','t','T','2026-01-01','s')")
        conn.commit()

        before_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        before_count = conn.execute("SELECT COUNT(*) FROM source_checkpoints").fetchone()[0]
        before_cols = {r[1] for r in conn.execute("PRAGMA table_info(source_checkpoints)").fetchall()}

        # Inject failure: impersonate a broken _apply_migration_v12
        import catalyst_data.migrations as mod
        original = mod._apply_migration_v12
        def _failing(conn_arg):
            conn_arg.execute("DROP TABLE IF EXISTS source_checkpoints_v12")
            conn_arg.execute("CREATE TABLE source_checkpoints_v12 (checkpoint_id TEXT PRIMARY KEY, run_id TEXT)")
            raise RuntimeError("injected rollback test failure")
        mod._apply_migration_v12 = _failing

        try:
            with pytest.raises(RuntimeError, match="injected rollback"):
                run_migrations(conn)
        finally:
            mod._apply_migration_v12 = original

        # Verify v11 restored
        assert conn.execute("PRAGMA user_version").fetchone()[0] == before_ver, "user_version not restored"
        assert conn.execute("SELECT COUNT(*) FROM source_checkpoints").fetchone()[0] == before_count, "rows lost"
        after_cols = {r[1] for r in conn.execute("PRAGMA table_info(source_checkpoints)").fetchall()}
        assert after_cols == before_cols, f"schema changed: {after_cols - before_cols}"
        # v12 table must not linger
        leftover = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='source_checkpoints_v12'").fetchone()[0]
        assert leftover == 0, "source_checkpoints_v12 left behind"

        conn.close()

    def test_schema_columns_exact(self):
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        cols = [c[1] for c in conn.execute("PRAGMA table_info(source_checkpoints)").fetchall()]
        assert cols[:6] == ["checkpoint_id", "run_id", "source_type", "ticker", "date", "status"]
        assert "retries" in cols and "cell_id" in cols and "endpoint_name" in cols
        assert len(cols) == 27
        conn.close()

    def test_triggers_present(self):
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='source_checkpoints'"
        ).fetchall()}
        expected = {"trg_checkpoint_v2_insert_guard", "trg_checkpoint_v2_update_guard",
                     "trg_b2o_checkpoint_cell_identity_insert", "trg_b2o_checkpoint_cell_identity_update",
                     "trg_b2o_checkpoint_cell_identity_update_contract"}
        assert triggers == expected, f"Missing: {expected - triggers}"
        conn.close()


# Checkpoint writer tests

class TestCheckpointWriter:
    def test_three_endpoints_coexist(self):
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        for ep in ["income_statement", "balance_sheet", "cash_flow"]:
            _write_b2_checkpoint(conn, run_id=RUN_ID, source=SRC_FMP, ticker="AAPL", date=DATE,
                status="success", logical_fetch_id=f"lf-{ep}", items_count=5, http_status=200, is_complete=1,
                cell={"cell_id": _cid("AAPL", ep), "window_start": DATE, "window_end": DATE,
                      "endpoint_name": ep, "provider_profile_version": "v1"})
        rows = conn.execute("SELECT endpoint_name FROM source_checkpoints WHERE run_id=? AND source_type=? AND cell_id IS NOT NULL", (RUN_ID, SRC_FMP)).fetchall()
        assert {r[0] for r in rows} == {"income_statement", "balance_sheet", "cash_flow"}
        conn.close()

    def test_update_preserves_siblings(self):
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        for ep in ["income_statement", "balance_sheet", "cash_flow"]:
            _write_b2_checkpoint(conn, run_id=RUN_ID, source=SRC_FMP, ticker="AAPL", date=DATE,
                status="success", logical_fetch_id=f"lf-{ep}", items_count=5, http_status=200, is_complete=1,
                cell={"cell_id": _cid("AAPL", ep), "window_start": DATE, "window_end": DATE,
                      "endpoint_name": ep, "provider_profile_version": "v1"})
        _write_b2_checkpoint(conn, run_id=RUN_ID, source=SRC_FMP, ticker="AAPL", date=DATE,
            status="success", logical_fetch_id="lf-is-v2", items_count=7, http_status=200, is_complete=1,
            cell={"cell_id": _cid("AAPL", "income_statement"), "window_start": DATE, "window_end": DATE,
                  "endpoint_name": "income_statement", "provider_profile_version": "v1"})
        rows = conn.execute("SELECT endpoint_name FROM source_checkpoints WHERE run_id=? AND source_type=? AND cell_id IS NOT NULL", (RUN_ID, SRC_FMP)).fetchall()
        assert len(rows) == 3
        assert {r[0] for r in rows} == {"income_statement", "balance_sheet", "cash_flow"}
        conn.close()

    def test_failed_402_coexists_with_success(self):
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        _write_b2_checkpoint(conn, run_id=RUN_ID, source=SRC_FMP, ticker="AAPL", date=DATE,
            status="success", logical_fetch_id="lf-is", items_count=5, http_status=200, is_complete=1,
            cell={"cell_id": _cid("AAPL", "income_statement"), "window_start": DATE, "window_end": DATE,
                  "endpoint_name": "income_statement", "provider_profile_version": "v1"})
        _write_b2_checkpoint(conn, run_id=RUN_ID, source=SRC_FMP, ticker="AAPL", date=DATE,
            status="failed", logical_fetch_id="lf-bs", items_count=0, http_status=402, is_complete=0,
            cell={"cell_id": _cid("AAPL", "balance_sheet"), "window_start": DATE, "window_end": DATE,
                  "endpoint_name": "balance_sheet", "provider_profile_version": "v1"})
        rows = conn.execute("SELECT endpoint_name, status, is_complete FROM source_checkpoints WHERE run_id=? AND source_type=? AND cell_id IS NOT NULL ORDER BY endpoint_name", (RUN_ID, SRC_FMP)).fetchall()
        assert len(rows) == 2
        bs = [r for r in rows if r[0] == "balance_sheet"][0]
        assert bs[1] == "failed" and bs[2] == 0
        inc = [r for r in rows if r[0] == "income_statement"][0]
        assert inc[1] == "success" and inc[2] == 1
        conn.close()

    def test_identity_different_cell_ids_create_separate_rows(self):
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        _write_b2_checkpoint(conn, run_id=RUN_ID, source=SRC_FMP, ticker="AAPL", date=DATE,
            status="success", logical_fetch_id="lf-is", items_count=5, http_status=200, is_complete=1,
            cell={"cell_id": _cid("AAPL", "income_statement"), "window_start": DATE, "window_end": DATE,
                  "endpoint_name": "income_statement", "provider_profile_version": "v1"})
        _write_b2_checkpoint(conn, run_id=RUN_ID, source=SRC_FMP, ticker="AAPL", date=DATE,
            status="success", logical_fetch_id="lf-cf", items_count=5, http_status=200, is_complete=1,
            cell={"cell_id": _cid("AAPL", "cash_flow"), "window_start": DATE, "window_end": DATE,
                  "endpoint_name": "cash_flow", "provider_profile_version": "v1"})
        rows = conn.execute("SELECT endpoint_name FROM source_checkpoints WHERE run_id=? AND source_type=? AND cell_id IS NOT NULL", (RUN_ID, SRC_FMP)).fetchall()
        assert {r[0] for r in rows} == {"income_statement", "cash_flow"}
        conn.close()

    def test_legacy_writer_still_works(self):
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        _write_b2_checkpoint(conn, run_id="r-legacy", source="polygon_news", ticker="MSFT", date=DATE,
            status="success", logical_fetch_id="lf-pn", items_count=10, http_status=200, is_complete=1, cell=None)
        row = conn.execute("SELECT status, is_complete, items_count FROM source_checkpoints WHERE run_id='r-legacy' AND cell_id IS NULL").fetchone()
        assert row == ("success", 1, 10)
        _write_b2_checkpoint(conn, run_id="r-legacy", source="polygon_news", ticker="MSFT", date=DATE,
            status="success", logical_fetch_id="lf-pn-v2", items_count=15, http_status=200, is_complete=1, cell=None)
        row2 = conn.execute("SELECT logical_fetch_id, items_count FROM source_checkpoints WHERE run_id='r-legacy' AND cell_id IS NULL").fetchone()
        assert row2 == ("lf-pn-v2", 15)
        conn.close()


# Resume/skip tests using production _remaining_b2_cells

class TestResumeSemantics:
    """Tests calling production _remaining_b2_cells with a real plan."""

    def _make_plan(self, cells):
        return _make_minimal_plan(cells)

    def _cell(self, ticker, endpoint, source_type=SRC_FMP):
        return {
            "cell_id": _cid(ticker, endpoint),
            "source_type": source_type,
            "subject": ticker,
            "window_start": DATE,
            "window_end": DATE,
            "endpoint_name": endpoint,
            "provider_profile_version": "v1",
        }

    def _setup_lineage(self, conn, run_id):
        conn.execute("INSERT INTO ingestion_runs (run_id, parent_run_id, status, started_at) VALUES (?, NULL, 'STARTED', '2026-01-01T00:00:00Z')", (run_id,))
        conn.commit()

    def test_completed_cell_skipped(self):
        """_remaining_b2_cells skips completed (success + is_complete=1) cells."""
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        self._setup_lineage(conn, RUN_ID)

        _write_b2_checkpoint(conn, run_id=RUN_ID, source=SRC_FMP, ticker="AAPL", date=DATE,
            status="success", logical_fetch_id="lf-is", items_count=5, http_status=200, is_complete=1,
            cell={"cell_id": _cid("AAPL", "income_statement"), "window_start": DATE, "window_end": DATE,
                  "endpoint_name": "income_statement", "provider_profile_version": "v1"})

        cells = [self._cell("AAPL", "income_statement")]
        plan = self._make_plan(cells)
        remaining = _remaining_b2_cells(conn, plan, "evidence", RUN_ID)
        assert len(remaining) == 0, f"Expected 0 remaining, got {len(remaining)}"
        conn.close()

    def test_fmp_402_skipped(self):
        """FMP cells with HTTP 402 in lineage are skipped (entitlement terminal)."""
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        self._setup_lineage(conn, RUN_ID)

        _write_b2_checkpoint(conn, run_id=RUN_ID, source=SRC_FMP, ticker="AAPL", date=DATE,
            status="failed", logical_fetch_id="lf-bs", items_count=0, http_status=402, is_complete=0,
            cell={"cell_id": _cid("AAPL", "balance_sheet"), "window_start": DATE, "window_end": DATE,
                  "endpoint_name": "balance_sheet", "provider_profile_version": "v1"})

        cells = [self._cell("AAPL", "balance_sheet")]
        plan = self._make_plan(cells)
        remaining = _remaining_b2_cells(conn, plan, "evidence", RUN_ID)
        assert len(remaining) == 0, f"FMP 402 should be skipped, got {len(remaining)}"
        conn.close()

    def test_fmp_403_not_skipped(self):
        """FMP cells with HTTP 403 are NOT skipped (different from 402)."""
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        self._setup_lineage(conn, RUN_ID)

        _write_b2_checkpoint(conn, run_id=RUN_ID, source=SRC_FMP, ticker="AAPL", date=DATE,
            status="failed", logical_fetch_id="lf-bs", items_count=0, http_status=403, is_complete=0,
            cell={"cell_id": _cid("AAPL", "balance_sheet"), "window_start": DATE, "window_end": DATE,
                  "endpoint_name": "balance_sheet", "provider_profile_version": "v1"})

        cells = [self._cell("AAPL", "balance_sheet")]
        plan = self._make_plan(cells)
        remaining = _remaining_b2_cells(conn, plan, "evidence", RUN_ID)
        assert len(remaining) == 1, f"FMP 403 should NOT be skipped, got {len(remaining)}"
        conn.close()

    def test_mandatory_source_402_not_skipped(self):
        """HTTP 402 on mandatory sources (non-FMP) is NOT skipped."""
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        self._setup_lineage(conn, RUN_ID)

        _write_b2_checkpoint(conn, run_id=RUN_ID, source="polygon_news", ticker="MSFT", date=DATE,
            status="failed", logical_fetch_id="lf-pn", items_count=0, http_status=402, is_complete=0, cell=None)

        cells = [self._cell("MSFT", "news", source_type="polygon_news")]
        plan = self._make_plan(cells)
        remaining = _remaining_b2_cells(conn, plan, "evidence", RUN_ID)
        assert len(remaining) == 1, f"Mandatory 402 should NOT be skipped, got {len(remaining)}"
        conn.close()

    def test_mandatory_source_403_not_skipped(self):
        """HTTP 403 on mandatory sources is NOT skipped."""
        conn = sqlite3.connect(":memory:")
        _init_v11_db(conn)
        run_migrations(conn)
        self._setup_lineage(conn, RUN_ID)

        _write_b2_checkpoint(conn, run_id=RUN_ID, source="polygon_news", ticker="MSFT", date=DATE,
            status="failed", logical_fetch_id="lf-pn", items_count=0, http_status=403, is_complete=0, cell=None)

        cells = [self._cell("MSFT", "news", source_type="polygon_news")]
        plan = self._make_plan(cells)
        remaining = _remaining_b2_cells(conn, plan, "evidence", RUN_ID)
        assert len(remaining) == 1, f"Mandatory 403 should NOT be skipped, got {len(remaining)}"
        conn.close()


# Audit regression tests

class TestAuditNoSourceTypeErrors:
    def test_filings_no_source_type_column(self):
        from catalyst_data.coverage_audit import _TABLE_SOURCE_COL
        assert _TABLE_SOURCE_COL["filings"] is None

    def test_filing_documents_no_source_type_column(self):
        from catalyst_data.coverage_audit import _TABLE_SOURCE_COL
        assert _TABLE_SOURCE_COL["filing_documents"] is None

    def test_d1_counts_filings_as_total(self):
        """With a complete minimal schema, filings are counted as total (no source_type)."""
        import sqlite3
        db = sqlite3.connect(":memory:")
        for tbl, ddl in [
            ("raw_assets", "CREATE TABLE raw_assets (asset_id TEXT, source_type TEXT)"),
            ("clean_assets", "CREATE TABLE clean_assets (asset_id TEXT, source_type TEXT)"),
            ("articles", "CREATE TABLE articles (article_id TEXT, source_type TEXT)"),
            ("article_tickers", "CREATE TABLE article_tickers (article_id TEXT, ticker TEXT)"),
            ("filings", "CREATE TABLE filings (accession_number TEXT, ticker TEXT)"),
            ("filing_documents", "CREATE TABLE filing_documents (doc_id TEXT)"),
            ("macro_observations", "CREATE TABLE macro_observations (series_id TEXT)"),
            ("index_state", "CREATE TABLE index_state (chunk_id TEXT, source_kind TEXT)"),
            ("index_manifests", "CREATE TABLE index_manifests (manifest_id TEXT)"),
            ("source_checkpoints", "CREATE TABLE source_checkpoints (run_id TEXT, source_type TEXT, ticker TEXT, date TEXT, status TEXT)"),
            ("ohlcv", "CREATE TABLE ohlcv (symbol TEXT, date TEXT)"),
        ]:
            db.execute(ddl)
        db.execute("INSERT INTO filings VALUES ('0001', 'AAPL')")
        db.commit()
        from catalyst_data.coverage_audit import _d1_per_source_table_counts
        report = {}
        result = _d1_per_source_table_counts(db, report)
        assert "filings" in result and "_total" in result["filings"], f"filings._total missing: {result.get("filings")}"
        assert "ERROR" not in str(result), f"ERROR in result: {result}"
        db.close()
