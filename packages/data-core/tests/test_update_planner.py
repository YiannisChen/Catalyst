"""Tests for update_planner — zero-write planning (W1-A)."""
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from catalyst_data.update_planner import (
    ActiveWALForPlanning,
    FrozenDBError,
    SchemaOutOfDate,
    UpdatePlan,
    plan_update,
)

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

MINIMAL_SCHEMA = {
    "ohlcv": """CREATE TABLE ohlcv (
        symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, source TEXT)""",
    "source_checkpoints": """CREATE TABLE source_checkpoints (
        run_id TEXT, source_type TEXT, ticker TEXT, date TEXT,
        status TEXT, error_class TEXT, retries INTEGER DEFAULT 0,
        error_message_redacted TEXT, http_status INTEGER,
        retry_after_seconds REAL, provider_latency_ms REAL,
        raw_asset_id TEXT, items_count INTEGER,
        fallback_provider TEXT, fallback_triggered INTEGER DEFAULT 0)""",
}

_INGESTION_RUNS_DDL = """CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
    ticker_list_json TEXT NOT NULL, source_list_json TEXT NOT NULL,
    status TEXT NOT NULL, success_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0, cost_usd REAL, notes TEXT,
    current_source TEXT, current_ticker TEXT, current_date TEXT,
    cells_total INTEGER DEFAULT 0, cells_done INTEGER DEFAULT 0,
    canceled_at TEXT, report_path TEXT, run_config_json TEXT)"""


def create_fixture_db(tmp_path, *, tables=None, extra_sql=None):
    db_path = str(tmp_path / "fixture.db")
    conn = sqlite3.connect(db_path)
    names = tables or list(MINIMAL_SCHEMA.keys())
    for name in names:
        if name in MINIMAL_SCHEMA:
            conn.execute(MINIMAL_SCHEMA[name])
    if extra_sql:
        for stmt in extra_sql:
            conn.execute(stmt)
    conn.commit()
    conn.close()
    return db_path


def create_full_fixture(tmp_path, *, extra_sql=None):
    db_path = str(tmp_path / "fixture.db")
    conn = sqlite3.connect(db_path)
    for name in MINIMAL_SCHEMA:
        conn.execute(MINIMAL_SCHEMA[name])
    conn.execute(_INGESTION_RUNS_DDL)
    if extra_sql:
        for stmt in extra_sql:
            conn.execute(stmt)
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# SchemaOutOfDate
# ---------------------------------------------------------------------------


