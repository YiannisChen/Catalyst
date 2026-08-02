"""plan/apply checkpoint reconciliation."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.ingestion.checkpoint_reconciliation import (
    apply_checkpoint_reconciliation,
    plan_checkpoint_reconciliation,
)
from catalyst_data.migrations import run_migrations


def _cand(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "candidates"
    d.mkdir(parents=True)
    return d / "work.db"


def _boot(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE source_checkpoints (
            checkpoint_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_type TEXT NOT NULL,
            ticker TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0, logical_fetch_id TEXT,
            cell_id TEXT, endpoint_name TEXT, raw_asset_id TEXT,
            pages_received INTEGER DEFAULT 0, items_received INTEGER DEFAULT 0,
            is_complete INTEGER DEFAULT 0
        );
        CREATE TABLE provider_request_attempts (
            request_id TEXT PRIMARY KEY, run_id TEXT, logical_fetch_id TEXT,
            source_type TEXT, provider TEXT, endpoint_name TEXT,
            ticker_or_series TEXT, window_start TEXT, window_end TEXT,
            attempt_no INTEGER, page_no INTEGER, request_fingerprint TEXT,
            request_params_redacted TEXT DEFAULT '{}', started_at TEXT,
            status TEXT, raw_asset_id TEXT
        );
        CREATE TABLE raw_assets (
            asset_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL,
            reference_date TEXT NOT NULL, fetched_at TEXT NOT NULL,
            content_raw BLOB NOT NULL, metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE ingestion_runs (run_id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE articles (article_id TEXT PRIMARY KEY);
        CREATE TABLE article_tickers (article_id TEXT, ticker TEXT, PRIMARY KEY (article_id, ticker));
        CREATE TABLE clean_assets (asset_id TEXT PRIMARY KEY);
        CREATE TABLE ohlcv (symbol TEXT, date TEXT, source TEXT, PRIMARY KEY (symbol, date, source));
        CREATE TABLE index_state (
            chunk_id TEXT, chunk_level TEXT, corpus_item_id TEXT, source_kind TEXT,
            content_hash TEXT, content_text TEXT, status TEXT
        );
        """
    )
    # skip full run_migrations - schema already has needed cols for recon tests
    return conn


def test_plan_proposes_request_count_only(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.execute(
        """INSERT INTO source_checkpoints
           (checkpoint_id, run_id, source_type, ticker, date, status, request_count,
            logical_fetch_id, cell_id, endpoint_name, pages_received, items_received)
           VALUES ('ck1','run1','fmp_fundamentals','AAPL','2026-07-23','success',0,
            'lf1','cell1','balance_sheet',9,9)"""
    )
    conn.execute(
        """INSERT INTO provider_request_attempts
           (request_id, run_id, logical_fetch_id, source_type, provider, endpoint_name,
            ticker_or_series, window_start, window_end, attempt_no, page_no,
            request_fingerprint, started_at, status, raw_asset_id)
           VALUES ('r1','run1','lf1','fmp_fundamentals','fmp','balance_sheet',
            'AAPL','2026-07-23','2026-07-23',1,1,'fp','t','SUCCEEDED','raw:1')"""
    )
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw:1','AAPL','fmp','2026-07-23','t',x'00')"""
    )
    conn.commit()
    conn.close()
    plan = plan_checkpoint_reconciliation(
        db, lineage_run_ids=["run1"], source_type="fmp_fundamentals", endpoint_name="balance_sheet"
    )
    assert len(plan.changes) == 1
    assert plan.changes[0].before_request_count == 0
    assert plan.changes[0].after_request_count == 1


