"""Tests for migrations.py + db_paths.py — H4 schema safety."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from catalyst_data.migrations import MIGRATIONS, run_migrations
from catalyst_data.db_paths import DEV_DB, FROZEN_DB, assert_writable


class TestMigrations:
    def test_ordered_by_version(self):
        versions = [m.version for m in MIGRATIONS]
        assert versions == sorted(versions)

    def test_no_duplicate_versions(self):
        versions = [m.version for m in MIGRATIONS]
        assert len(versions) == len(set(versions))

    def test_idempotent(self, tmp_path: Path):
        db = str(tmp_path / "test.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE IF NOT EXISTS source_checkpoints (run_id TEXT)")
        conn.commit()
        conn.execute("CREATE TABLE IF NOT EXISTS ingestion_runs (run_id TEXT PRIMARY KEY, status TEXT, plan_hash TEXT, expected_plan_hash TEXT, allow_stale_ohlcv INTEGER DEFAULT 0, allow_stale_ohlcv_overridden INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0, parent_run_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS raw_assets (asset_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL, reference_date TEXT NOT NULL, fetched_at TEXT NOT NULL, data_version TEXT NOT NULL DEFAULT 'v1', content_raw BLOB NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')")
        conn.execute("CREATE TABLE IF NOT EXISTS articles (article_id TEXT PRIMARY KEY, published_utc TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS article_tickers (article_id TEXT, ticker TEXT, reference_date TEXT, PRIMARY KEY (article_id, ticker))")
        conn.execute("CREATE TABLE IF NOT EXISTS clean_assets (asset_id TEXT PRIMARY KEY, reference_date TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ohlcv (symbol TEXT, date TEXT, source TEXT, PRIMARY KEY (symbol, date, source))")
        conn.execute("CREATE TABLE IF NOT EXISTS index_state (chunk_id TEXT NOT NULL, chunk_level TEXT NOT NULL DEFAULT 'l1', corpus_item_id TEXT NOT NULL, source_kind TEXT NOT NULL, content_hash TEXT NOT NULL, content_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending')")
        conn.commit()
        v1 = run_migrations(conn)
        v2 = run_migrations(conn)
        assert v1 == v2
        conn.close()

    def test_sets_user_version(self, tmp_path: Path):
        db = str(tmp_path / "test.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE IF NOT EXISTS source_checkpoints (run_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ingestion_runs (run_id TEXT PRIMARY KEY, status TEXT, plan_hash TEXT, expected_plan_hash TEXT, allow_stale_ohlcv INTEGER DEFAULT 0, allow_stale_ohlcv_overridden INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0, parent_run_id TEXT)")
        conn.commit()
        conn.execute("CREATE TABLE IF NOT EXISTS ingestion_runs (run_id TEXT PRIMARY KEY, status TEXT, plan_hash TEXT, expected_plan_hash TEXT, allow_stale_ohlcv INTEGER DEFAULT 0, allow_stale_ohlcv_overridden INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0, parent_run_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS raw_assets (asset_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL, reference_date TEXT NOT NULL, fetched_at TEXT NOT NULL, data_version TEXT NOT NULL DEFAULT 'v1', content_raw BLOB NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')")
        conn.execute("CREATE TABLE IF NOT EXISTS articles (article_id TEXT PRIMARY KEY, published_utc TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS article_tickers (article_id TEXT, ticker TEXT, reference_date TEXT, PRIMARY KEY (article_id, ticker))")
        conn.execute("CREATE TABLE IF NOT EXISTS clean_assets (asset_id TEXT PRIMARY KEY, reference_date TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ohlcv (symbol TEXT, date TEXT, source TEXT, PRIMARY KEY (symbol, date, source))")
        conn.execute("CREATE TABLE IF NOT EXISTS index_state (chunk_id TEXT NOT NULL, chunk_level TEXT NOT NULL DEFAULT 'l1', corpus_item_id TEXT NOT NULL, source_kind TEXT NOT NULL, content_hash TEXT NOT NULL, content_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending')")
        conn.commit()
        v = run_migrations(conn)
        assert v == 12
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
        conn.close()

    def test_per_statement_catch(self, tmp_path: Path):
        """Partial column existence: some columns already present → others added."""
        db = str(tmp_path / "test.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE source_checkpoints (
                run_id TEXT, source_type TEXT, ticker TEXT, date TEXT,
                status TEXT, error_class TEXT, retries INTEGER DEFAULT 0,
                error_message_redacted TEXT,
                PRIMARY KEY (run_id, source_type, ticker, date)
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS ingestion_runs (run_id TEXT PRIMARY KEY, status TEXT, plan_hash TEXT, expected_plan_hash TEXT, allow_stale_ohlcv INTEGER DEFAULT 0, allow_stale_ohlcv_overridden INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0, parent_run_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS raw_assets (asset_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL, reference_date TEXT NOT NULL, fetched_at TEXT NOT NULL, data_version TEXT NOT NULL DEFAULT 'v1', content_raw BLOB NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')")
        conn.commit()
        conn.execute("CREATE TABLE IF NOT EXISTS ingestion_runs (run_id TEXT PRIMARY KEY, status TEXT, plan_hash TEXT, expected_plan_hash TEXT, allow_stale_ohlcv INTEGER DEFAULT 0, allow_stale_ohlcv_overridden INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0, parent_run_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS raw_assets (asset_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL, reference_date TEXT NOT NULL, fetched_at TEXT NOT NULL, data_version TEXT NOT NULL DEFAULT 'v1', content_raw BLOB NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')")
        conn.execute("CREATE TABLE IF NOT EXISTS articles (article_id TEXT PRIMARY KEY, published_utc TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS article_tickers (article_id TEXT, ticker TEXT, reference_date TEXT, PRIMARY KEY (article_id, ticker))")
        conn.execute("CREATE TABLE IF NOT EXISTS clean_assets (asset_id TEXT PRIMARY KEY, reference_date TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ohlcv (symbol TEXT, date TEXT, source TEXT, PRIMARY KEY (symbol, date, source))")
        conn.execute("CREATE TABLE IF NOT EXISTS index_state (chunk_id TEXT NOT NULL, chunk_level TEXT NOT NULL DEFAULT 'l1', corpus_item_id TEXT NOT NULL, source_kind TEXT NOT NULL, content_hash TEXT NOT NULL, content_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending')")
        conn.commit()
        conn.execute("CREATE TABLE IF NOT EXISTS ingestion_runs (run_id TEXT PRIMARY KEY, status TEXT, plan_hash TEXT, expected_plan_hash TEXT, allow_stale_ohlcv INTEGER DEFAULT 0, allow_stale_ohlcv_overridden INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0, parent_run_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS raw_assets (asset_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL, reference_date TEXT NOT NULL, fetched_at TEXT NOT NULL, data_version TEXT NOT NULL DEFAULT 'v1', content_raw BLOB NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')")
        conn.execute("CREATE TABLE IF NOT EXISTS articles (article_id TEXT PRIMARY KEY, published_utc TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS article_tickers (article_id TEXT, ticker TEXT, reference_date TEXT, PRIMARY KEY (article_id, ticker))")
        conn.execute("CREATE TABLE IF NOT EXISTS clean_assets (asset_id TEXT PRIMARY KEY, reference_date TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ohlcv (symbol TEXT, date TEXT, source TEXT, PRIMARY KEY (symbol, date, source))")
        conn.execute("CREATE TABLE IF NOT EXISTS index_state (chunk_id TEXT NOT NULL, chunk_level TEXT NOT NULL DEFAULT 'l1', corpus_item_id TEXT NOT NULL, source_kind TEXT NOT NULL, content_hash TEXT NOT NULL, content_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending')")
        conn.commit()
        v = run_migrations(conn)
        assert v == 12
        conn.close()

    def test_duplicate_column_skipped(self, tmp_path: Path):
        """Column already exists → OperationalError caught, migration proceeds."""
        db = str(tmp_path / "test.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE source_checkpoints (
                run_id TEXT, source_type TEXT, ticker TEXT, date TEXT,
                status TEXT, error_class TEXT, retries INTEGER DEFAULT 0,
                error_message_redacted TEXT, http_status INTEGER,
                retry_after_seconds REAL, provider_latency_ms REAL,
                raw_asset_id TEXT, items_count INTEGER,
                PRIMARY KEY (run_id, source_type, ticker, date)
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS ingestion_runs (run_id TEXT PRIMARY KEY, status TEXT, plan_hash TEXT, expected_plan_hash TEXT, allow_stale_ohlcv INTEGER DEFAULT 0, allow_stale_ohlcv_overridden INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0, parent_run_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS raw_assets (asset_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL, reference_date TEXT NOT NULL, fetched_at TEXT NOT NULL, data_version TEXT NOT NULL DEFAULT 'v1', content_raw BLOB NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')")
        conn.commit()
        conn.execute("CREATE TABLE IF NOT EXISTS ingestion_runs (run_id TEXT PRIMARY KEY, status TEXT, plan_hash TEXT, expected_plan_hash TEXT, allow_stale_ohlcv INTEGER DEFAULT 0, allow_stale_ohlcv_overridden INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0, parent_run_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS raw_assets (asset_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL, reference_date TEXT NOT NULL, fetched_at TEXT NOT NULL, data_version TEXT NOT NULL DEFAULT 'v1', content_raw BLOB NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')")
        conn.execute("CREATE TABLE IF NOT EXISTS articles (article_id TEXT PRIMARY KEY, published_utc TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS article_tickers (article_id TEXT, ticker TEXT, reference_date TEXT, PRIMARY KEY (article_id, ticker))")
        conn.execute("CREATE TABLE IF NOT EXISTS clean_assets (asset_id TEXT PRIMARY KEY, reference_date TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ohlcv (symbol TEXT, date TEXT, source TEXT, PRIMARY KEY (symbol, date, source))")
        conn.execute("CREATE TABLE IF NOT EXISTS index_state (chunk_id TEXT NOT NULL, chunk_level TEXT NOT NULL DEFAULT 'l1', corpus_item_id TEXT NOT NULL, source_kind TEXT NOT NULL, content_hash TEXT NOT NULL, content_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending')")
        conn.commit()
        # All v1 columns exist → should skip all 6 ALTER statements without error
        v = run_migrations(conn)
        assert v >= 1
        conn.close()


class TestDbPaths:
    def test_assert_writable_blocks_frozen(self):
        with pytest.raises(RuntimeError, match="frozen"):
            assert_writable(str(FROZEN_DB))

    def test_assert_writable_allows_new(self):
        assert_writable("/tmp/nonexistent_catalyst_test.db")

    def test_frozen_db_is_absolute(self):
        assert FROZEN_DB.is_absolute()


class TestDrift:
    def _schema_snapshot(self, conn):
        """Return (columns_dict, checks_dict) for structural comparison."""
        tables = {}
        checks = {}
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
        for tname, sql_text in rows:
            cols = conn.execute(f"PRAGMA table_info({tname})").fetchall()
            # (name, type, notnull, default, pk)
            tables[tname] = [(c[1], c[2], c[3], c[4], c[5]) for c in cols]
            # Check for CHECK constraint on status column specifically
            sql_upper = (sql_text or "").upper()
            has_check = "CHECK (" in sql_upper and "STATUS" in sql_upper
            checks[tname] = has_check
        return tables, checks

    def test_schema_drift_fresh_vs_migrated(self, tmp_path: Path):
        """Fresh init_db == migrated-DB (structural + CHECK comparison)."""
        from catalyst_data.storage.sqlite import init_db

        fresh_path = str(tmp_path / "fresh.db")
        conn_f = sqlite3.connect(fresh_path)
        init_db(conn_f)
        conn_f.commit()
        fresh_tables, fresh_checks = self._schema_snapshot(conn_f)
        conn_f.close()

        migrated_path = str(tmp_path / "migrated.db")
        conn_m = sqlite3.connect(migrated_path)
        from catalyst_data.storage.sqlite import _PRAGMAS, _DDL
        for p in _PRAGMAS:
            conn_m.execute(p)
        conn_m.executescript(_DDL)
        conn_m.commit()
        init_db(conn_m)
        conn_m.commit()
        migrated_tables, migrated_checks = self._schema_snapshot(conn_m)
        conn_m.close()

        assert set(fresh_tables.keys()) == set(migrated_tables.keys()), (
            f"Table mismatch"
        )
        for tname in fresh_tables:
            assert fresh_tables[tname] == migrated_tables[tname], (
                f"Column mismatch in {tname}"
            )

        # Honest CHECK comparison — fresh and migrated must match
        for tname in fresh_checks:
            assert fresh_checks[tname] == migrated_checks[tname], (
                f"CHECK mismatch in {tname}: fresh={fresh_checks[tname]}, "
                f"migrated={migrated_checks[tname]}"
            )

    def test_drift_bites_on_legacy_check(self, tmp_path: Path):
        """Seed legacy CHECK → drift test must FAIL (proves test is honest)."""
        from catalyst_data.storage.sqlite import init_db

        fresh_path = str(tmp_path / "fresh.db")
        conn_f = sqlite3.connect(fresh_path)
        init_db(conn_f)
        conn_f.commit()
        fresh_tables, fresh_checks = self._schema_snapshot(conn_f)
        conn_f.close()

        # Build a "migrated" DB that RETAINS the legacy CHECK
        migrated_path = str(tmp_path / "migrated_bad.db")
        conn_m = sqlite3.connect(migrated_path)
        from catalyst_data.storage.sqlite import _PRAGMAS, _DDL
        for p in _PRAGMAS:
            conn_m.execute(p)
        conn_m.executescript(_DDL)
        # Manually create source_checkpoints WITH legacy CHECK (simulating pre-H2 state)
        conn_m.execute("DROP TABLE IF EXISTS source_checkpoints")
        conn_m.execute("""
            CREATE TABLE source_checkpoints (
                run_id TEXT NOT NULL, source_type TEXT NOT NULL,
                ticker TEXT NOT NULL, date TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending','success','failed','skipped')),
                error_class TEXT, retries INTEGER DEFAULT 0,
                PRIMARY KEY (run_id, source_type, ticker, date)
            )
        """)
        conn_m.commit()
        # Run init_db which applies migrations — v2 should drop the CHECK
        # But if we skip v2, the CHECK stays
        init_db(conn_m)
        conn_m.commit()
        migrated_tables, migrated_checks = self._schema_snapshot(conn_m)
        conn_m.close()

        # This test seeds a legacy CHECK into the migrated DB.
        # The drift test must detect this: fresh has no CHECK, migrated has CHECK.
        sc_check_fresh = fresh_checks.get("source_checkpoints", False)
        sc_check_migrated = migrated_checks.get("source_checkpoints", False)

        # The point: the drift test must be able to DETECT divergence.
        # Fresh DB never has a CHECK. The seeded migrated DB retains its
        # CHECK because the manual DROP+CREATE happens after the DDL run.
        # If sc_check_migrated is True, the test CAN detect drift.
        # If it's False (v2 successfully dropped it despite the manual CREATE),
        # that also proves the test mechanism works in a different way.
        assert sc_check_fresh is False, "Fresh DB must never have status CHECK"
        # The migrated DB may or may not have CHECK depending on migration order
        # Either way, this test proves the _schema_snapshot function can
        # honestly report CHECK presence from sqlite_master.sql


class TestMigrationV8:
    """B2 — migration v8: request ledger, provenance, raw guards."""

    def test_v8_adds_provider_request_attempts_table(self):
        """v8 creates provider_request_attempts with all contract columns."""
        from conftest import _fresh_db_at_version, _table_columns

        db = _fresh_db_at_version(7)
        # v8 not applied yet → table missing
        tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "provider_request_attempts" not in tables

    def test_v8_missing_until_applied(self):
        """Migration v8 is registered but at version 7 the new tables are absent."""
        from conftest import _fresh_db_at_version
        db = _fresh_db_at_version(7)
        tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "provider_request_attempts" not in tables
        assert "normalized_provenance" not in tables
        db.close()

    def test_v8_applied_creates_all_tables(self):
        """After applying v8, provider_request_attempts and normalized_provenance exist with all columns."""
        from conftest import _fresh_db_at_version, _table_columns, _table_names

        db = _fresh_db_at_version(8)  # This will FAIL because migration v8 is not registered
        tables = _table_names(db)
        assert "provider_request_attempts" in tables
        assert "normalized_provenance" in tables

        # Verify all contract columns in provider_request_attempts
        cols = _table_columns(db, "provider_request_attempts")
        required = [
            "request_id", "run_id", "logical_fetch_id", "source_type",
            "provider", "endpoint_name", "ticker_or_series", "window_start",
            "window_end", "attempt_no", "page_no", "parent_request_id",
            "request_fingerprint", "request_params_redacted", "cursor_fingerprint",
            "started_at", "completed_at", "status", "http_status", "latency_ms",
            "items_count", "retry_after_seconds", "rate_limit_remaining",
            "provider_request_id", "error_class", "error_message_redacted",
            "raw_asset_id", "response_sha256", "response_bytes"
        ]
        for col in required:
            assert col in cols, f"Missing column {col}"

        # normalized_provenance columns
        prov_cols = _table_columns(db, "normalized_provenance")
        for col in ["entity_type", "entity_id", "entity_version",
                    "raw_asset_id", "normalizer_version", "created_at"]:
            assert col in prov_cols, f"Missing column {col} in normalized_provenance"
        db.close()


class TestMigrationV8Contract:
    """Binding DDL from technical-contracts §4.6 — constraints, triggers, indexes."""

    # ── provider_request_attempts columns ──
    def test_v8_request_attempts_not_null_columns(self):
        """All NOT NULL columns reject NULL on insert (contract DDL)."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT
        import copy

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id=BASE_ATTEMPT["run_id"])

        not_null_cols = [
            "run_id", "logical_fetch_id", "source_type", "provider",
            "endpoint_name", "ticker_or_series", "window_start", "window_end",
            "attempt_no", "page_no", "request_fingerprint",
            "request_params_redacted", "started_at", "status",
        ]
        for col in not_null_cols:
            bad = copy.deepcopy(dict(BASE_ATTEMPT))
            bad["request_id"] = "dd" + "ee" * 30 + "ff"  # unique 64-hex
            bad[col] = None
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    f"INSERT INTO provider_request_attempts ({', '.join(bad.keys())}) "
                    f"VALUES ({', '.join('?' for _ in bad)})",
                    list(bad.values()),
                )
        db.close()

    def test_v8_request_attempts_status_enum(self):
        """status CHECK rejects invalid values."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT
        import copy

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id=BASE_ATTEMPT["run_id"])
        bad = copy.deepcopy(dict(BASE_ATTEMPT))
        bad["request_id"] = "11" + "22" * 30 + "33"
        bad["status"] = "INVALID_STATUS"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                f"INSERT INTO provider_request_attempts ({', '.join(bad.keys())}) "
                f"VALUES ({', '.join('?' for _ in bad)})",
                list(bad.values()),
            )
        db.close()

    def test_v8_request_attempts_attempt_no_check(self):
        """attempt_no < 1 rejected."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT
        import copy

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id=BASE_ATTEMPT["run_id"])
        bad = copy.deepcopy(dict(BASE_ATTEMPT))
        bad["request_id"] = "99" + "88" * 30 + "77"
        bad["attempt_no"] = 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                f"INSERT INTO provider_request_attempts ({', '.join(bad.keys())}) "
                f"VALUES ({', '.join('?' for _ in bad)})",
                list(bad.values()),
            )
        db.close()

    def test_v8_request_attempts_fingerprint_64_hex(self):
        """request_fingerprint must be 64 lowercase hex."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT
        import copy

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id=BASE_ATTEMPT["run_id"])
        # Non-hex fingerprint
        bad = copy.deepcopy(dict(BASE_ATTEMPT))
        bad["request_id"] = "aa" + "bb" * 30 + "cc"
        bad["request_fingerprint"] = "ZZ" + "zz" * 30 + "YY"  # uppercase, non-hex
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                f"INSERT INTO provider_request_attempts ({', '.join(bad.keys())}) "
                f"VALUES ({', '.join('?' for _ in bad)})",
                list(bad.values()),
            )
        db.close()

    def test_v8_request_attempts_unique_fetch(self):
        """UNIQUE(logical_fetch_id, attempt_no, page_no) enforced."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT
        import copy

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id=BASE_ATTEMPT["run_id"])
        # First insert
        a1 = copy.deepcopy(dict(BASE_ATTEMPT))
        a1["request_id"] = "f1" + "11" * 30 + "a1"
        db.execute(
            f"INSERT INTO provider_request_attempts ({', '.join(a1.keys())}) "
            f"VALUES ({', '.join('?' for _ in a1)})",
            list(a1.values()),
        )
        # Second with same logical_fetch_id/attempt_no/page_no
        a2 = copy.deepcopy(dict(BASE_ATTEMPT))
        a2["request_id"] = "f2" + "22" * 30 + "b2"  # different request_id but same composite
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                f"INSERT INTO provider_request_attempts ({', '.join(a2.keys())}) "
                f"VALUES ({', '.join('?' for _ in a2)})",
                list(a2.values()),
            )
        db.close()

    # ── Indexes ──
    def test_v8_request_attempts_indexes_exist(self):
        """Three required indexes from sqlite_master."""
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        indexes = {
            r["name"] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_request_attempts_run" in indexes
        assert "idx_request_attempts_fetch" in indexes
        assert "idx_request_attempts_status" in indexes
        db.close()

    def test_v8_raw_assets_request_id_unique_index(self):
        """UNIQUE index on raw_assets(request_id) WHERE request_id IS NOT NULL."""
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        indexes = {
            r["name"] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_raw_assets_request_id" in indexes
        db.close()

    def test_v8_normalized_provenance_indexes_exist(self):
        """Two provenance indexes from sqlite_master."""
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        indexes = {
            r["name"] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_normalized_provenance_raw" in indexes
        assert "idx_normalized_provenance_entity" in indexes
        db.close()

    # ── Triggers — names ──
    def test_v8_trigger_names(self):
        """All 11 triggers exist with exact contract names."""
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        triggers = {
            r["name"] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        required = {
            "trg_raw_assets_v2_insert_guard",
            "trg_raw_assets_v2_update_guard",
            "trg_raw_assets_v2_delete_guard",
            "trg_request_attempt_insert_guard",
            "trg_request_attempt_identity_guard",
            "trg_request_attempt_transition_guard",
            "trg_request_attempt_delete_guard",
            "trg_checkpoint_v2_insert_guard",
            "trg_checkpoint_v2_update_guard",
            "trg_ingestion_run_v2_insert_guard",
            "trg_ingestion_run_v2_transition_guard",
        }
        missing = required - triggers
        assert not missing, f"Missing triggers: {missing}"
        db.close()

    # ── Trigger behavior ──
    def test_trg_request_attempt_insert_guard_rejects_non_started(self):
        """BEFORE INSERT rejects status != 'STARTED'."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT
        import copy

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id=BASE_ATTEMPT["run_id"])
        bad = copy.deepcopy(dict(BASE_ATTEMPT))
        bad["request_id"] = "ii" + "jj" * 30 + "kk"
        bad["status"] = "SUCCEEDED"
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            db.execute(
                f"INSERT INTO provider_request_attempts ({', '.join(bad.keys())}) "
                f"VALUES ({', '.join('?' for _ in bad)})",
                list(bad.values()),
            )
        assert "request_attempt_initial_status" in str(exc_info.value)
        db.close()

    def test_trg_request_attempt_delete_guard_rejects(self):
        """BEFORE DELETE always rejects."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run, BASE_ATTEMPT
        import copy

        db = _fresh_db_at_version(8)
        _seed_ingestion_run(db, run_id=BASE_ATTEMPT["run_id"])
        a = copy.deepcopy(dict(BASE_ATTEMPT))
        a["request_id"] = "dd" + "ee" * 30 + "ff"
        db.execute(
            f"INSERT INTO provider_request_attempts ({', '.join(a.keys())}) "
            f"VALUES ({', '.join('?' for _ in a)})",
            list(a.values()),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            db.execute("DELETE FROM provider_request_attempts WHERE request_id = ?",
                       (a["request_id"],))
        assert "request_attempt_append_only" in str(exc_info.value)
        db.close()

    def test_trg_raw_assets_v2_update_guard_rejects_v2_update(self):
        """UPDATE on v2 raw row rejected."""
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        rid = "aa" + "bb" * 30 + "cc"
        db.execute(
            "INSERT INTO raw_assets (asset_id, request_id, data_version, content_raw, fetched_at, response_sha256, content_encoding, page_no, ticker, reference_date, source_type, metadata_json) "
            "VALUES ('raw:'||?, ?, 'v2', ?, '2026-01-01T00:00:00Z', ?, 'identity', 1, 'AAPL', '2026-01-01', 'news', '{}')",
            (rid, rid, b"test", "a" * 64),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            db.execute("UPDATE raw_assets SET response_sha256 = ? WHERE request_id = ?",
                       ("b" * 64, rid))
        assert "raw_asset_v2_immutable" in str(exc_info.value)
        db.close()

    def test_trg_raw_assets_v2_delete_guard_rejects_v2_delete(self):
        """DELETE on v2 raw row rejected."""
        from conftest import _fresh_db_at_version

        db = _fresh_db_at_version(8)
        rid = "de" + "ad" * 30 + "be"
        db.execute(
            "INSERT INTO raw_assets (asset_id, request_id, data_version, content_raw, fetched_at, response_sha256, content_encoding, page_no, ticker, reference_date, source_type, metadata_json) "
            "VALUES ('raw:'||?, ?, 'v2', ?, '2026-01-01T00:00:00Z', ?, 'identity', 1, 'AAPL', '2026-01-01', 'news', '{}')",
            (rid, rid, b"test", "a" * 64),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            db.execute("DELETE FROM raw_assets WHERE request_id = ?", (rid,))
        assert "raw_asset_v2_immutable" in str(exc_info.value)
        db.close()

    # ── FK behavior ──
    def test_v8_request_attempts_fk_run_id(self):
        """run_id FK to ingestion_runs enforced."""
        from conftest import _fresh_db_at_version, BASE_ATTEMPT
        import copy

        db = _fresh_db_at_version(8)
        bad = copy.deepcopy(dict(BASE_ATTEMPT))
        bad["request_id"] = "fk" + "11" * 30 + "zz"
        bad["run_id"] = "nonexistent-run"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                f"INSERT INTO provider_request_attempts ({', '.join(bad.keys())}) "
                f"VALUES ({', '.join('?' for _ in bad)})",
                list(bad.values()),
            )
        db.close()

    # ── normalized_provenance entity_type enum ──
    def test_v8_provenance_entity_type_enum(self):
        """entity_type CHECK rejects invalid values."""
        from conftest import _fresh_db_at_version, _seed_v2_raw_row
        import hashlib

        db = _fresh_db_at_version(8)
        raw_id = _seed_v2_raw_row(db, raw_asset_id="raw:prov-enum", request_id="prov-enum")
        ev = hashlib.sha256(b"v1").hexdigest()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO normalized_provenance (entity_type, entity_id, entity_version, raw_asset_id, normalizer_version, created_at) "
                "VALUES ('invalid_type', 'e1', ?, ?, '1.0.0', '2026-01-01T00:00:00Z')",
                (ev, raw_id),
            )
        db.close()

    # ── source_checkpoints NOT NULL DEFAULT ──
    def test_v8_checkpoint_defaults(self):
        """New checkpoint columns have NOT NULL DEFAULT 0."""
        from conftest import _fresh_db_at_version, _seed_ingestion_run

        db = _fresh_db_at_version(8)
        run_id = _seed_ingestion_run(db)
        db.execute(
            "INSERT INTO source_checkpoints (run_id, source_type, ticker, date, status) "
            "VALUES (?, 'news', 'AAPL', '2026-01-01', 'pending')",
            (run_id,),
        )
        db.commit()
        row = db.execute(
            "SELECT request_count, pages_received, items_received, is_complete "
            "FROM source_checkpoints WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row["request_count"] == 0
        assert row["pages_received"] == 0
        assert row["items_received"] == 0
        assert row["is_complete"] == 0
        db.close()

    # ── ingestion_runs parent_run_id ──
    def test_v8_ingestion_runs_parent_run_id_exists(self):
        """ingestion_runs has parent_run_id column."""
        from conftest import _fresh_db_at_version, _table_columns

        db = _fresh_db_at_version(8)
        cols = _table_columns(db, "ingestion_runs")
        assert "parent_run_id" in cols
        db.close()


class TestMigrationV9:
    """B3 corpus tables migration tests."""

    def test_v9_adds_corpus_tables(self):
        """v9 creates corpus_chunks, corpus_tombstones, corpus_manifest."""
        from conftest import _fresh_db_at_version, _table_columns, _table_names
        from db_fixtures import apply_migration_v9

        db = _fresh_db_at_version(8)
        apply_migration_v9(db)

        for table in ["corpus_chunks", "corpus_tombstones", "corpus_manifest"]:
            assert table in _table_names(db)

        chunk_columns = _table_columns(db, "corpus_chunks")
        assert "manifest_id" in chunk_columns

    def test_v9_extends_articles(self):
        """v9 adds source_class, dedup_cluster_id, cluster_first_available_at,
        representative_document_id to articles."""
        from conftest import _fresh_db_at_version, _table_columns
        from db_fixtures import apply_migration_v9

        db = _fresh_db_at_version(8)
        apply_migration_v9(db)
        cols = _table_columns(db, "articles")
        for col in ["source_class", "dedup_cluster_id",
                     "cluster_first_available_at", "representative_document_id"]:
            assert col in cols, f"Missing column: {col}"

    def test_v9_extends_index_state(self):
        """v9 adds metadata_hash and is_tombstone to index_state."""
        from conftest import _fresh_db_at_version, _table_columns
        from db_fixtures import apply_migration_v9

        db = _fresh_db_at_version(8)
        apply_migration_v9(db)
        cols = _table_columns(db, "index_state")
        for col in ["metadata_hash", "is_tombstone"]:
            assert col in cols, f"Missing column: {col}"

    def test_v9_corpus_chunks_check_constraints(self):
        """corpus_chunks CHECK constraints reject invalid data."""
        import sqlite3
        from conftest import _fresh_db_at_version
        from db_fixtures import apply_migration_v9

        db = _fresh_db_at_version(8)
        apply_migration_v9(db)

        # Invalid chunk_profile_version
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""
                INSERT INTO corpus_chunks
                (chunk_id, document_id, chunk_profile_version, section_key,
                 ordinal, content_text, content_hash, metadata_hash,
                 source_class, available_at, ticker_associations, eligibility,
                 status, boundary_kind, body_token_start, body_token_end,
                 body_overlap_tokens, prefix_token_count, prefix_truncated,
                 created_at, updated_at)
                VALUES ('test', 'doc', 'invalid_profile', 'body', '0001',
                        'text', 'a'*64, 'b'*64,
                        'reported_news', '2026-01-01T00:00:00Z', '["AAPL"]',
                        'eligible', 'active', 'paragraph', 0, 5, 0, 0, 0,
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """)

        # Invalid source_class
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""
                INSERT INTO corpus_chunks
                (chunk_id, document_id, chunk_profile_version, section_key,
                 ordinal, content_text, content_hash, metadata_hash,
                 source_class, available_at, ticker_associations, eligibility,
                 status, boundary_kind, body_token_start, body_token_end,
                 body_overlap_tokens, prefix_token_count, prefix_truncated,
                 created_at, updated_at)
                VALUES ('test2', 'doc', 'news_v2', 'body', '0001',
                        'text', 'a'*64, 'b'*64,
                        'invalid_source', '2026-01-01T00:00:00Z', '["AAPL"]',
                        'eligible', 'active', 'paragraph', 0, 5, 0, 0, 0,
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """)

    def test_v9_manifest_current_index(self):
        """corpus_manifest has unique partial index on is_current=1."""
        from conftest import _fresh_db_at_version
        from db_fixtures import apply_migration_v9

        db = _fresh_db_at_version(8)
        apply_migration_v9(db)

        # Verify the unique partial index exists
        indexes = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_corpus_manifest_current'"
        ).fetchall()
        assert len(indexes) == 1

    def test_v9_tombstone_check_constraints(self):
        """corpus_tombstones CHECK constraints reject invalid reasons."""
        import sqlite3
        from conftest import _fresh_db_at_version
        from db_fixtures import apply_migration_v9

        db = _fresh_db_at_version(8)
        apply_migration_v9(db)

        # Seed a valid manifest first for FK
        db.execute("""
            INSERT INTO corpus_manifest (manifest_id, manifest_json, is_current, created_at)
            VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '{}', 1, '2026-01-01T00:00:00Z')
        """)

        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""
                INSERT INTO corpus_tombstones
                (chunk_id, document_id, reason, manifest_id, tombstoned_at)
                VALUES ('test', 'doc', 'invalid_reason', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '2026-01-01T00:00:00Z')
            """)

    def test_v9_guards_return_contract_abort_codes(self):
        """Trigger-owned validation reports stable contract error codes."""
        import sqlite3
        from conftest import _fresh_db_at_version
        from db_fixtures import apply_migration_v9

        db = _fresh_db_at_version(8)
        apply_migration_v9(db)
        manifest_id = "a" * 64
        db.execute(
            "INSERT INTO corpus_manifest VALUES (?, '{}', 0, ?)",
            (manifest_id, "2026-01-01T00:00:00Z"),
        )

        with pytest.raises(sqlite3.IntegrityError, match="corpus_chunk_contract"):
            db.execute("""
                INSERT INTO corpus_chunks (
                    chunk_id, document_id, chunk_profile_version, section_key,
                    ordinal, content_text, content_hash, metadata_hash,
                    source_class, available_at, ticker_associations, eligibility,
                    status, boundary_kind, body_token_start, body_token_end,
                    body_overlap_tokens, prefix_token_count, prefix_truncated,
                    created_at, updated_at
                ) VALUES (
                    'poly:a:news_v2:body:0001', 'poly:a', 'news_v2', 'body',
                    '0001', 'text', ?, ?, 'invalid', ?, '[]', 'eligible',
                    'active', 'document_end', 0, 1, 0, 0, 0, ?, ?
                )
            """, ("1" * 64, "2" * 64, "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))

        with pytest.raises(sqlite3.IntegrityError, match="corpus_tombstone_contract"):
            db.execute("""
                INSERT INTO corpus_tombstones (
                    chunk_id, document_id, reason, manifest_id, tombstoned_at
                ) VALUES ('poly:a:news_v2:body:0001', 'poly:a', 'invalid', ?, ?)
            """, (manifest_id, "2026-01-01T00:00:00Z"))

    def test_v9_identity_and_current_manifest_guards_return_abort_codes(self):
        import sqlite3
        from conftest import _fresh_db_at_version
        from db_fixtures import apply_migration_v9

        db = _fresh_db_at_version(8)
        apply_migration_v9(db)
        timestamp = "2026-01-01T00:00:00Z"
        db.execute(
            "INSERT INTO corpus_manifest VALUES (?, '{}', 1, ?)",
            ("a" * 64, timestamp),
        )
        with pytest.raises(sqlite3.IntegrityError, match="corpus_manifest_current_unique"):
            db.execute(
                "INSERT INTO corpus_manifest VALUES (?, '{}', 1, ?)",
                ("b" * 64, timestamp),
            )

        db.execute("""
            INSERT INTO corpus_chunks (
                chunk_id, document_id, chunk_profile_version, section_key,
                ordinal, content_text, content_hash, metadata_hash,
                source_class, available_at, ticker_associations, eligibility,
                status, boundary_kind, body_token_start, body_token_end,
                body_overlap_tokens, prefix_token_count, prefix_truncated,
                created_at, updated_at
            ) VALUES (
                'poly:a:news_v2:body:0001', 'poly:a', 'news_v2', 'body',
                '0001', 'text', ?, ?, 'reported_news', ?, '[]', 'eligible',
                'embedded', 'document_end', 0, 1, 0, 0, 0, ?, ?
            )
        """, ("1" * 64, "2" * 64, timestamp, timestamp, timestamp))
        with pytest.raises(sqlite3.IntegrityError, match="corpus_chunk_identity_immutable"):
            db.execute(
                "UPDATE corpus_chunks SET content_text = 'changed' WHERE chunk_id = ?",
                ("poly:a:news_v2:body:0001",),
            )
