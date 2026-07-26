"""Tests for live_runner.py — gated --live --confirm path (3F.1 Part C).

All tests use mocked env and connectors — ZERO network calls.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
import unittest.mock as mock

import pytest


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """Run a CLI command via subprocess."""
    env = os.environ.copy()
    # Always strip API keys for tests
    for key in ["POLYGON_API_KEY", "FINNHUB_API_KEY", "FRED_API_KEY", "SEC_USER_AGENT"]:
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "catalyst_data.cli_index"] + args,
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
    )


# ---------------------------------------------------------------------------
# TC1 — --live without --confirm exits early even without keys (DISCRIMINATING)
# ---------------------------------------------------------------------------

def test_live_without_confirm_exits_early_even_without_keys(tmp_path: Path):
    """--live without --confirm exits 0 with friendly message, even if no keys set."""
    result = _run_cli([
        "update-news", "--live", "--db", str(tmp_path / "test.db"),
    ])
    combined = result.stdout + result.stderr
    # Must exit 0 (not crash)
    assert result.returncode == 0
    # Must mention confirm requirement
    assert "confirm" in combined.lower() or "--confirm" in combined


# ---------------------------------------------------------------------------
# TC2 — frozen DB refused
# ---------------------------------------------------------------------------

def test_frozen_db_refused():
    """--live --confirm with frozen DB realpath → RuntimeError."""
    frozen = os.path.realpath("../../data/catalyst_eval_frozen_v2.db")
    result = _run_cli([
        "update-news", "--live", "--confirm", "--db", frozen,
    ])
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "frozen" in combined.lower()


# ---------------------------------------------------------------------------
# TC3 — missing API key raises after confirm
# ---------------------------------------------------------------------------

def test_missing_api_key_raises_after_confirm(tmp_path: Path):
    """--live --confirm with missing key → error about the missing var."""
    # Create a temp DB so the frozen check passes
    db_path = str(tmp_path / "test.db")
    import sqlite3
    from catalyst_data.storage.sqlite import init_db
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.execute("INSERT INTO ohlcv (symbol, date, open, high, low, close, volume) VALUES ('AAPL', '2025-06-15', 100, 110, 90, 105, 1000)")
    conn.commit()
    conn.close()

    # Ensure key is unset
    env = os.environ.copy()
    env.pop("POLYGON_API_KEY", None)
    env.pop("FINNHUB_API_KEY", None)

    result = subprocess.run(
        [sys.executable, "-m", "catalyst_data.cli_index",
         "update-news", "--live", "--confirm", "--db", db_path,
         "--sources", "polygon_news"],
        capture_output=True, text=True, env=env,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "POLYGON_API_KEY" in combined or "api_key" in combined.lower()


# ---------------------------------------------------------------------------
# TC4 — redacted config echo
# ---------------------------------------------------------------------------

def test_redacted_config_echo():
    """build_polygon_fetcher echoes redacted key, never full key."""
    full_key = "abcdef1234567890abcdef1234567890abcdef12"

    with mock.patch.dict(os.environ, {"POLYGON_API_KEY": full_key}):
        from catalyst_data.live_runner import build_polygon_fetcher

        # Capture stdout
        import io
        saved_stdout = sys.stdout
        try:
            captured = io.StringIO()
            sys.stdout = captured
            fetcher, limiter = build_polygon_fetcher(trust_env=False)
            output = captured.getvalue()
        finally:
            sys.stdout = saved_stdout

        # Must NOT contain full key
        assert full_key not in output, f"Full key leaked in output: {output}"
        # Must contain redacted version (at least partial)
        assert "REDACTED" in output or "[REDACTED]" in output or "****" in output or "abcd" in output


# ---------------------------------------------------------------------------
# TC5 — polygon fetcher constructed
# ---------------------------------------------------------------------------

def test_polygon_fetcher_constructed():
    """Valid mock env → returns callable + limiter."""
    from catalyst_data.live_runner import build_polygon_fetcher
    from catalyst_data.rate_limiter import TokenBucketLimiter

    with mock.patch.dict(os.environ, {"POLYGON_API_KEY": "test_key_12345"}):
        fetcher, limiter = build_polygon_fetcher(trust_env=False)

    assert callable(fetcher)
    assert isinstance(limiter, TokenBucketLimiter)


# ---------------------------------------------------------------------------
# TC6 — finnhub fetcher constructed
# ---------------------------------------------------------------------------

def test_finnhub_fetcher_constructed():
    """Valid mock env → returns callable + limiter."""
    from catalyst_data.live_runner import build_finnhub_fetcher
    from catalyst_data.rate_limiter import TokenBucketLimiter

    with mock.patch.dict(os.environ, {"FINNHUB_API_KEY": "test_key_67890"}):
        fetcher, limiter = build_finnhub_fetcher(trust_env=False)

    assert callable(fetcher)
    assert isinstance(limiter, TokenBucketLimiter)


# ---------------------------------------------------------------------------
# TC7 — default dry-run unchanged
# ---------------------------------------------------------------------------

def test_dry_run_default_unchanged(tmp_path: Path):
    """No --live flag → dry-run behavior (no crash, no network)."""
    db_path = str(tmp_path / "test.db")
    import sqlite3
    from catalyst_data.storage.sqlite import init_db
    conn = sqlite3.connect(db_path)
    init_db(conn)
    # Populate minimal OHLCV so freshness doesn't crash
    conn.execute("INSERT INTO ohlcv (symbol, date, open, high, low, close, volume) VALUES ('AAPL', '2025-06-15', 100, 110, 90, 105, 1000)")
    conn.commit()
    conn.close()

    result = _run_cli([
        "update-news", "--db", db_path, "--dry-run",
    ])
    combined = result.stdout + result.stderr
    assert result.returncode == 0
    assert "dry-run" in combined.lower() or "ZERO" in combined


# ---------------------------------------------------------------------------
# TC8 — live pipeline accepts fetch_fn (mock)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_pipeline_accepts_fetch_fn(tmp_path: Path):
    """Mocked fetch_fn → pipeline runs, returns result dict."""
    db_path = str(tmp_path / "test.db")
    import sqlite3
    from catalyst_data.storage.sqlite import init_db
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.execute("INSERT INTO ohlcv (symbol, date, open, high, low, close, volume) VALUES ('AAPL', '2025-06-15', 100, 110, 90, 105, 1000)")
    conn.commit()
    conn.close()

    from catalyst_data.update_pipeline import run_update_batch

    async def mock_fetch_fn(*args, **kwargs):
        return {"status": "ok", "results": []}

    result = await run_update_batch(
        db_path,
        tickers=["AAPL"],
        sources=["polygon_news"],
        from_date="2025-06-01",
        to_date="2025-06-02",
        fetch_fn=mock_fetch_fn,
        dry_run=True,
    )
    assert "cells_total" in result
    assert isinstance(result.get("cells_total"), int)