def test_dry_run_true_zero_writes(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.execute(
        """INSERT INTO source_checkpoints
           (checkpoint_id, run_id, source_type, ticker, date, status, request_count,
            logical_fetch_id, cell_id, endpoint_name)
           VALUES ('ck1','run1','fmp_fundamentals','AAPL','d','success',0,'lf1','cell1','balance_sheet')"""
    )
    conn.execute(
        """INSERT INTO provider_request_attempts
           (request_id, run_id, logical_fetch_id, source_type, provider, endpoint_name,
            ticker_or_series, window_start, window_end, attempt_no, page_no,
            request_fingerprint, started_at, status, raw_asset_id)
           VALUES ('r1','run1','lf1','fmp','fmp','balance_sheet','AAPL','d','d',1,1,'fp','t','SUCCEEDED','raw:1')"""
    )
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw:1','AAPL','fmp','d','t',x'00')"""
    )
    conn.commit()
    conn.close()
    plan = plan_checkpoint_reconciliation(db, lineage_run_ids=["run1"])
    apply_checkpoint_reconciliation(db, plan, expected_plan_hash=plan.plan_hash, dry_run=True)
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT request_count FROM source_checkpoints").fetchone()[0] == 0
    conn.close()


def test_apply_wrong_run_id_refused(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.close()
    plan = plan_checkpoint_reconciliation(db, lineage_run_ids=["none"])
    with pytest.raises(ValueError):
        apply_checkpoint_reconciliation(db, plan, expected_plan_hash="wrong", dry_run=False)


def test_apply_empty_change_list_noop(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.close()
    plan = plan_checkpoint_reconciliation(db, lineage_run_ids=["none"])
    assert plan.changes == ()
    apply_checkpoint_reconciliation(db, plan, expected_plan_hash=plan.plan_hash, dry_run=False)


def test_second_apply_idempotent(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.execute(
        """INSERT INTO source_checkpoints
           (checkpoint_id, run_id, source_type, ticker, date, status, request_count,
            logical_fetch_id, cell_id, endpoint_name)
           VALUES ('ck1','run1','fmp_fundamentals','AAPL','d','success',0,'lf1','cell1','balance_sheet')"""
    )
    conn.execute(
        """INSERT INTO provider_request_attempts
           (request_id, run_id, logical_fetch_id, source_type, provider, endpoint_name,
            ticker_or_series, window_start, window_end, attempt_no, page_no,
            request_fingerprint, started_at, status, raw_asset_id)
           VALUES ('r1','run1','lf1','fmp','fmp','balance_sheet','AAPL','d','d',1,1,'fp','t','SUCCEEDED','raw:1')"""
    )
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw:1','AAPL','fmp','d','t',x'00')"""
    )
    conn.commit()
    conn.close()
    plan = plan_checkpoint_reconciliation(db, lineage_run_ids=["run1"])
    apply_checkpoint_reconciliation(db, plan, expected_plan_hash=plan.plan_hash, dry_run=False)
    plan2 = plan_checkpoint_reconciliation(db, lineage_run_ids=["run1"])
    assert plan2.changes == ()


def test_refuses_frozen_realpath(tmp_path: Path, monkeypatch):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.close()
    monkeypatch.setattr(
        "catalyst_data.ingestion.checkpoint_reconciliation.FROZEN_PATHS",
        frozenset({os.path.realpath(str(db))}),
    )
    with pytest.raises(Exception):
        plan_checkpoint_reconciliation(db, lineage_run_ids=["x"])


def test_refuses_snapshots_dir_symlink(tmp_path: Path):
    snap = tmp_path / "data" / "snapshots"
    snap.mkdir(parents=True)
    db = snap / "x.db"
    conn = _boot(db)
    conn.close()
    with pytest.raises(PermissionError):
        plan_checkpoint_reconciliation(db, lineage_run_ids=["x"])


def test_refuses_active_promoted_path(tmp_path: Path):
    # treat non-candidates as refused
    other = tmp_path / "other.db"
    conn = _boot(other)
    conn.close()
    with pytest.raises(PermissionError):
        plan_checkpoint_reconciliation(other, lineage_run_ids=["x"])


def test_allows_only_candidates_dir(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.close()
    plan_checkpoint_reconciliation(db, lineage_run_ids=["x"])


def test_no_cli_bypass_flag_exists():
    import inspect
    from catalyst_data.ingestion import checkpoint_reconciliation as m

    sig = inspect.signature(m.apply_checkpoint_reconciliation)
    assert "force" not in sig.parameters
    assert "i_am_repairing" not in sig.parameters


def test_does_not_modify_status_pages_items_raw_asset_id(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.execute(
        """INSERT INTO source_checkpoints
           (checkpoint_id, run_id, source_type, ticker, date, status, request_count,
            logical_fetch_id, cell_id, endpoint_name, pages_received, items_received, raw_asset_id)
           VALUES ('ck1','run1','fmp_fundamentals','AAPL','d','success',0,'lf1','cell1','balance_sheet',3,4,'raw:1')"""
    )
    conn.execute(
        """INSERT INTO provider_request_attempts
           (request_id, run_id, logical_fetch_id, source_type, provider, endpoint_name,
            ticker_or_series, window_start, window_end, attempt_no, page_no,
            request_fingerprint, started_at, status, raw_asset_id)
           VALUES ('r1','run1','lf1','fmp','fmp','balance_sheet','AAPL','d','d',1,1,'fp','t','SUCCEEDED','raw:1')"""
    )
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw:1','AAPL','fmp','d','t',x'00')"""
    )
    conn.commit()
    conn.close()
    plan = plan_checkpoint_reconciliation(db, lineage_run_ids=["run1"])
    apply_checkpoint_reconciliation(db, plan, expected_plan_hash=plan.plan_hash, dry_run=False)
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT status, pages_received, items_received, raw_asset_id, request_count FROM source_checkpoints"
    ).fetchone()
    assert row == ("success", 3, 4, "raw:1", 1)
    conn.close()


def test_atomic_audit_json_before_after_hashes(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.execute(
        """INSERT INTO source_checkpoints
           (checkpoint_id, run_id, source_type, ticker, date, status, request_count,
            logical_fetch_id, cell_id, endpoint_name)
           VALUES ('ck1','run1','fmp_fundamentals','AAPL','d','success',0,'lf1','cell1','balance_sheet')"""
    )
    conn.execute(
        """INSERT INTO provider_request_attempts
           (request_id, run_id, logical_fetch_id, source_type, provider, endpoint_name,
            ticker_or_series, window_start, window_end, attempt_no, page_no,
            request_fingerprint, started_at, status, raw_asset_id)
           VALUES ('r1','run1','lf1','fmp','fmp','balance_sheet','AAPL','d','d',1,1,'fp','t','SUCCEEDED','raw:1')"""
    )
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw:1','AAPL','fmp','d','t',x'00')"""
    )
    conn.commit()
    conn.close()
    plan = plan_checkpoint_reconciliation(db, lineage_run_ids=["run1"])
    audit = tmp_path / "audit.json"
    apply_checkpoint_reconciliation(
        db, plan, expected_plan_hash=plan.plan_hash, dry_run=False, audit_path=audit
    )
    assert audit.exists()
    assert "before_request_count" in audit.read_text()


def test_apply_rowcount_mismatch_full_rollback(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.execute(
        """INSERT INTO source_checkpoints
           (checkpoint_id, run_id, source_type, ticker, date, status, request_count,
            logical_fetch_id, cell_id, endpoint_name)
           VALUES ('ck1','run1','fmp_fundamentals','AAPL','d','success',0,'lf1','cell1','balance_sheet')"""
    )
    conn.execute(
        """INSERT INTO provider_request_attempts
           (request_id, run_id, logical_fetch_id, source_type, provider, endpoint_name,
            ticker_or_series, window_start, window_end, attempt_no, page_no,
            request_fingerprint, started_at, status, raw_asset_id)
           VALUES ('r1','run1','lf1','fmp','fmp','balance_sheet','AAPL','d','d',1,1,'fp','t','SUCCEEDED','raw:1')"""
    )
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw:1','AAPL','fmp','d','t',x'00')"""
    )
    conn.commit()
    conn.close()
    plan = plan_checkpoint_reconciliation(db, lineage_run_ids=["run1"])
    # corrupt before_count so WHERE fails
    from dataclasses import replace
    from catalyst_data.ingestion.checkpoint_reconciliation import ProposedCheckpointChanges

    bad = ProposedCheckpointChanges(
        changes=tuple(
            replace(c, before_request_count=99) for c in plan.changes
        ),
        plan_hash=plan.plan_hash,
    )
    with pytest.raises(RuntimeError, match="drift|rowcount"):
        apply_checkpoint_reconciliation(db, bad, expected_plan_hash=plan.plan_hash, dry_run=False)
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT request_count FROM source_checkpoints").fetchone()[0] == 0
    conn.close()


def test_apply_rejects_new_attempt_after_plan(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.execute(
        """INSERT INTO source_checkpoints
           (checkpoint_id, run_id, source_type, ticker, date, status, request_count,
            logical_fetch_id, cell_id, endpoint_name, raw_asset_id)
           VALUES ('ck1','run1','fmp_fundamentals','AAPL','d','success',0,'lf1','cell1','balance_sheet','raw:1')"""
    )
    conn.execute(
        """INSERT INTO provider_request_attempts
           (request_id, run_id, logical_fetch_id, source_type, provider, endpoint_name,
            ticker_or_series, window_start, window_end, attempt_no, page_no,
            request_fingerprint, started_at, status, raw_asset_id)
           VALUES ('r1','run1','lf1','fmp','fmp','balance_sheet','AAPL','d','d',1,1,?,?, 'STARTED', NULL)""",
        ("a" * 64, "t"),
    )
    conn.execute(
        "UPDATE provider_request_attempts SET status='SUCCEEDED', raw_asset_id='raw:1' WHERE request_id='r1'"
    )
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw:1','AAPL','fmp','d','t',x'00')"""
    )
    conn.commit()
    conn.close()
    plan = plan_checkpoint_reconciliation(db, lineage_run_ids=["run1"])
    assert plan.changes
    # new attempt after plan
    conn = sqlite3.connect(str(db))
    conn.execute(
        """INSERT INTO provider_request_attempts
           (request_id, run_id, logical_fetch_id, source_type, provider, endpoint_name,
            ticker_or_series, window_start, window_end, attempt_no, page_no,
            request_fingerprint, started_at, status)
           VALUES ('r2','run1','lf1','fmp','fmp','balance_sheet','AAPL','d','d',2,1,?,?, 'STARTED')""",
        ("b" * 64, "t"),
    )
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="ledger changed|replan"):
        apply_checkpoint_reconciliation(db, plan, expected_plan_hash=plan.plan_hash, dry_run=False)


def test_apply_second_exact_is_noop(tmp_path: Path):
    db = _cand(tmp_path)
    conn = _boot(db)
    conn.execute(
        """INSERT INTO source_checkpoints
           (checkpoint_id, run_id, source_type, ticker, date, status, request_count,
            logical_fetch_id, cell_id, endpoint_name, raw_asset_id)
           VALUES ('ck1','run1','fmp_fundamentals','AAPL','d','success',0,'lf1','cell1','balance_sheet','raw:1')"""
    )
    conn.execute(
        """INSERT INTO provider_request_attempts
           (request_id, run_id, logical_fetch_id, source_type, provider, endpoint_name,
            ticker_or_series, window_start, window_end, attempt_no, page_no,
            request_fingerprint, started_at, status)
           VALUES ('r1','run1','lf1','fmp','fmp','balance_sheet','AAPL','d','d',1,1,?,?, 'STARTED')""",
        ("a" * 64, "t"),
    )
    conn.execute(
        "UPDATE provider_request_attempts SET status='SUCCEEDED', raw_asset_id='raw:1' WHERE request_id='r1'"
    )
    conn.execute(
        """INSERT INTO raw_assets (asset_id, ticker, source_type, reference_date, fetched_at, content_raw)
           VALUES ('raw:1','AAPL','fmp','d','t',x'00')"""
    )
    conn.commit()
    conn.close()
    plan = plan_checkpoint_reconciliation(db, lineage_run_ids=["run1"])
    apply_checkpoint_reconciliation(db, plan, expected_plan_hash=plan.plan_hash, dry_run=False)
    # second apply exact state
    apply_checkpoint_reconciliation(db, plan, expected_plan_hash=plan.plan_hash, dry_run=False)
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT request_count FROM source_checkpoints").fetchone()[0] == 1
    conn.close()
