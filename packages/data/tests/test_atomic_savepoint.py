"""Transaction boundaries shared by active data writers."""

import sqlite3

import pytest
from catalyst_data.storage.atomic import atomic_savepoint


def test_successful_savepoint_remains_under_caller_rollback() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (value INTEGER)")
    with atomic_savepoint(conn, "outer"):
        conn.execute("INSERT INTO items VALUES (1)")
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_nested_failure_rolls_back_only_its_savepoint() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (value INTEGER)")
    with atomic_savepoint(conn, "outer"):
        conn.execute("INSERT INTO items VALUES (1)")
        with pytest.raises(RuntimeError, match="fail inner"):
            with atomic_savepoint(conn, "inner"):
                conn.execute("INSERT INTO items VALUES (2)")
                raise RuntimeError("fail inner")
        conn.execute("INSERT INTO items VALUES (3)")
    conn.commit()
    assert conn.execute("SELECT value FROM items ORDER BY value").fetchall() == [(1,), (3,)]