class TestSchemaOutOfDate:
    def test_empty_db_raises_schema_out_of_date(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE irrelevant (x INT)")
            conn.commit(); conn.close()
            with pytest.raises(SchemaOutOfDate) as exc_info:
                plan_update(db_path)
            assert "ohlcv" in str(exc_info.value).lower()
        finally:
            os.unlink(db_path)

    def test_missing_required_column_raises(self, tmp_path):
        db_path = str(tmp_path / "fixture.db")
        conn = sqlite3.connect(db_path)
        conn.execute(MINIMAL_SCHEMA["ohlcv"])
        conn.execute("CREATE TABLE source_checkpoints (run_id TEXT, source_type TEXT)")
        conn.commit(); conn.close()
        with pytest.raises(SchemaOutOfDate):
            plan_update(db_path)


# ---------------------------------------------------------------------------
# Read-only + immutable enforcement
# ---------------------------------------------------------------------------


class TestReadOnlyEnforcement:
    def test_planner_opens_immutable_read_only(self, tmp_path):
        """Assert planner uses mode=ro&immutable=1 via uri=True."""
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        original_connect = sqlite3.connect
        captured = []
        def _capturing(*args, **kwargs):
            captured.append((args, kwargs))
            return original_connect(*args, **kwargs)

        import catalyst_data.update_planner as up
        with patch.object(up.sqlite3, "connect", side_effect=_capturing):
            plan = up.plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])
        assert plan.plan_hash

        planner_calls = [c for c in captured if "mode=ro" in str(c[0])]
        assert planner_calls
        args, kwargs = planner_calls[0]
        assert kwargs.get("uri") is True
        uri = args[0]
        assert "mode=ro" in uri
        assert "immutable=1" in uri, f"missing immutable=1 in URI: {uri}"

    def test_planner_makes_no_network_calls(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        original = socket.socket
        def _block(*a, **kw):
            raise RuntimeError("network blocked")
        socket.socket = _block
        try:
            plan = plan_update(db_path, use_ohlcv_universe=True)
            assert plan.plan_hash
        finally:
            socket.socket = original

    def test_planner_source_has_no_write_imports(self):
        import catalyst_data.update_planner as up
        src = Path(up.__file__).read_text()
        for token in ["open_ingestion_run", "write_source_checkpoint",
                       "ensure_ingestion_quality_tables", "init_db",
                       "save_run_report", "close_ingestion_run"]:
            assert token not in src, f"planner imports {token}"


# ---------------------------------------------------------------------------
# WAL-mode tests
# ---------------------------------------------------------------------------


class TestWALMode:
    def test_wal_mode_copy_creates_no_sidecars(self, tmp_path):
        """Create WAL-mode DB, checkpoint close, copy only main file,
        verify planner creates no sidecars."""
        # Create a WAL-mode DB with checkpointed close
        src_db = str(tmp_path / "src.db")
        conn = sqlite3.connect(src_db)
        conn.execute("PRAGMA journal_mode=WAL")
        for name in MINIMAL_SCHEMA:
            conn.execute(MINIMAL_SCHEMA[name])
        conn.execute("INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

        # Copy only the main DB file to a fresh directory
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dest_db = str(dest_dir / "plan.db")
        shutil.copy2(src_db, dest_db)

        before_sha = hashlib.sha256(Path(dest_db).read_bytes()).hexdigest()
        before_size = os.path.getsize(dest_db)
        before_mtime = os.path.getmtime(dest_db)
        before_files = set(os.listdir(str(dest_dir)))

        plan = plan_update(dest_db, tickers=["AAPL"], sources=["polygon_news"])
        assert plan.plan_hash

        after_sha = hashlib.sha256(Path(dest_db).read_bytes()).hexdigest()
        after_size = os.path.getsize(dest_db)
        after_mtime = os.path.getmtime(dest_db)
        after_files = set(os.listdir(str(dest_dir)))

        assert before_sha == after_sha
        assert before_size == after_size
        assert before_mtime == after_mtime
        assert not (after_files - before_files)
        # Prove no -wal/-shm/-journal created
        assert not Path(dest_db + "-wal").exists()
        assert not Path(dest_db + "-shm").exists()
        assert not Path(dest_db + "-journal").exists()

    def test_nonempty_wal_fails_closed(self, tmp_path):
        """Non-empty WAL -> ActiveWALForPlanning; nothing mutated."""
        db_path = str(tmp_path / "wal.db")
        # Create DB with non-empty WAL via subprocess that exits without close
        code = (
            "import sqlite3 as _s, sys as _sys, os as _os\n"
            "_db = _sys.argv[1]\n"
            "_c = _s.connect(_db)\n"
            "_c.execute('PRAGMA journal_mode=WAL')\n"
            "_c.execute('PRAGMA wal_autocheckpoint=0')\n"
            "_c.execute('CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)')\n"
            "_c.execute(\"CREATE TABLE source_checkpoints (run_id TEXT, source_type TEXT, ticker TEXT, date TEXT, status TEXT, error_class TEXT, retries INTEGER DEFAULT 0, error_message_redacted TEXT, http_status INTEGER, retry_after_seconds REAL, provider_latency_ms REAL, raw_asset_id TEXT, items_count INTEGER, fallback_provider TEXT, fallback_triggered INTEGER DEFAULT 0)\")\n"
            "_c.execute(\"INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')\")\n"
            "_c.commit()\n"
            "for _i in range(100):\n"
            "    _c.execute('INSERT INTO ohlcv VALUES (?,?,0,0,0,0,0,\"\")', (str(_i),'2026-01-01'))\n"
            "_c.commit()\n"
            "_os._exit(0)\n"
        )
        subprocess.run([sys.executable, "-c", code, db_path], check=True)
        wal_path = Path(db_path + "-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0

        before_db = Path(db_path).read_bytes()
        before_wal = wal_path.read_bytes()
        before_files = set(os.listdir(str(tmp_path)))

        with pytest.raises(ActiveWALForPlanning):
            plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])

        after_db = Path(db_path).read_bytes()
        after_wal = wal_path.read_bytes()
        after_files = set(os.listdir(str(tmp_path)))
        assert before_db == after_db
        assert before_wal == after_wal
        assert before_files == after_files

    def test_zero_byte_wal_allows_immutable_read(self, tmp_path):
        """Zero-byte WAL -> planner succeeds, no mutation to DB or WAL."""
        db_path = str(tmp_path / "wal0.db")
        code = (
            "import sqlite3 as _s, sys as _sys\n"
            "_db = _sys.argv[1]\n"
            "_c = _s.connect(_db)\n"
            "_c.execute('PRAGMA journal_mode=WAL')\n"
            "_c.execute('CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)')\n"
            "_c.execute(\"CREATE TABLE source_checkpoints (run_id TEXT, source_type TEXT, ticker TEXT, date TEXT, status TEXT, error_class TEXT, retries INTEGER DEFAULT 0, error_message_redacted TEXT, http_status INTEGER, retry_after_seconds REAL, provider_latency_ms REAL, raw_asset_id TEXT, items_count INTEGER, fallback_provider TEXT, fallback_triggered INTEGER DEFAULT 0)\")\n"
            "_c.execute(\"INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')\")\n"
            "_c.commit()\n"
            "_c.execute('PRAGMA wal_checkpoint(TRUNCATE)')\n"
            "_c.close()\n"
        )
        subprocess.run([sys.executable, "-c", code, db_path], check=True)
        wal_path = Path(db_path + "-wal")
        wal_existed = wal_path.exists()
        wal_bytes_before = wal_path.read_bytes() if wal_existed else None
        shm_path = Path(db_path + "-shm")

        before_db = Path(db_path).read_bytes()
        before_mtime = os.path.getmtime(db_path)
        before_files = set(os.listdir(str(tmp_path)))

        plan = plan_update(db_path, use_ohlcv_universe=True)
        assert plan.plan_hash

        after_db = Path(db_path).read_bytes()
        after_mtime = os.path.getmtime(db_path)
        after_files = set(os.listdir(str(tmp_path)))

        assert before_db == after_db
        assert before_mtime == after_mtime
        assert before_files == after_files
        if wal_existed:
            assert wal_path.read_bytes() == wal_bytes_before
        # No SHM created
        assert not shm_path.exists()


# ---------------------------------------------------------------------------
# PlanUpdateBasic
# ---------------------------------------------------------------------------


class TestPlanUpdateBasic:
    def test_plan_update_returns_update_plan(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        plan = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"])
        assert isinstance(plan, UpdatePlan)
        assert len(plan.plan_hash) == 64
        assert plan.universe["provenance"] == "explicit-config"

    def test_default_universe_emits_fallback_warning(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        plan = plan_update(db_path, use_ohlcv_universe=True)
        assert plan.universe["provenance"] == "ohlcv-derived-fallback"
        assert any(w["type"] == "universe-fallback" for w in plan.warnings)

    def test_empty_universe_returns_plan_with_zero_cells(self, tmp_path):
        db_path = create_fixture_db(tmp_path)
        plan = plan_update(db_path, use_ohlcv_universe=True)
        assert plan.universe["tickers"] == []
        assert plan.plan_hash


# ---------------------------------------------------------------------------
# PlanHashDeterminism
# ---------------------------------------------------------------------------


class TestPlanHashDeterminism:
    @pytest.fixture
    def fixture_db(self, tmp_path):
        return create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
            "INSERT INTO ohlcv VALUES ('MSFT','2026-07-09',400.0,405.0,398.0,403.0,30000000,'polygon')",
        ])

    def test_identical_inputs_identical_hash(self, fixture_db):
        p1 = plan_update(fixture_db, tickers=["AAPL"], sources=["polygon_news"])
        p2 = plan_update(fixture_db, tickers=["AAPL"], sources=["polygon_news"])
        assert p1.plan_hash == p2.plan_hash

    def test_hash_excludes_created_at(self, fixture_db, monkeypatch):
        from datetime import datetime, timezone
        call_count = [0]
        times = [
            datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 12, 11, 0, 0, tzinfo=timezone.utc),
        ]
        def fake_now(tz=None):
            t = times[min(call_count[0], 1)]
            call_count[0] += 1
            return t
        monkeypatch.setattr("catalyst_data.update_planner.datetime", type(
            "FakeDT", (), {"now": staticmethod(fake_now), "timezone": timezone}
        )())
        p1 = plan_update(fixture_db, tickers=["AAPL"], sources=["polygon_news"])
        p2 = plan_update(fixture_db, tickers=["AAPL"], sources=["polygon_news"])
        assert p1.created_at != p2.created_at
        assert p1.plan_hash == p2.plan_hash

    def test_checkpoint_flip_changes_hash(self, fixture_db):
        p1 = plan_update(fixture_db, tickers=["AAPL"], sources=["polygon_news"])
        conn = sqlite3.connect(fixture_db)
        conn.execute(
            "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status) "
            "VALUES ('t','polygon_news','AAPL','2026-07-09','success')"
        )
        conn.commit(); conn.close()
        p2 = plan_update(fixture_db, tickers=["AAPL"], sources=["polygon_news"])
        assert p1.plan_hash != p2.plan_hash

    def test_different_tickers_different_hashes(self, fixture_db):
        """Same DB, same sources/dates, different tickers -> different hash."""
        before_db = Path(fixture_db).read_bytes()
        p1 = plan_update(fixture_db, tickers=["AAPL"], sources=["polygon_news"])
        p2 = plan_update(fixture_db, tickers=["MSFT"], sources=["polygon_news"])
        after_db = Path(fixture_db).read_bytes()
        assert p1.plan_hash != p2.plan_hash
        assert before_db == after_db, "DB bytes changed"


