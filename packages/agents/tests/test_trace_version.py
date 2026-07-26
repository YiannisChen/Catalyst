from __future__ import annotations

import pytest

from attribution_fixtures import create_temp_trace_db, initialize_trace_schema, open_trace_reader


def test_trace_schema_has_version():
    from catalyst_agents.trace.version import TRACE_SCHEMA_VERSION, get_trace_schema_version

    db = create_temp_trace_db()
    initialize_trace_schema(db)
    assert get_trace_schema_version(db) == TRACE_SCHEMA_VERSION
    assert db.execute("SELECT COUNT(*) FROM trace_schema_version").fetchone()[0] == 1


def test_trace_version_not_data_core_user_version():
    from catalyst_agents.trace.version import TRACE_SCHEMA_VERSION, get_trace_schema_version

    db = create_temp_trace_db()
    initialize_trace_schema(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert get_trace_schema_version(db) == TRACE_SCHEMA_VERSION


def test_unknown_future_trace_version_fails_closed():
    from catalyst_agents.trace.version import UnsupportedTraceSchemaVersion

    db = create_temp_trace_db(schema_version="999.0.0")
    with pytest.raises(UnsupportedTraceSchemaVersion):
        open_trace_reader(db)


def test_trace_schema_has_run_assurance_table():
    db = create_temp_trace_db()
    initialize_trace_schema(db)
    columns = {row[1] for row in db.execute("PRAGMA table_info(run_assurance)").fetchall()}
    assert columns == {"run_id", "schema_version", "record_json", "created_at"}


def test_v1_trace_schema_migrates_without_losing_run_rows():
    import sqlite3
    from catalyst_agents.trace.schema import init_trace_db
    from catalyst_agents.trace.version import get_trace_schema_version

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE trace_schema_version (singleton_id INTEGER PRIMARY KEY, schema_version TEXT NOT NULL, applied_at TEXT NOT NULL)")
    db.execute("INSERT INTO trace_schema_version VALUES (1, '1.0.0', '2026-01-01T00:00:00Z')")
    db.execute("""CREATE TABLE agent_runs (
        run_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, ticker TEXT, trade_date TEXT,
        status TEXT, started_at TEXT NOT NULL, ended_at TEXT, total_latency_ms INTEGER,
        total_cost_usd REAL DEFAULT 0.0, model_id_per_role TEXT, config TEXT,
        error_type TEXT, error_message TEXT
    )""")
    db.execute("INSERT INTO agent_runs (run_id, trace_id, status, started_at) VALUES ('legacy', 'trace', 'SUFFICIENT', '2026-01-01T00:00:00Z')")
    db.commit()

    init_trace_db(db)

    assert get_trace_schema_version(db) == "2.0.0"
    assert db.execute("SELECT run_id, status FROM agent_runs").fetchall() == [("legacy", "SUFFICIENT")]
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0


def test_trace_schema_enforces_run_assurance_foreign_key():
    import sqlite3
    from catalyst_agents.trace.schema import init_trace_db

    db = sqlite3.connect(":memory:")
    init_trace_db(db)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO run_assurance VALUES ('missing', '1.0.0', '{}', '2026-01-15T00:00:00Z')"
        )
