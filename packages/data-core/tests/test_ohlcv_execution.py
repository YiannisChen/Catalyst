"""W1-C: OHLCV execution, fallback, precedence, lifecycle, and migration tests."""
import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from catalyst_data.connectors.base import FetchResult
from catalyst_data.storage.sqlite import (
    SOURCE_PRECEDENCE, UnknownOHLCVSource, UpsertOutcome, upsert_ohlcv,
    compute_asset_id,
)


def create_temp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    # Only pre-create the tables these OHLCV tests operate on directly.
    # articles / article_tickers / clean_assets are owned by init_db()'s canonical
    # DDL (with their ticker indexes); pre-creating minimal versions here collides
    # with those indexes when _fetch_cell() calls init_db().
    conn.executescript("""CREATE TABLE ohlcv (
        symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, source TEXT,
        UNIQUE(symbol, date))""")
    conn.execute("""CREATE TABLE source_checkpoints (
        run_id TEXT, source_type TEXT, ticker TEXT, date TEXT,
        status TEXT, error_class TEXT, retries INTEGER DEFAULT 0,
        error_message_redacted TEXT, http_status INTEGER,
        retry_after_seconds REAL, provider_latency_ms REAL,
        raw_asset_id TEXT, items_count INTEGER,
        fallback_provider TEXT, fallback_triggered INTEGER DEFAULT 0,
        empty_reason TEXT)""")
    conn.execute("""CREATE TABLE raw_assets (
        asset_id TEXT PRIMARY KEY, ticker TEXT, source_type TEXT,
        reference_date TEXT, fetched_at TEXT, data_version TEXT,
        content_raw BLOB, http_status INTEGER, metadata_json TEXT)""")
    conn.commit()
    conn.close()
    return db_path


def row_counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM source_checkpoints").fetchone()[0],
    )


# =====================================================================
# SOURCE_PRECEDENCE + upsert_ohlcv
# =====================================================================