# ---------------------------------------------------------------------------
# Zero-write proof
# ---------------------------------------------------------------------------


class TestZeroWriteProof:
    def test_planner_does_not_modify_db(self, tmp_path):
        db_path = create_full_fixture(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
            "INSERT INTO ingestion_runs (run_id,started_at,ticker_list_json,source_list_json,status) "
            "VALUES ('r1','2026-01-01T00:00:00','[\"AAPL\"]','[\"polygon_news\"]','completed')",
        ])
        db_file = Path(db_path)
        report_dir = tmp_path / "run_reports"
        report_dir.mkdir()

        before_sha = hashlib.sha256(db_file.read_bytes()).hexdigest()
        before_size = db_file.stat().st_size
        before_mtime_ns = db_file.stat().st_mtime_ns
        before_files = set(os.listdir(str(tmp_path)))
        wal_path = Path(str(db_path) + "-wal")
        shm_path = Path(str(db_path) + "-shm")
        wal_existed = wal_path.exists()
        shm_existed = shm_path.exists()

        pre_conn = sqlite3.connect(db_path)
        before_ir = pre_conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]
        pre_conn.close()

        plan = plan_update(db_path, use_ohlcv_universe=True)

        after_sha = hashlib.sha256(db_file.read_bytes()).hexdigest()
        after_size = db_file.stat().st_size
        after_mtime_ns = db_file.stat().st_mtime_ns
        after_files = set(os.listdir(str(tmp_path)))

        post_conn = sqlite3.connect(db_path)
        after_ir = post_conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]
        post_conn.close()

        assert before_sha == after_sha
        assert before_size == after_size
        assert before_mtime_ns == after_mtime_ns
        assert not (after_files - before_files)
        assert wal_path.exists() == wal_existed
        assert shm_path.exists() == shm_existed
        assert before_ir == after_ir
        assert len(list(report_dir.glob("*.json"))) == 0
        assert plan.plan_hash


