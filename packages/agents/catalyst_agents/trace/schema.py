"""SQLite schema for run-level and node-level trace persistence."""
from __future__ import annotations

import sqlite3


_DDL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id            TEXT PRIMARY KEY,
    trace_id          TEXT NOT NULL,
    ticker            TEXT,
    trade_date        TEXT,
    status            TEXT,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    total_latency_ms  INTEGER,
    total_cost_usd    REAL DEFAULT 0.0,
    model_id_per_role TEXT,
    config            TEXT,
    error_type        TEXT,
    error_message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at ON agent_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_trace_id ON agent_runs(trace_id);

CREATE TABLE IF NOT EXISTS trace_events (
    run_id         TEXT NOT NULL,
    trace_id       TEXT NOT NULL,
    event_seq      INTEGER NOT NULL,
    node           TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT NOT NULL,
    latency_ms     INTEGER NOT NULL,
    model_id       TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL,
    decision       TEXT,
    error_type     TEXT,
    error_message  TEXT,
    status_before  TEXT,
    status_after   TEXT,
    PRIMARY KEY (run_id, event_seq),
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_trace_events_run_seq ON trace_events(run_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_trace_events_error_type ON trace_events(error_type);
"""


def init_trace_db(conn: sqlite3.Connection) -> None:
    """Create trace tables and indexes if they do not already exist."""
    conn.executescript(_DDL)
    conn.commit()

