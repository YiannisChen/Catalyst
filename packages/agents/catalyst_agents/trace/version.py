from __future__ import annotations

from datetime import datetime, timezone
import sqlite3


TRACE_SCHEMA_VERSION = "2.0.0"
SUPPORTED_TRACE_SCHEMA_VERSIONS = {"1.0.0", TRACE_SCHEMA_VERSION}


class UnsupportedTraceSchemaVersion(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_trace_schema_version(conn: sqlite3.Connection) -> str | None:
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trace_schema_version'"
    ).fetchone()
    if exists is None:
        return None
    row = conn.execute("SELECT schema_version FROM trace_schema_version WHERE singleton_id = 1").fetchone()
    return row[0] if row else None


def assert_supported_trace_schema(conn: sqlite3.Connection) -> None:
    version = get_trace_schema_version(conn)
    if version is None:
        return
    if version not in SUPPORTED_TRACE_SCHEMA_VERSIONS:
        raise UnsupportedTraceSchemaVersion(f"unsupported trace schema version: {version}")


def ensure_trace_schema_version(conn: sqlite3.Connection) -> None:
    assert_supported_trace_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trace_schema_version (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
            schema_version TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    current = get_trace_schema_version(conn)
    if current is None:
        conn.execute(
            "INSERT INTO trace_schema_version (singleton_id, schema_version, applied_at) VALUES (1, ?, ?)",
            (TRACE_SCHEMA_VERSION, _utc_now()),
        )
    elif current != TRACE_SCHEMA_VERSION:
        conn.execute(
            "UPDATE trace_schema_version SET schema_version = ?, applied_at = ? WHERE singleton_id = 1",
            (TRACE_SCHEMA_VERSION, _utc_now()),
        )