# ---------------------------------------------------------------------------
# Dry-run delegation
# ---------------------------------------------------------------------------


class TestDryRunDelegation:
    @pytest.fixture
    def fixture_db(self, tmp_path):
        return create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])

    def test_run_update_batch_dry_run_mode(self, fixture_db):
        import asyncio
        from catalyst_data.update_pipeline import run_update_batch
        with pytest.deprecated_call():
            result = asyncio.run(
                run_update_batch(db_path=fixture_db, tickers=["AAPL"],
                                 sources=["polygon_news"], dry_run=True))
        assert result["mode"] == "dry-run"
        assert result.get("plan_preview") is True
        assert "plan_hash" in result

    def test_run_update_dry_run_mode(self, fixture_db):
        from catalyst_data.run_report import RunConfig
        from catalyst_data.update_pipeline import run_update
        config = RunConfig(db_path=fixture_db, tickers=["AAPL"],
                          sources=["polygon_news"], dry_run=True)
        with pytest.deprecated_call():
            result = run_update(config)
        assert result.mode == "dry-run"
        assert result.config.get("plan_preview") is True
        assert "plan_hash" in result.config

# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------


class TestPathHandling:
    def test_relative_db_path_works(self, tmp_path, monkeypatch):
        """plan_update with relative path succeeds, no sidecars, DB unchanged."""
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        db_name = os.path.basename(db_path)
        db_file = Path(db_path)
        before_bytes = db_file.read_bytes()
        before_mtime = db_file.stat().st_mtime

        orig_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            plan = plan_update(db_name, tickers=["AAPL"], sources=["polygon_news"])
        finally:
            os.chdir(orig_cwd)

        assert plan.plan_hash
        after_bytes = db_file.read_bytes()
        after_mtime = db_file.stat().st_mtime
        assert before_bytes == after_bytes
        assert before_mtime == after_mtime
        # No sidecars
        for ext in ["-wal", "-shm", "-journal"]:
            assert not os.path.exists(db_path + ext), f"sidecar {ext} created"

    def test_path_with_spaces_and_reserved_chars(self, tmp_path):
        """DB path with spaces and # -> planner succeeds, zero sidecars."""
        db_dir = tmp_path / "my data #1"
        db_dir.mkdir()
        db_path = str(db_dir / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute(MINIMAL_SCHEMA["ohlcv"])
        conn.execute(MINIMAL_SCHEMA["source_checkpoints"])
        conn.execute("INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')")
        conn.commit(); conn.close()

        before_files = set(os.listdir(str(db_dir)))
        before_bytes = Path(db_path).read_bytes()
        before_mtime = os.path.getmtime(db_path)

        plan = plan_update(db_path, use_ohlcv_universe=True)

        after_files = set(os.listdir(str(db_dir)))
        after_bytes = Path(db_path).read_bytes()
        after_mtime = os.path.getmtime(db_path)

        assert plan.plan_hash
        assert before_bytes == after_bytes
        assert before_mtime == after_mtime
        assert before_files == after_files

    def test_missing_db_path_fails_without_creating_files(self, tmp_path):
        """Nonexistent relative path -> FileNotFoundError, no files created."""
        db_dir = tmp_path / "empty_dir"
        db_dir.mkdir()
        nonexistent = str(db_dir / "nope.db")
        before_files = set(os.listdir(str(db_dir)))

        orig_cwd = os.getcwd()
        try:
            os.chdir(str(db_dir))
            with pytest.raises((FileNotFoundError, RuntimeError)):
                plan_update("nope.db", tickers=["AAPL"], sources=["polygon_news"])
        finally:
            os.chdir(orig_cwd)

        after_files = set(os.listdir(str(db_dir)))
        assert before_files == after_files



# ---------------------------------------------------------------------------
# W1-B: Per-ticker windows + universe provenance
# ---------------------------------------------------------------------------


class TestPerTickerWindows:
    @pytest.fixture
    def fixture_db(self, tmp_path):
        return create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
            "INSERT INTO ohlcv VALUES ('JPM','2026-05-01',190.0,192.0,188.0,191.0,20000000,'polygon')",
        ])

    def test_stale_ticker_does_not_constrain_current(self, fixture_db):
        """JPM at 2026-05-01 should not limit AAPL's window."""
        plan = plan_update(fixture_db, tickers=["AAPL", "JPM"],
                          sources=["polygon_ohlcv"], reference_today="2026-07-11")
        aapl_dates = sorted({c[1] for c in plan.stages["market"]["cells"] if c[0] == "AAPL"})
        jpm_dates = sorted({c[1] for c in plan.stages["market"]["cells"] if c[0] == "JPM"})
        # Both tickers planned; JPM has cells (not excluded by stale watermark)
        assert len(jpm_dates) > 0
        assert len(aapl_dates) > 0
        # Global window starts at or before JPM's next session (2026-05-04)
        assert jpm_dates[0] <= '2026-05-04'

    def test_absent_ticker_uses_historical_start(self, fixture_db):
        """NVDA not in ohlcv → plans from HISTORICAL_START."""
        plan = plan_update(fixture_db, tickers=["NVDA"], sources=["polygon_ohlcv"],
                          reference_today="2026-07-11")
        nvda_cells = [c for c in plan.stages["market"]["cells"]]
        assert len(nvda_cells) > 0

    def test_deleting_ohlcv_does_not_shrink_calendar(self, tmp_path):
        """Deleting OHLCV row lowers measured state but doesn't shrink calendar."""
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-08',220.0,225.0,219.0,224.0,50000000,'polygon')",
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',221.0,226.0,220.0,225.0,50000000,'polygon')",
        ])
        plan1 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_ohlcv"],
                           reference_today="2026-07-11")
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM ohlcv WHERE date='2026-07-09'")
        conn.commit(); conn.close()
        plan2 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_ohlcv"],
                           reference_today="2026-07-11")
        assert plan1.latest_closed_session == plan2.latest_closed_session
        # More cells planned after deletion (more missing)
        assert plan2.stages["market"]["count"] >= plan1.stages["market"]["count"]


