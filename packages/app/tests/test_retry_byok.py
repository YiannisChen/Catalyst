"""Real LiveRunService retry tests with temp SQLite DB.

Tests actual retry_run behavior: BYOK retry without fresh credential
returns failure; BYOK retry with fresh credential creates new run;
new config contains metadata but never api_key.
"""
import sys
sys.path.insert(0, 'packages/agents')
sys.path.insert(0, 'packages/data-core')
sys.path.insert(0, 'packages/app')

import json
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

from catalyst_agents.trace.schema import init_trace_db
from catalyst_agents.runtime.service import LiveRunService
from runtime_fixture import prepare_runtime_db

TEST_API_KEY = "sk-test-secret-retry-key-abc123"

BYOK_META = {
    "provider": "openrouter",
    "model_id": "google/gemini-2.5-flash",
    "base_url": "https://openrouter.ai/api/v1",
}


def _dummy_graph_factory(*args, **kwargs):
    """Dummy — never called by retry tests (only by run_one / run_next)."""
    raise RuntimeError("graph_factory should not be called in retry tests")


def _make_service(db_path: Path) -> LiveRunService:
    return LiveRunService(
        db_path=db_path,
        graph_factory=_dummy_graph_factory,
    )


def _insert_terminal_byok_run(db_path: Path) -> str:
    """Insert a terminal SUCCEEDED run with BYOK metadata in config."""
    prepare_runtime_db(db_path)
    conn = sqlite3.connect(str(db_path))
    run_id = uuid4().hex
    trace_id = uuid4().hex
    config = json.dumps({"model": BYOK_META, "config": "mcj_full"}, sort_keys=True)
    conn.execute(
        """INSERT INTO agent_runs
           (run_id, trace_id, ticker, trade_date, status, queued_at, started_at, ended_at, config)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, trace_id, "AAPL", "2025-09-08", "SUCCEEDED",
         "2025-09-08T10:00:00Z", "2025-09-08T10:00:00Z", "2025-09-08T10:00:01Z", config),
    )
    conn.commit()
    conn.close()
    return run_id


def _read_config(db_path: Path, run_id: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT config FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}


# ── Tests ──

def test_retry_byok_without_fresh_credential_fails():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        parent_run_id = _insert_terminal_byok_run(db_path)
        service = _make_service(db_path)

        result = service.retry_run(parent_run_id, model=None)
        assert result["ok"] is False
        assert result["failure"]["sub_reason"] == "credential_required"
        print("PASS: retry_run with model=None returns credential_required")


def test_retry_byok_with_fresh_credential_creates_run():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        parent_run_id = _insert_terminal_byok_run(db_path)
        service = _make_service(db_path)

        fresh_meta = dict(BYOK_META)  # caller supplies new metadata
        result = service.retry_run(parent_run_id, model=fresh_meta)
        assert result["ok"] is True
        assert result["run_id"] is not None
        assert result["run_id"] != parent_run_id
        print("PASS: retry_run with fresh metadata creates new run")


def test_new_run_config_contains_metadata_but_not_api_key():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        parent_run_id = _insert_terminal_byok_run(db_path)
        service = _make_service(db_path)

        result = service.retry_run(parent_run_id, model=dict(BYOK_META))
        new_run_id = result["run_id"]
        config = _read_config(db_path, new_run_id)

        model_in_config = config.get("model", {})
        assert isinstance(model_in_config, dict)
        assert model_in_config.get("provider") == "openrouter"
        assert model_in_config.get("model_id") == "google/gemini-2.5-flash"
        # api_key must never be in persisted config
        assert "api_key" not in model_in_config
        assert TEST_API_KEY not in json.dumps(config)
        print("PASS: new run config has metadata but no api_key")


def test_inserted_parent_config_has_no_api_key():
    """Verify original test fixture: agent_runs.config must not contain api_key."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        run_id = _insert_terminal_byok_run(db_path)
        config = _read_config(db_path, run_id)
        config_str = json.dumps(config)
        assert "api_key" not in config_str
        assert TEST_API_KEY not in config_str
        print("PASS: parent run config has no api_key")


def test_retry_error_message_sanitized():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        run_id = _insert_terminal_byok_run(db_path)
        service = _make_service(db_path)
        result = service.retry_run(run_id, model=None)
        msg = result["failure"]["message"]
        assert "sk-" not in msg
        assert TEST_API_KEY not in msg
        print("PASS: retry error message is sanitized")


print()
print("ALL RETRY BYOK TESTS PASSED")
