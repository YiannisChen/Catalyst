"""Composable SQLite savepoints for data writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def atomic_savepoint(conn: sqlite3.Connection, name: str) -> Iterator[None]:
    if not name.isidentifier():
        raise ValueError("savepoint name must be an identifier")
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")
