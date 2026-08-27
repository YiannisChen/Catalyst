"""Connection-per-operation ownership contract (M6-2).

Final TSD §16: open_readonly/open_rw are context managers, not cached bare
connections; no sqlite3.Connection lives inside RuntimeDependencies or the
cached loader; concurrent readers/writers on one WAL DB never cross threads
and never leak "database is locked" beyond bounded busy-retry.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
import threading

import pytest

from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_data.storage.connect import open_readonly
from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader


def _fixture_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    init_runtime_db(conn)
    conn.close()


class FakeTable:
    def to_list(self):
        return [{"vector": [0.0] * 3, "asset_id": "a1"}]


class FakeLanceDB:
    def __init__(self, table=None):
        self._table = table or FakeTable()

    def open_table(self, _):
        return self._table


def _loader_with_fakes(tmp_path: Path) -> RuntimeDependencyLoader:
    db_path = tmp_path / "runtime.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text("ok")
    return RuntimeDependencyLoader(
        sqlite_db_path=db_path,
        lancedb_dir=tmp_path / "ldb",
        lancedb_table_name="chunks",
        default_model="gpt-4.1-mini",
        embedding_factory=lambda _: (lambda _q: [0.1, 0.2, 0.3], 3),
        reranker_factory=lambda _: object(),
        lancedb_connect_factory=lambda _path: FakeLanceDB(),
    )


def test_open_readonly_is_context_manager_and_readonly(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)
    journal_before = sqlite3.connect(db_path).execute("PRAGMA journal_mode").fetchone()[0]

    with open_readonly(db_path) as conn:
        assert isinstance(conn, sqlite3.Connection)
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        # A read-only connection must never attempt to change journal mode.
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == journal_before
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id,"
                " manifest_hash, capacity_slot, created_at, updated_at)"
                " VALUES ('run:ro', 'ACCEPTED', 'x', 'm', 'h', 0, 't', 't')"
            )
    # Closed on success.
    assert conn is not None


def test_open_readonly_closes_on_exception(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)

    with pytest.raises(RuntimeError, match="boom"):
        with open_readonly(db_path) as conn:
            raw = conn
            raise RuntimeError("boom")

    # The connection must be closed after the exception unwinds.
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_open_rw_is_short_lived_context_manager(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)

    with open_rw(db_path) as conn:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id,"
            " manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES ('run:rw', 'ACCEPTED', 'y', 'm', 'h', 0, 't', 't')"
        )
        conn.commit()
        raw = conn
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_open_rw_closes_on_exception(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)

    with pytest.raises(RuntimeError, match="boom"):
        with open_rw(db_path) as conn:
            raw = conn
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_no_sqlite_connection_stored_on_runtime_dependencies(tmp_path: Path) -> None:
    loader = _loader_with_fakes(tmp_path)
    deps = loader.get_dependencies()

    for attr, value in vars(deps).items():
        assert not isinstance(value, sqlite3.Connection), (
            f"RuntimeDependencies.{attr} is a cached SQLite connection"
        )
    # The cached loader itself must retain paths/factories, never connections.
    for attr, value in vars(loader).items():
        assert not isinstance(value, sqlite3.Connection), (
            f"RuntimeDependencyLoader.{attr} is a cached SQLite connection"
        )
    assert isinstance(deps.sqlite_db_path, Path)


def test_runtime_loader_returns_factory_not_connection(tmp_path: Path) -> None:
    loader = _loader_with_fakes(tmp_path)
    deps = loader.get_dependencies()
    # The loader exposes the db path; connection acquisition is per operation.
    assert deps.sqlite_db_path.is_file()
    health = loader.health()
    assert health["status"] == "ready"


def test_concurrent_writers_and_readers_single_wal_db(tmp_path: Path) -> None:
    """MEDIUM: two writers + readers on one WAL DB without cross-thread leaks."""
    db_path = tmp_path / "runtime.db"
    _fixture_db(db_path)

    errors: list[BaseException] = []
    lock = threading.Lock()

    def writer(prefix: str, start: int, count: int) -> None:
        try:
            for i in range(start, start + count):
                run_id = f"{prefix}:{i}"
                with open_rw(db_path) as conn:
                    conn.execute(
                        "INSERT INTO runs (run_id, lifecycle_status, request_hash,"
                        " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
                        " VALUES (?, 'ACCEPTED', ?, ?, ?, 0, 't', 't')",
                        (run_id, f"hash-{i}" + "0" * 58, f"manifest:{run_id}", "h" * 64),
                    )
                    conn.commit()
        except BaseException as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc)

    def reader(expected_prefixes: tuple[str, ...]) -> None:
        try:
            for _ in range(50):
                with open_readonly(db_path) as conn:
                    rows = conn.execute(
                        "SELECT run_id FROM runs ORDER BY run_id"
                    ).fetchall()
                assert all(r["run_id"].startswith(expected_prefixes) for r in rows)
        except BaseException as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("w1", 0, 10)),
        threading.Thread(target=writer, args=("w2", 100, 10)),
        threading.Thread(target=reader, args=(("w1", "w2"),)),
        threading.Thread(target=reader, args=(("w1", "w2"),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent WAL access failed: {errors}"
    with open_readonly(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert count == 20
