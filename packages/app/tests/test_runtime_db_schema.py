"""App-owned V1.1 runtime DB schema contract (M6-1).

Final Migration TSD §11/§16/§17; Grok SQLITE-01/RUNTIME-01. The app owns the
new-write tables (runs/run_events/run_artifacts); the agents trace schema stays
legacy-readable only and never receives V1.1 tables or lifecycle columns.
SQLite itself backstops admission via a unique index on non-NULL
idempotency_key and a partial unique index on active request_hash.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from catalyst_app.persistence.schema import init_runtime_db
from catalyst_agents.trace.schema import init_trace_db

_VALID_LIFECYCLE = {
    "ACCEPTED",
    "RUNNING",
    "CANCEL_REQUESTED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
}


def _rw_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_runtime_db(conn)
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


def _insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str = "run:1",
    status: str = "ACCEPTED",
    idempotency_key: str | None = None,
    request_hash: str = "a" * 64,
    capacity_slot: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO runs
            (run_id, lifecycle_status, idempotency_key, request_hash,
             run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, '2026-08-19T00:00:00Z', '2026-08-19T00:00:00Z')
        """,
        (
            run_id,
            status,
            idempotency_key,
            request_hash,
            f"manifest:{run_id}",
            "b" * 64,
            capacity_slot,
        ),
    )


def test_runtime_schema_creates_v11_tables(tmp_path: Path) -> None:
    conn = _rw_conn(tmp_path / "runtime.db")
    tables = _tables(conn)
    conn.close()

    assert {"runs", "run_events", "run_artifacts"} <= tables


def test_runs_has_lifecycle_and_admission_columns(tmp_path: Path) -> None:
    conn = _rw_conn(tmp_path / "runtime.db")
    columns = {
        row[1]: row for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    conn.close()

    for name in (
        "lifecycle_status",
        "idempotency_key",
        "request_hash",
        "run_manifest_id",
        "manifest_hash",
        "capacity_slot",
    ):
        assert name in columns, f"runs missing column {name}"


def test_runs_lifecycle_check_rejects_unknown_status(tmp_path: Path) -> None:
    conn = _rw_conn(tmp_path / "runtime.db")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_run(conn, status="QUEUED")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_run(conn, run_id="run:x", status="SUCCEEDED")
    conn.close()


def test_run_events_unique_run_seq(tmp_path: Path) -> None:
    conn = _rw_conn(tmp_path / "runtime.db")
    _insert_run(conn)
    conn.execute(
        """
        INSERT INTO run_events (run_id, seq, occurred_at, event_type, payload_json, schema_version)
        VALUES ('run:1', 1, '2026-08-19T00:00:00Z', 'run.accepted', '{}', 'v1')
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO run_events (run_id, seq, occurred_at, event_type, payload_json, schema_version)
            VALUES ('run:1', 1, '2026-08-19T00:00:01Z', 'stage.started', '{}', 'v1')
            """
        )
    conn.commit()
    conn.close()


def test_run_artifacts_columns_and_optional_flag(tmp_path: Path) -> None:
    conn = _rw_conn(tmp_path / "runtime.db")
    _insert_run(conn)
    conn.execute(
        """
        INSERT INTO run_events (run_id, seq, occurred_at, event_type, payload_json, schema_version)
        VALUES ('run:1', 1, '2026-08-19T00:00:00Z', 'run.accepted', '{}', 'v1')
        """
    )
    conn.execute(
        """
        INSERT INTO run_artifacts
            (artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional)
        VALUES ('artifact:1', 'run:1', 1, 'evidence_state', ?, '{}', 1)
        """,
        ("c" * 64,),
    )
    row = conn.execute(
        "SELECT artifact_id, run_id, event_seq, artifact_type, payload_hash, payload_json, optional "
        "FROM run_artifacts WHERE artifact_id = 'artifact:1'"
    ).fetchone()
    conn.commit()
    conn.close()

    assert row is not None
    assert row[0] == "artifact:1"
    assert row[1] == "run:1"
    assert row[2] == 1
    assert row[3] == "evidence_state"
    assert row[4] == "c" * 64
    assert row[6] == 1


def test_unique_index_on_non_null_idempotency_key(tmp_path: Path) -> None:
    conn = _rw_conn(tmp_path / "runtime.db")
    _insert_run(conn, run_id="run:1", idempotency_key="key-1", request_hash="h1" + "0" * 62)
    conn.commit()
    # A second run with the same idempotency key must be rejected by SQLite.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_run(conn, run_id="run:2", idempotency_key="key-1", request_hash="h2" + "0" * 62)
    # NULL keys are allowed to repeat.
    _insert_run(conn, run_id="run:3", idempotency_key=None, request_hash="h3" + "0" * 62)
    conn.commit()
    conn.close()


def test_partial_unique_index_on_active_request_hash(tmp_path: Path) -> None:
    conn = _rw_conn(tmp_path / "runtime.db")
    _insert_run(conn, run_id="run:1", status="ACCEPTED", request_hash="h" * 64)
    conn.commit()
    # Duplicate active request hash must be rejected by SQLite.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_run(conn, run_id="run:2", status="RUNNING", request_hash="h" * 64)
    # Terminal rows may reuse the same request hash.
    _insert_run(conn, run_id="run:3", status="COMPLETED", request_hash="h" * 64)
    conn.commit()
    conn.close()


def test_agents_trace_schema_stays_legacy_readable_only(tmp_path: Path) -> None:
    """Ownership: only the app schema creates V1.1 tables/lifecycle columns."""
    conn = sqlite3.connect(tmp_path / "legacy.db")
    init_trace_db(conn)
    tables = _tables(conn)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
    }
    conn.close()

    assert "agent_runs" in tables
    assert "run_events" not in tables
    assert "run_artifacts" not in tables
    assert "runs" not in tables
    assert "lifecycle_status" not in columns

    # Source-level: the agents schema must not declare V1.1 tables and the
    # app schema must be the sole V1.1 new-write owner.
    import catalyst_agents.trace.schema as agents_schema
    import catalyst_app.persistence.schema as app_schema

    agents_source = Path(agents_schema.__file__).read_text(encoding="utf-8")
    app_source = Path(app_schema.__file__).read_text(encoding="utf-8")
    for table in ("run_events", "run_artifacts"):
        assert table not in agents_source, (
            f"agents trace schema must not own {table}"
        )
        assert table in app_source, f"app schema must own {table}"
    assert "lifecycle_status" not in agents_source


def test_migration_is_additive_and_legacy_rows_remain_readable(tmp_path: Path) -> None:
    """App schema init on a legacy DB must not disturb historical rows."""
    db_path = tmp_path / "mixed.db"
    conn = sqlite3.connect(db_path)
    init_trace_db(conn)
    conn.execute(
        """
        INSERT INTO agent_runs
            (run_id, trace_id, ticker, trade_date, status, queued_at, config)
        VALUES ('legacy:1', 'trace:1', 'AAPL', '2026-01-06', 'SUCCEEDED',
                '2026-01-06T00:00:00Z', '{}')
        """
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_path)
    init_runtime_db(conn)
    row = conn.execute(
        "SELECT status FROM agent_runs WHERE run_id = 'legacy:1'"
    ).fetchone()
    tables = _tables(conn)
    conn.close()

    assert row is not None and row[0] == "SUCCEEDED"
    assert "runs" in tables
    assert "run_events" in tables
    assert "run_artifacts" in tables


def test_rw_schema_init_enables_wal_and_connection_settings(tmp_path: Path) -> None:
    db_path = tmp_path / "wal.db"
    conn = _rw_conn(db_path)
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()

    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 5000
