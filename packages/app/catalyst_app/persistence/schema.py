"""App-owned V1.1 runtime lifecycle/event/artifact schema (M6-1).

Final Migration TSD §11/§16/§17; Frozen §8–9. The app owns the new-write
tables: runs (lifecycle + idempotency backstops), run_events
(UNIQUE(run_id, seq)), and run_artifacts. The agents trace schema
(``catalyst_agents.trace.schema``) is intentionally untouched and remains
legacy-readable only; it never receives V1.1 tables or lifecycle columns.

SQLite itself backstops admission (Grok SQLITE-01): a unique index on non-NULL
``idempotency_key`` and a partial unique index on ``request_hash`` while the
run is capacity-bearing. Admission still re-checks both under BEGIN IMMEDIATE;
process memory is never the idempotency authority.

RW schema initialization enables WAL. Per-operation connections configure
foreign keys, busy timeout 5000, and the row factory; read-only connections
never attempt to change journal mode.
"""
from __future__ import annotations

import sqlite3
from typing import Any

SCHEMA_VERSION = "runtime_v1"

BUSY_TIMEOUT_MS = 5000

# Capacity-bearing states are exactly the admission-bound states (RUNTIME-01).
_ACTIVE_STATUSES = "'ACCEPTED','RUNNING','CANCEL_REQUESTED'"

_DDL = f"""
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    lifecycle_status  TEXT NOT NULL CHECK (lifecycle_status IN
                        ('ACCEPTED','RUNNING','CANCEL_REQUESTED','COMPLETED','FAILED','CANCELLED')),
    idempotency_key   TEXT,
    request_hash      TEXT NOT NULL,
    run_manifest_id   TEXT NOT NULL,
    manifest_hash     TEXT NOT NULL,
    capacity_slot     INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    failure_code      TEXT,
    failure_message   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency_key
    ON runs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_active_request_hash
    ON runs(request_hash)
    WHERE lifecycle_status IN ({_ACTIVE_STATUSES});
CREATE INDEX IF NOT EXISTS idx_runs_lifecycle ON runs(lifecycle_status);

CREATE TABLE IF NOT EXISTS run_events (
    run_id         TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    occurred_at    TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    stage          TEXT,
    payload_json   TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_run_events_run_seq
    ON run_events(run_id, seq);

CREATE TABLE IF NOT EXISTS run_artifacts (
    artifact_id   TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    event_seq     INTEGER NOT NULL,
    artifact_type TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    optional      INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (run_id, event_seq) REFERENCES run_events(run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_run_artifacts_run_seq
    ON run_artifacts(run_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_run_artifacts_run_type
    ON run_artifacts(run_id, artifact_type);
"""


def configure_runtime_connection(conn: sqlite3.Connection) -> None:
    """Configure one per-operation runtime connection (Final TSD §16).

    Never changes journal mode here: RW initialization is the only writer of
    the journal-mode pragma; read-only connections must not attempt it.
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def init_runtime_db(conn: sqlite3.Connection) -> None:
    """Create app-owned V1.1 tables on a read-write connection.

    Additive only: historical agents tables (agent_runs/trace_events/
    node_artifacts) and their QUEUED/SUCCEEDED rows are preserved and remain
    readable through the legacy agents reader.
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.executescript(_DDL)
    conn.commit()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


__all__ = [
    "BUSY_TIMEOUT_MS",
    "SCHEMA_VERSION",
    "configure_runtime_connection",
    "init_runtime_db",
    "table_exists",
]
