"""B3 shared DB fixtures — apply_migration_v9 and related helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from catalyst_data.migrations import MIGRATIONS


def apply_migration_v9(conn: sqlite3.Connection) -> int:
    """Apply migration v9 to a temporary database at user_version 8.

    Must be called after the DB is at version 8. Returns the new
    user_version after applying v9.

    Raises RuntimeError if the DB is not at version 8 before migration.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current != 8:
        raise RuntimeError(
            f"Expected user_version=8 before v9 migration, got {current}"
        )

    v9 = [m for m in MIGRATIONS if m.version == 9]
    if not v9:
        raise RuntimeError("Migration v9 not found in registry")
    migration = v9[0]

    for stmt in migration.statements:
        if stmt.strip().startswith("--"):
            continue
        try:
            conn.executescript(stmt)
        except sqlite3.OperationalError as exc:
            err = str(exc).lower()
            if "duplicate column name" in err or "already exists" in err:
                continue
            raise

    conn.execute(f"PRAGMA user_version = {migration.version}")
    conn.commit()
    return conn.execute("PRAGMA user_version").fetchone()[0]
