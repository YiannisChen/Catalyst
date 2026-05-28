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
    queued_at         TEXT,
    started_at        TEXT,
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

CREATE TABLE IF NOT EXISTS node_artifacts (
    run_id         TEXT NOT NULL,
    event_seq      INTEGER NOT NULL,
    node           TEXT NOT NULL,
    artifact_type  TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, event_seq, artifact_type),
    FOREIGN KEY (run_id, event_seq) REFERENCES trace_events(run_id, event_seq)
);
CREATE INDEX IF NOT EXISTS idx_node_artifacts_run_event ON node_artifacts(run_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_node_artifacts_run_type ON node_artifacts(run_id, artifact_type);

CREATE TABLE IF NOT EXISTS run_links (
    run_id         TEXT NOT NULL,
    parent_run_id  TEXT NOT NULL,
    link_type      TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (run_id, parent_run_id, link_type),
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id),
    FOREIGN KEY (parent_run_id) REFERENCES agent_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_run_links_parent ON run_links(parent_run_id);
"""


def init_trace_db(conn: sqlite3.Connection) -> None:
    """Create trace tables and indexes if they do not already exist."""
    conn.executescript(_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
    if "queued_at" not in columns:
        conn.execute("ALTER TABLE agent_runs ADD COLUMN queued_at TEXT")
    conn.commit()
    _migrate_agent_runs_started_at_nullable(conn)
    conn.commit()


def _migrate_agent_runs_started_at_nullable(conn: sqlite3.Connection) -> None:
    columns = {
        row[1]: row
        for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
    }
    started_at = columns.get("started_at")
    if started_at is None or int(started_at[3] or 0) == 0:
        return
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.commit()
    if foreign_keys_enabled:
        conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS agent_runs_new;

            CREATE TABLE agent_runs_new (
                run_id            TEXT PRIMARY KEY,
                trace_id          TEXT NOT NULL,
                ticker            TEXT,
                trade_date        TEXT,
                status            TEXT,
                queued_at         TEXT,
                started_at        TEXT,
                ended_at          TEXT,
                total_latency_ms  INTEGER,
                total_cost_usd    REAL DEFAULT 0.0,
                model_id_per_role TEXT,
                config            TEXT,
                error_type        TEXT,
                error_message     TEXT
            );

            INSERT INTO agent_runs_new
                (run_id, trace_id, ticker, trade_date, status, queued_at, started_at, ended_at,
                 total_latency_ms, total_cost_usd, model_id_per_role, config, error_type, error_message)
            SELECT run_id, trace_id, ticker, trade_date, status, queued_at, started_at, ended_at,
                   total_latency_ms, total_cost_usd, model_id_per_role, config, error_type, error_message
            FROM agent_runs;

            DROP TABLE agent_runs;
            ALTER TABLE agent_runs_new RENAME TO agent_runs;
            CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at ON agent_runs(started_at);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_trace_id ON agent_runs(trace_id);
            """
        )
        conn.commit()
    finally:
        if foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys=ON")