class TestUniverseProvenance:
    def test_configured_universe_is_default(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
            "INSERT INTO ohlcv VALUES ('XYZ','2026-07-09',10.0,11.0,9.0,10.0,100,'polygon')",
        ])
        # Default: uses TICKER_UNIVERSE, not ohlcv-derived
        plan = plan_update(db_path)
        assert plan.universe["provenance"] == "explicit-config"
        from catalyst_data.config import TICKER_UNIVERSE
        assert set(plan.universe["tickers"]) == set(TICKER_UNIVERSE)
        # XYZ not in TICKER_UNIVERSE → excluded
        assert "XYZ" not in plan.universe["tickers"]

    def test_explicit_tickers_override_configured(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('TSLA','2026-07-09',250.0,255.0,248.0,253.0,40000000,'polygon')",
        ])
        plan = plan_update(db_path, tickers=["TSLA"], sources=["polygon_news"])
        assert plan.universe["provenance"] == "explicit-config"
        assert plan.universe["tickers"] == ["TSLA"]

    def test_compat_flag_required_for_ohlcv_derived(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
            "INSERT INTO ohlcv VALUES ('META','2026-07-09',500.0,505.0,498.0,503.0,30000000,'polygon')",
        ])
        # Without compat flag: TICKER_UNIVERSE (all 10)
        plan1 = plan_update(db_path)
        assert plan1.universe["provenance"] == "explicit-config"
        assert len(plan1.universe["tickers"]) == 10
        # With compat flag: ohlcv-derived (2)
        plan2 = plan_update(db_path, use_ohlcv_universe=True)
        assert plan2.universe["provenance"] == "ohlcv-derived-fallback"
        assert set(plan2.universe["tickers"]) == {"AAPL", "META"}
        assert any(w["type"] == "universe-fallback" for w in plan2.warnings)


