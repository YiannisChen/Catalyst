"""App read-write connection factory (Final TSD §16, M6-2).

``open_rw`` is a short-lived context manager owning at most one transaction
per operation. It configures the runtime connection (row factory, foreign
keys, busy timeout 5000) and closes on success or exception. Schema
initialization is performed once by the app lifespan, not per connection.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from catalyst_app.persistence.schema import configure_runtime_connection

DEFAULT_BUSY_TIMEOUT_MS = 5000


@contextmanager
def open_rw(
    db_path: str | Path, *, timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> Iterator[sqlite3.Connection]:
    """Open one short-lived read-write connection for one operation."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    configure_runtime_connection(conn)
    conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def open_readonly(
    db_path: str | Path, *, timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> Iterator[sqlite3.Connection]:
    """Open a SQLite file read-only without immutable semantics.

    The file may be written elsewhere and may have an active WAL;
    ``immutable=1`` is reserved for externally-owned frozen data databases.
    """
    path = Path(db_path)
    uri = f"{path.expanduser().resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    configure_runtime_connection(conn)
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    try:
        yield conn
    finally:
        conn.close()


__all__ = ["DEFAULT_BUSY_TIMEOUT_MS", "open_readonly", "open_rw"]
