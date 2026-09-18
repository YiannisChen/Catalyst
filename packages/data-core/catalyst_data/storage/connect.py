"""Read-only SQLite connection factory (Final TSD §16, M6-2).

Data-core owns the read-only factory used by retrieval and offline reads.
Connections are per-operation: each ``open_readonly`` context returns one
configured read-only connection that is closed on success or exception. A
read-only connection never attempts to change journal mode.

``make_readonly_factory`` is the raw-connection seam for components (such as
the production hybrid retriever) that acquire and close their own connection
per operation.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Callable, Iterator

DEFAULT_BUSY_TIMEOUT_MS = 5000


def _connect_readonly(
    db_path: str | Path, *, timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    uri = f"file:{path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    # Never changes journal mode: read-only connections must not attempt it.
    return conn


@contextmanager
def open_readonly(
    db_path: str | Path, *, timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> Iterator[sqlite3.Connection]:
    """Open one read-only connection for a bounded operation.

    Closes on success or exception. The connection is never cached. The
    context-manager API configures the row factory; the raw factory used by
    retrieval remains tuple-compatible (index-based access).
    """
    conn = _connect_readonly(db_path, timeout_ms=timeout_ms)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def make_readonly_factory(
    db_path: str | Path, *, timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> Callable[[], sqlite3.Connection]:
    """Return a per-operation read-only connection factory.

    Components that own their connection lifecycle (for example
    ``ProductionHybridRetriever``) call the factory and close the returned
    connection themselves; the factory never returns a cached connection.
    """
    path = Path(db_path)

    def _factory() -> sqlite3.Connection:
        return _connect_readonly(path, timeout_ms=timeout_ms)

    return _factory


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "make_readonly_factory",
    "open_readonly",
]
