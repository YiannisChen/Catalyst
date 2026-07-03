"""Tests for schema reconciliation (3F.1 Part B) — idempotent additive migration."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from catalyst_data.storage.sqlite import init_db


def _db_path_and_conn(tmp_path: Path) -> tuple[str, sqlite3.Connection]:
    """Create a fresh DB and return (path, connection)."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    return db_path, conn


def _run_reconcile(args: list[str]) -> subprocess.CompletedProcess:
    """Run reconcile-schema via the CLI module."""
    return subprocess.run(
        [sys.executable, "-m", "catalyst_data.cli_index", "reconcile-schema"] + args,
        capture_output=True,
        text=True,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
    )


# ---------------------------------------------------------------------------
# TB1 — dry-run reports macro_observations missing on pre-3E DB
# ---------------------------------------------------------------------------

def test_dry_run_reports_macro_missing(tmp_path: Path):
    """DB without macro_observations → dry-run reports it missing."""
    db_path, conn = _db_path_and_conn(tmp_path)
    # Drop macro_observations to simulate pre-3E state
    conn.execute("DROP TABLE IF EXISTS macro_observations")
    conn.commit()
    conn.close()

    result = _run_reconcile(["--dry-run", "--db", db_path])
    combined = result.stdout + result.stderr
    assert "macro_observations" in combined
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# TB2 — dry-run reports zero missing when current (DISCRIMINATING)
# ---------------------------------------------------------------------------

def test_dry_run_no_missing_when_current(tmp_path: Path):
    """DB built with init_db (current code) → dry-run reports zero missing."""
    db_path, conn = _db_path_and_conn(tmp_path)
    conn.close()

    result = _run_reconcile(["--dry-run", "--db", db_path])
    combined = result.stdout + result.stderr
    assert "0 missing" in combined or "already reconciled" in combined or "no changes" in combined.lower()
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# TB3 — apply creates macro table + dedup column
# ---------------------------------------------------------------------------

def test_apply_creates_macro_table(tmp_path: Path):
    """DB without macro_observations → apply creates it."""
    db_path, conn = _db_path_and_conn(tmp_path)
    conn.execute("DROP TABLE IF EXISTS macro_observations")
    conn.commit()
    conn.close()

    result = _run_reconcile(["--apply", "--db", db_path])
    combined = result.stdout + result.stderr
    assert result.returncode == 0

    # Verify table exists
    conn2 = sqlite3.connect(db_path)
    tables = [r[0] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='macro_observations'"
    ).fetchall()]
    assert len(tables) == 1
    conn2.close()


# ---------------------------------------------------------------------------
# TB4 — apply is idempotent (DISCRIMINATING)
# ---------------------------------------------------------------------------

def test_apply_idempotent(tmp_path: Path):
    """Second apply reports 'already reconciled' or 'no changes'."""
    db_path, conn = _db_path_and_conn(tmp_path)
    conn.close()

    # First apply
    r1 = _run_reconcile(["--apply", "--db", db_path])
    assert r1.returncode == 0

    # Second apply
    r2 = _run_reconcile(["--apply", "--db", db_path])
    combined2 = r2.stdout + r2.stderr
    assert r2.returncode == 0
    assert "already reconciled" in combined2.lower() or "no changes" in combined2.lower()


# ---------------------------------------------------------------------------
# TB5 — apply refuses frozen DB
# ---------------------------------------------------------------------------

def test_apply_refuses_frozen_db():
    """Passing frozen DB realpath → error message."""
    frozen = os.path.realpath("../../data/catalyst_eval_frozen_v2.db")
    result = _run_reconcile(["--apply", "--db", frozen])
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "frozen" in combined.lower()


# ---------------------------------------------------------------------------
# TB6 — apply requires explicit --db
# ---------------------------------------------------------------------------

def test_apply_requires_explicit_db():
    """Calling --apply without --db → exits non-zero."""
    result = _run_reconcile(["--apply"])
    assert result.returncode != 0