class TestSourcePrecedence:
    @pytest.fixture
    def db_path(self, tmp_path):
        return create_temp_db(tmp_path)

    def test_polygon_overwrites_yfinance(self, db_path):
        conn = sqlite3.connect(db_path)
        upsert_ohlcv(conn, symbol="AAPL", date="2026-07-09",
                     open=220, high=225, low=219, close=224, volume=5e7,
                     source="yfinance", commit=True)
        outcome = upsert_ohlcv(conn, symbol="AAPL", date="2026-07-09",
                                open=221, high=226, low=220, close=225, volume=5e7,
                                source="polygon", commit=True)
        assert outcome == UpsertOutcome.UPDATED
        row = conn.execute("SELECT source, open FROM ohlcv WHERE symbol='AAPL'").fetchone()
        assert row[0] == "polygon"
        assert row[1] == 221
        conn.close()

    def test_yfinance_cannot_overwrite_polygon(self, db_path):
        conn = sqlite3.connect(db_path)
        upsert_ohlcv(conn, symbol="AAPL", date="2026-07-09",
                     open=220, high=225, low=219, close=224, volume=5e7,
                     source="polygon", commit=True)
        outcome = upsert_ohlcv(conn, symbol="AAPL", date="2026-07-09",
                                open=190, high=195, low=189, close=194, volume=4e7,
                                source="yfinance", commit=True)
        assert outcome == UpsertOutcome.BLOCKED
        row = conn.execute("SELECT source, open FROM ohlcv WHERE symbol='AAPL'").fetchone()
        assert row[0] == "polygon"
        assert row[1] == 220
        conn.close()

    def test_same_source_updates_idempotently(self, db_path):
        conn = sqlite3.connect(db_path)
        upsert_ohlcv(conn, symbol="AAPL", date="2026-07-09",
                     open=220, high=225, low=219, close=224, volume=5e7,
                     source="polygon", commit=True)
        outcome = upsert_ohlcv(conn, symbol="AAPL", date="2026-07-09",
                                open=221, high=226, low=220, close=225, volume=5e7,
                                source="polygon", commit=True)
        assert outcome == UpsertOutcome.UPDATED
        row = conn.execute("SELECT open FROM ohlcv WHERE symbol='AAPL'").fetchone()
        assert row[0] == 221
        conn.close()

    def test_unknown_source_raises(self, db_path):
        conn = sqlite3.connect(db_path)
        with pytest.raises(UnknownOHLCVSource):
            upsert_ohlcv(conn, symbol="AAPL", date="2026-07-09",
                         open=220, high=225, low=219, close=224, volume=5e7,
                         source="nonexistent", commit=True)
        conn.close()

    def test_rollback_removes_ohlcv(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("BEGIN IMMEDIATE")
        upsert_ohlcv(conn, symbol="AAPL", date="2026-07-09",
                     open=220, high=225, low=219, close=224, volume=5e7,
                     source="polygon", commit=False)
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM ohlcv WHERE symbol='AAPL'").fetchone()[0] == 0
        conn.close()


# =====================================================================
# Polygon adjusted=true
# =====================================================================


class TestPolygonOHLCVURL:
    def test_url_includes_adjusted_param(self):
        from catalyst_data.connectors.polygon import _build_ohlcv_url
        url, params = _build_ohlcv_url("AAPL", "2026-07-09")
        assert "adjusted" in params
        assert params["adjusted"] == "true"


# =====================================================================
# Real _fetch_cell end-to-end tests
# =====================================================================


async def fake_polygon_success(ticker, endpoint, date):
    assert endpoint == "ohlcv"
    return FetchResult(
        status=200,
        data={
            "results": [
                {"o": 220.0, "h": 225.0, "l": 219.0, "c": 224.0, "v": 5e7}
            ]
        },
        source_label="polygon:ohlcv",
    )


async def fake_polygon_empty(ticker, endpoint, date):
    assert endpoint == "ohlcv"
    return FetchResult(status=200, data={"results": []}, source_label="polygon:ohlcv")


def fake_polygon_transport_error(ticker, endpoint, date):
    raise ConnectionError("polygon unreachable")


async def fake_polygon_auth_error(ticker, endpoint, date):
    assert endpoint == "ohlcv"
    return FetchResult(status=403, error="Forbidden", source_label="polygon:ohlcv")


async def fake_yfinance_success(ticker, endpoint, date):
    assert endpoint == "ohlcv"
    return FetchResult(
        status=200,
        data={"Open": 190.0, "High": 195.0, "Low": 189.0, "Close": 194.0, "Volume": 4e7},
        source_label="yfinance:ohlcv",
    )


async def fake_yfinance_empty(ticker, endpoint, date):
    return FetchResult(status=200, data=None, source_label="yfinance:ohlcv")


def fake_yfinance_transport_error(ticker, endpoint, date):
    raise ConnectionError("yfinance unreachable")


class TestFetchCellOHLCV:
    @pytest.fixture
    def db_path(self, tmp_path):
        return create_temp_db(tmp_path)

    def _fetch(self, db_path, fetch_map):
        from catalyst_data.update_pipeline import _fetch_cell
        return asyncio.run(_fetch_cell(
            db_path, "AAPL", "2026-07-09", "polygon_ohlcv", "run-1",
            fetch_map,
        ))

    # --- success ---

    def test_polygon_success_persists_three_layers(self, db_path):
        result = self._fetch(db_path, {"polygon_ohlcv": fake_polygon_success})
        assert result["status"] == "success"

        conn = sqlite3.connect(db_path)
        r, o, c = row_counts(conn)
        assert r == 1
        assert o == 1
        assert c == 1
        chk = conn.execute(
            "SELECT status, error_class, fallback_provider, fallback_triggered FROM source_checkpoints"
        ).fetchone()
        assert chk[0] == "success"
        assert chk[1] is None
        assert chk[2] is None
        assert chk[3] == 0
        ohlcv_r = conn.execute("SELECT source, close FROM ohlcv").fetchone()
        assert ohlcv_r[0] == "polygon"
        assert ohlcv_r[1] == 224.0
        conn.close()

    def test_polygon_success_fallback_not_called(self, db_path):
        yf_calls = []
        def counting_yf(ticker, endpoint, date):
            yf_calls.append(1)
            return fake_yfinance_success(ticker, endpoint, date)

        fm = {"polygon_ohlcv": fake_polygon_success, "yfinance_ohlcv": counting_yf}
        result = self._fetch(db_path, fm)
        assert result["status"] == "success"
        assert len(yf_calls) == 0

    # --- empty valid ---

    def test_polygon_empty_persists_success_empty(self, db_path):
        result = self._fetch(db_path, {"polygon_ohlcv": fake_polygon_empty})
        assert result["status"] == "success_empty"
        assert result.get("empty_reason") == "no_bars_returned"

        conn = sqlite3.connect(db_path)
        chk = conn.execute(
            "SELECT status, empty_reason, items_count FROM source_checkpoints"
        ).fetchone()
        assert chk[0] == "success_empty"
        assert chk[1] == "no_bars_returned"
        assert chk[2] == 0
        conn.close()

    # --- fallback ---

    def test_primary_transport_fallback_success_lands_yfinance(self, db_path):
        fm = {
            "polygon_ohlcv": fake_polygon_transport_error,
            "yfinance_ohlcv": fake_yfinance_success,
        }
        result = self._fetch(db_path, fm)
        assert result["status"] == "success"
        assert result.get("fallback_provider") == "yfinance_ohlcv"

        conn = sqlite3.connect(db_path)
        ohlcv_r = conn.execute("SELECT source, close FROM ohlcv").fetchone()
        assert ohlcv_r[0] == "yfinance"
        assert ohlcv_r[1] == 194.0
        chk = conn.execute(
            "SELECT fallback_provider, fallback_triggered FROM source_checkpoints"
        ).fetchone()
        assert chk[0] == "yfinance_ohlcv"
        assert chk[1] == 1
        conn.close()

    def test_primary_auth_does_not_trigger_fallback(self, db_path):
        yf_calls = []
        def counting_yf(ticker, endpoint, date):
            yf_calls.append(1)
            return fake_yfinance_success(ticker, endpoint, date)

        fm = {"polygon_ohlcv": fake_polygon_auth_error, "yfinance_ohlcv": counting_yf}
        result = self._fetch(db_path, fm)
        assert result["status"] == "failed"
        assert len(yf_calls) == 0, "fallback must not be called for AUTH"

    # --- double failure ---

    def test_primary_and_fallback_both_fail_preserves_evidence(self, db_path):
        fm = {
            "polygon_ohlcv": fake_polygon_transport_error,
            "yfinance_ohlcv": fake_yfinance_transport_error,
        }
        result = self._fetch(db_path, fm)
        assert result["status"] == "failed"
        assert result.get("error")

        conn = sqlite3.connect(db_path)
        r, o, c = row_counts(conn)
        assert r == 0, f"raw_assets should be 0 on failure, got {r}"
        assert o == 0, f"ohlcv should be 0 on failure, got {o}"
        assert c == 1  # one failed checkpoint
        chk = conn.execute("SELECT status, error_class FROM source_checkpoints").fetchone()
        assert chk[0] == "failed"
        conn.close()

    # --- fallback empty ---

    def test_fallback_empty_does_not_overwrite(self, db_path):
        fm = {
            "polygon_ohlcv": fake_polygon_transport_error,
            "yfinance_ohlcv": fake_yfinance_empty,
        }
        result = self._fetch(db_path, fm)
        assert result["status"] == "failed"
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 0
        conn.close()


# =====================================================================
# Fallback provenance
# =====================================================================


class TestFallbackProvenance:
    def test_ohlcv_fallback_chain_exists(self):
        from catalyst_data.fallback import OHLCV_FALLBACK_CHAIN, FallbackPolicy
        assert "polygon_ohlcv" in OHLCV_FALLBACK_CHAIN
        assert "yfinance_ohlcv" in OHLCV_FALLBACK_CHAIN
        policy = FallbackPolicy()
        fb = policy.next_provider("polygon_ohlcv")
        assert fb == "yfinance_ohlcv"


# =====================================================================
# Migration v7
# =====================================================================


class TestMigrationV7:
    def test_empty_reason_column_exists_after_migration(self, tmp_path):
        db_path = str(tmp_path / "migrate.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE source_checkpoints (
            run_id TEXT, source_type TEXT, ticker TEXT, date TEXT,
            status TEXT, error_class TEXT, retries INTEGER DEFAULT 0,
            error_message_redacted TEXT, http_status INTEGER,
            retry_after_seconds REAL, provider_latency_ms REAL,
            raw_asset_id TEXT, items_count INTEGER,
            fallback_provider TEXT, fallback_triggered INTEGER DEFAULT 0)""")
        conn.executescript("CREATE TABLE IF NOT EXISTS ingestion_runs (run_id TEXT PRIMARY KEY, status TEXT); CREATE TABLE IF NOT EXISTS raw_assets (asset_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL, reference_date TEXT NOT NULL, fetched_at TEXT NOT NULL, data_version TEXT NOT NULL DEFAULT 'v1', content_raw BLOB NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'); CREATE TABLE IF NOT EXISTS articles (article_id TEXT PRIMARY KEY, published_utc TEXT); CREATE TABLE IF NOT EXISTS article_tickers (article_id TEXT, ticker TEXT, reference_date TEXT, PRIMARY KEY (article_id, ticker)); CREATE TABLE IF NOT EXISTS clean_assets (asset_id TEXT PRIMARY KEY, reference_date TEXT); CREATE TABLE IF NOT EXISTS index_state (chunk_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'); CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)")
        conn.commit(); conn.close()
        from catalyst_data.migrations import run_migrations
        conn = sqlite3.connect(db_path)
        run_migrations(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(source_checkpoints)")]
        assert "empty_reason" in cols
        conn.close()

    def test_migration_refuses_frozen_db(self, tmp_path, monkeypatch):
        """Call run_migrations on a FROZEN_PATHS path must raise FrozenDBWriteError."""
        db_path = str(tmp_path / "frozen_copy.db")
        import shutil, os, hashlib

        frozen = "/Users/yiannischen/Desktop/Catalyst/data/catalyst_eval_frozen_v2.db"
        if not os.path.exists(frozen):
            pytest.skip("frozen DB not available")
        shutil.copy2(frozen, db_path)
        before_sha = hashlib.sha256(Path(db_path).read_bytes()).hexdigest()

        import catalyst_data.storage.sqlite as st
        monkeypatch.setattr(st, "FROZEN_PATHS",
                           st.FROZEN_PATHS | {os.path.realpath(db_path)})

        from catalyst_data.storage.sqlite import _assert_not_frozen
        with pytest.raises(Exception):
            _assert_not_frozen(db_path)

        after_sha = hashlib.sha256(Path(db_path).read_bytes()).hexdigest()
        assert before_sha == after_sha


# =====================================================================
# Lifecycle state machine
# =====================================================================


class TestLifecycleStateMachine:
    @pytest.fixture
    def db_path(self, tmp_path):
        return create_temp_db(tmp_path)

    def test_no_checkpoint_plans(self, db_path):
        conn = sqlite3.connect(db_path)
        from catalyst_data.update_planner import _should_plan_cell
        assert _should_plan_cell(conn, "AAPL", "2026-07-12", "polygon_ohlcv",
                                reference_today="2026-07-12")
        conn.close()

    def test_success_never_plans(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status) "
            "VALUES ('r0','polygon_ohlcv','AAPL','2026-07-12','success')")
        conn.commit()
        from catalyst_data.update_planner import _should_plan_cell
        assert not _should_plan_cell(conn, "AAPL", "2026-07-12", "polygon_ohlcv",
                                    reference_today="2026-07-12")
        conn.close()

    def test_first_empty_same_day_not_planned(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status) "
            "VALUES ('r1','polygon_ohlcv','AAPL','2026-07-12','success_empty')")
        conn.commit()
        from catalyst_data.update_planner import _should_plan_cell
        assert not _should_plan_cell(conn, "AAPL", "2026-07-12", "polygon_ohlcv",
                                    reference_today="2026-07-12")
        conn.close()

    def test_first_empty_next_day_recheck_planned(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status) "
            "VALUES ('r1','polygon_ohlcv','AAPL','2026-07-12','success_empty')")
        conn.commit()
        from catalyst_data.update_planner import _should_plan_cell
        assert _should_plan_cell(conn, "AAPL", "2026-07-12", "polygon_ohlcv",
                                reference_today="2026-07-13")
        conn.close()

    def test_second_empty_terminal(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status) "
            "VALUES ('r1','polygon_ohlcv','AAPL','2026-07-12','success_empty')")
        conn.execute(
            "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status) "
            "VALUES ('r2','polygon_ohlcv','AAPL','2026-07-12','success_empty')")
        conn.commit()
        from catalyst_data.update_planner import _should_plan_cell
        assert not _should_plan_cell(conn, "AAPL", "2026-07-12", "polygon_ohlcv",
                                    reference_today="2026-07-14")
        conn.close()

    def test_empty_expires_after_5_day_window(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status) "
            "VALUES ('r1','polygon_ohlcv','AAPL','2026-07-01','success_empty')")
        conn.commit()
        from catalyst_data.update_planner import _should_plan_cell
        assert not _should_plan_cell(conn, "AAPL", "2026-07-01", "polygon_ohlcv",
                                    reference_today="2026-07-13")
        conn.close()

    def test_0_to_2_permanent_failures_still_planned(self, db_path):
        conn = sqlite3.connect(db_path)
        for i in range(2):
            conn.execute(
                "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status,error_class) "
                f"VALUES ('r{i}','polygon_ohlcv','AAPL','2026-07-09','failed','auth')")
        conn.commit()
        from catalyst_data.update_planner import _should_plan_cell
        assert _should_plan_cell(conn, "AAPL", "2026-07-09", "polygon_ohlcv",
                                reference_today="2026-07-12")
        conn.close()

    def test_3_permanent_failures_excluded(self, db_path):
        conn = sqlite3.connect(db_path)
        for i in range(3):
            conn.execute(
                "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status,error_class) "
                f"VALUES ('r{i}','polygon_ohlcv','AAPL','2026-07-09','failed','auth')")
        conn.commit()
        from catalyst_data.update_planner import _should_plan_cell
        assert not _should_plan_cell(conn, "AAPL", "2026-07-09", "polygon_ohlcv",
                                    reference_today="2026-07-13")
        conn.close()

    def test_transient_failures_not_excluded_by_permanent_threshold(self, db_path):
        conn = sqlite3.connect(db_path)
        for i in range(5):
            conn.execute(
                "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status,error_class) "
                f"VALUES ('r{i}','polygon_ohlcv','AAPL','2026-07-09','failed','transport')")
        conn.commit()
        from catalyst_data.update_planner import _should_plan_cell
        assert _should_plan_cell(conn, "AAPL", "2026-07-09", "polygon_ohlcv",
                                reference_today="2026-07-13")
        conn.close()

    def test_success_supersedes_previous_states(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status) "
            "VALUES ('r0','polygon_ohlcv','AAPL','2026-07-09','success_empty')")
        conn.execute(
            "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status,error_class) "
            "VALUES ('r0','polygon_ohlcv','AAPL','2026-07-09','failed','auth')")
        conn.execute(
            "INSERT INTO source_checkpoints (run_id,source_type,ticker,date,status) "
            "VALUES ('r1','polygon_ohlcv','AAPL','2026-07-09','success')")
        conn.commit()
        from catalyst_data.update_planner import _should_plan_cell
        assert not _should_plan_cell(conn, "AAPL", "2026-07-09", "polygon_ohlcv",
                                    reference_today="2026-07-12")
        conn.close()
