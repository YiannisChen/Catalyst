import sqlite3
import threading
import time

import pytest
from catalyst_data.storage import connect


def test_live_read_waits_for_writer_then_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_SQLITE_LIVE_READ", "1")
    monkeypatch.setenv("CATALYST_SQLITE_READ_TIMEOUT_S", "1")
    path = tmp_path / "live.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE values_table (value INTEGER)")
        db.execute("INSERT INTO values_table VALUES (7)")
    writer = sqlite3.connect(path, timeout=0, check_same_thread=False)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO values_table VALUES (9)")
    def release_writer():
        time.sleep(0.3)
        writer.rollback()
        writer.close()
    slept = []
    real_sleep = time.sleep
    monkeypatch.setattr(connect.time, "sleep", lambda seconds: (slept.append(seconds), real_sleep(seconds))[1])
    released = threading.Thread(target=release_writer)
    released.start()
    with connect.open_readonly(path) as reader:
        assert reader.execute("SELECT value FROM values_table").fetchone()[0] == 7
    released.join()
    assert slept, "query-level lock conflict should trigger exponential retry backoff"


def test_live_read_fails_within_configured_bound_when_writer_keeps_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_SQLITE_LIVE_READ", "1")
    monkeypatch.setenv("CATALYST_SQLITE_READ_TIMEOUT_S", "0.2")
    path = tmp_path / "locked.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE values_table (value INTEGER)")
    writer = sqlite3.connect(path, timeout=0, check_same_thread=False)
    writer.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with connect.open_readonly(path) as reader:
            with pytest.raises(connect.SQLiteReadTimeoutError, match="locked"):
                reader.execute("SELECT * FROM values_table")
    finally:
        writer.rollback()
        writer.close()
    assert time.monotonic() - started < 1.0


def test_default_read_remains_immutable(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALYST_SQLITE_LIVE_READ", raising=False)
    captured = {}

    class FakeConnection:
        def execute(self, _sql):
            pass

    def fake_connect(database, **kwargs):
        captured["uri"] = database
        captured["kwargs"] = kwargs
        return FakeConnection()

    monkeypatch.setattr(connect.sqlite3, "connect", fake_connect)
    connect._connect_readonly(tmp_path / "snapshot.db")
    assert "immutable=1" in captured["uri"]


def test_non_lock_sqlite_errors_are_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_SQLITE_READ_TIMEOUT_S", "1")
    path = tmp_path / "missing-table.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE exists_table (value INTEGER)")
    attempts = []
    real_sleep = time.sleep
    monkeypatch.setattr(connect.time, "sleep", lambda seconds: (attempts.append(seconds), real_sleep(seconds))[1])
    with connect.open_readonly(path, immutable=False) as reader:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            reader.execute("SELECT * FROM absent_table")
    assert attempts == []
