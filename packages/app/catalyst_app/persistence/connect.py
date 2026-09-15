"""App read-write connection factory (Final TSD §16, M6-2).

``open_rw`` is a short-lived context manager owning at most one transaction
per operation. It configures the runtime connection (row factory, foreign
keys, busy timeout 5000) and closes on success or exception. Schema
initialization is performed once by the app lifespan, not per connection.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
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
    """Open the app-owned runtime DB read-only without immutable semantics.

    The app runtime database is writable elsewhere and may have an active WAL;
    ``immutable=1`` is reserved for the externally-owned Q-001 data database.
    """
    path = Path(db_path)
    conn = sqlite3.connect(path, check_same_thread=False)
    configure_runtime_connection(conn)
    conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    try:
        yield conn
    finally:
        conn.close()


__all__ = ["DEFAULT_BUSY_TIMEOUT_MS", "open_readonly", "open_rw"]
