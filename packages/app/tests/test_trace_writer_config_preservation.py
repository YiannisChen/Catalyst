"""TraceWriter must not overwrite live-run metadata config.

LiveRunRunner marks runs RUNNING before invoking the traced graph. TraceWriter
then enters with an existing RUNNING row and must preserve the JSON config that
contains BYOK model metadata.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from catalyst_agents.trace.schema import init_trace_db
from catalyst_agents.trace.writer import TraceWriter


def test_trace_writer_preserves_existing_running_config() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        _assert_trace_writer_preserves_existing_running_config(Path(tmpdir) / "trace.db")


def _assert_trace_writer_preserves_existing_running_config(db_path: Path) -> None:
    run_config = {
        "query": None,
        "model": {
            "provider": "openrouter",
            "model_id": "google/gemini-2.5-flash",
            "base_url": "https://openrouter.ai/api/v1",
        },
        "config": "mcj_full",
    }

    conn = sqlite3.connect(db_path)
    init_trace_db(conn)
    conn.execute(
        """
        INSERT INTO agent_runs
            (run_id, trace_id, ticker, trade_date, status, started_at, config)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run-1",
            "trace-1",
            "AAPL",
            "2025-09-08",
            "RUNNING",
            "2025-09-08T10:00:00Z",
            json.dumps(run_config, sort_keys=True),
        ),
    )
    conn.commit()
    conn.close()

    with TraceWriter(
        run_id="run-1",
        trace_id="trace-new",
        db_path=db_path,
        ticker="AAPL",
        trade_date="2025-09-08",
        config="mcj_full",
    ):
        pass

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT trace_id, status, config FROM agent_runs WHERE run_id = ?",
        ("run-1",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "trace-1"
    assert row[1] == "RUNNING"
    assert json.loads(row[2]) == run_config
    assert "api_key" not in row[2]


if __name__ == "__main__":
    test_trace_writer_preserves_existing_running_config()
    print("TRACE WRITER CONFIG PRESERVATION TEST PASSED")