class TestPlanHashCompleteness:
    """B2 — plan_hash excludes runtime fields; PlanDriftError on mismatch."""

    def test_plan_hash_excludes_runtime_fields(self):
        """plan_hash is deterministic and unchanged when runtime fields change."""
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

        plan = UpdatePlan(
            universe={"tickers": ["AAPL"]},
            reference_today="2026-01-15",
            latest_closed_session="2026-01-14",
            config={"sources": ["polygon_news"]},
        )
        h1 = compute_plan_hash(plan)
        # Change runtime fields → hash unchanged
        plan.created_at = "2099-01-01T00:00:00Z"
        plan.db_path = "/different/path"
        h2 = compute_plan_hash(plan)
        assert h1 == h2, (
            f"plan_hash must be invariant under runtime-field changes: {h1} != {h2}"
        )

    def test_plan_hash_changes_with_universe(self):
        """plan_hash changes when universe changes."""
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

        plan1 = UpdatePlan(
            universe={"tickers": ["AAPL"]},
            reference_today="2026-01-15",
            latest_closed_session="2026-01-14",
        )
        plan2 = UpdatePlan(
            universe={"tickers": ["MSFT"]},
            reference_today="2026-01-15",
            latest_closed_session="2026-01-14",
        )
        assert compute_plan_hash(plan1) != compute_plan_hash(plan2)

    def test_plan_drift_error_exists(self):
        """PlanDriftError is importable from update_planner."""
        from catalyst_data.update_planner import PlanDriftError
        assert issubclass(PlanDriftError, Exception)

    def test_expected_plan_hash_field_exists(self):
        """UpdatePlan has expected_plan_hash field."""
        from catalyst_data.update_planner import UpdatePlan
        plan = UpdatePlan()
        assert hasattr(plan, "expected_plan_hash")

    def test_plan_drift_error_raised_on_mismatch(self):
        """When expected_plan_hash differs from computed plan_hash, PlanDriftError is raised."""
        from catalyst_data.update_planner import UpdatePlan, compute_plan_hash, PlanDriftError, _check_plan_drift

        plan = UpdatePlan(
            universe={"tickers": ["AAPL"]},
            reference_today="2026-01-15",
            latest_closed_session="2026-01-14",
        )
        plan.plan_hash = compute_plan_hash(plan)
        plan.expected_plan_hash = "0000000000000000000000000000000000000000000000000000000000000000"

        with pytest.raises(PlanDriftError):
            _check_plan_drift(plan)
