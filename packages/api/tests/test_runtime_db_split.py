from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_readonly, open_rw
from catalyst_app.routers import live_runs
from fastapi import FastAPI
from fastapi.testclient import TestClient
from v1_helpers import fixture_db, make_v1_app


def _served_db(path: Path) -> None:
    with open_rw(path) as conn:
        conn.execute("CREATE TABLE served_marker (value TEXT NOT NULL)")
        conn.execute("INSERT INTO served_marker VALUES ('served')")
        conn.commit()


def _set_split_env(monkeypatch, served: Path, runtime: Path) -> None:
    monkeypatch.setenv("CATALYST_DB_PATH", str(served))
    monkeypatch.setenv("CATALYST_RUNTIME_DB_PATH", str(runtime))


def _run_body() -> dict[str, str]:
    return {
        "ticker": "AAPL",
        "trade_date": "2026-01-06",
        "model_id": "deepseek-v4-flash",
    }


def test_split_store_writes_only_to_runtime_and_served_stays_byte_identical(
    tmp_path: Path, monkeypatch
) -> None:
    served = tmp_path / "served.sqlite3"
    runtime = tmp_path / "runtime.sqlite3"
    _served_db(served)
    fixture_db(runtime)
    _set_split_env(monkeypatch, served, runtime)
    before = served.read_bytes()

    app = make_v1_app(runtime)
    assert Path(app.state.db_path) == runtime
    assert Path(app.state.served_db_path) == served

    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=_run_body())
        health = client.get("/api/health")

    assert created.status_code == 200, created.text
    assert health.status_code == 200
    assert health.json()["runtime_db"] == "ready"
    assert health.json()["served_db"] == "ready"
    assert served.read_bytes() == before

    with open_rw(runtime) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM run_budget_reservations").fetchone()[0] == 1

    with open_rw(served) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone() is None


def test_split_store_served_connection_is_really_read_only(tmp_path: Path) -> None:
    served = tmp_path / "served.sqlite3"
    _served_db(served)

    with open_readonly(served) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE must_not_exist (value TEXT)")


def test_runtime_path_unset_keeps_single_db_behavior(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "live_runtime.db"
    fixture_db(db_path)
    monkeypatch.setenv("CATALYST_DB_PATH", str(db_path))
    monkeypatch.delenv("CATALYST_RUNTIME_DB_PATH", raising=False)

    app = make_v1_app(db_path)
    assert Path(app.state.db_path) == db_path
    assert Path(app.state.served_db_path) == db_path

    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=_run_body())

    assert created.status_code == 200, created.text
    with open_rw(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_app_and_dependency_factories_resolve_split_paths(
    tmp_path: Path, monkeypatch
) -> None:
    served = tmp_path / "served.sqlite3"
    runtime = tmp_path / "runtime.sqlite3"
    _served_db(served)
    fixture_db(runtime)
    _set_split_env(monkeypatch, served, runtime)

    app = create_app()
    assert Path(app.state.db_path) == runtime
    assert Path(app.state.served_db_path) == served

    import catalyst_app.dependencies as dependencies

    dependencies.get_runtime_dependency_loader.cache_clear()
    dependencies.get_live_run_service.cache_clear()
    dependencies.get_workbench_store.cache_clear()
    try:
        assert dependencies.get_runtime_dependency_loader().sqlite_db_path == served
        assert dependencies.get_live_run_service().db_path == runtime
        assert dependencies.get_workbench_store().db_path == served
    finally:
        dependencies.get_runtime_dependency_loader.cache_clear()
        dependencies.get_live_run_service.cache_clear()
        dependencies.get_workbench_store.cache_clear()


def test_health_fails_when_runtime_db_is_not_writable(
    tmp_path: Path, monkeypatch
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can write through file permissions")
    served = tmp_path / "served.sqlite3"
    runtime = tmp_path / "runtime.sqlite3"
    _served_db(served)
    fixture_db(runtime)
    os.chmod(runtime, 0o444)

    app = FastAPI()
    app.include_router(live_runs.router)
    app.state.db_path = runtime
    app.state.served_db_path = served
    app.state.admission_controller = SimpleNamespace(
        executor=SimpleNamespace(is_closed=False)
    )
    app.state.runtime_composition = SimpleNamespace(
        dependency_loader=SimpleNamespace(health=lambda: {"status": "ready"})
    )

    try:
        with TestClient(app) as client:
            response = client.get("/api/health")
    finally:
        os.chmod(runtime, 0o644)

    assert response.status_code == 200
    assert response.json()["served_db"] == "ready"
    assert response.json()["runtime_db"] == "failed"
    assert response.json()["status"] == "failed"
