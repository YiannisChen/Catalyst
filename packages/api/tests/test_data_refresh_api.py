from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from catalyst_app.routers import data_refresh
from catalyst_data.ingestion.collectors import market
from catalyst_data.ingestion.storage import store
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    bodies = tmp_path / "bodies.sqlite3"
    market_db = tmp_path / "market.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    peer_map = tmp_path / "peer-map.json"

    conn = store.open_writer(bodies)
    conn.close()
    conn = market.open_market_writer(market_db)
    conn.close()
    conn = market.open_market_writer(snapshot)
    conn.close()
    peer_map.write_text(json.dumps({"benchmark": "SPY", "targets": {}}))

    monkeypatch.setenv("CATALYST_MARKET_CONTEXT_DB", str(market_db))
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))
    monkeypatch.setenv("CATALYST_PEER_MAP", str(peer_map))
    monkeypatch.setenv("CATALYST_SNAPSHOT_DB", str(snapshot))
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")
    app = FastAPI()
    app.include_router(data_refresh.router)
    return TestClient(app, headers={"Authorization": "Bearer test-admin-token"})


def test_status_returns_missing_items_and_invalid_date_is_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        status_response = client.get("/api/data/status?ticker=AAPL&date=2026-01-06")
        bad_date = client.get("/api/data/status?ticker=AAPL&date=2026-1-6")

    assert status_response.status_code == 200
    assert "missing" in status_response.json()
    assert bad_date.status_code == 422


def test_refresh_disabled_without_environment_flag(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.delenv("CATALYST_DATA_REFRESH", raising=False)
    with client:
        response = client.post(
            "/api/data/refresh", json={"ticker": "AAPL", "date": "2026-01-06"}
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "DATA_REFRESH_DISABLED"}


def test_refresh_persists_done_job_and_report(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("CATALYST_DATA_REFRESH", "1")
    monkeypatch.setattr(
        data_refresh,
        "_run_refresh",
        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: {"before": ["bars:target"]}),
    )
    monkeypatch.setattr(data_refresh, "_start", lambda fn: fn())

    with client:
        created = client.post(
            "/api/data/refresh", json={"ticker": "AAPL", "date": "2026-01-06"}
        )
        job = client.get(f"/api/data/refresh/{created.json()['job_id']}")

    assert created.status_code == 202
    assert job.status_code == 200
    assert job.json()["state"] == "done"
    assert job.json()["report"] == {"before": ["bars:target"]}


def test_refresh_rejects_while_persisted_job_is_running(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("CATALYST_DATA_REFRESH", "1")
    with client:
        db_path = tmp_path / "bodies.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE refresh_jobs (job_id TEXT PRIMARY KEY, ticker TEXT, "
                "session_date TEXT, state TEXT, created_at TEXT, finished_at TEXT, "
                "report_json TEXT, error TEXT)"
            )
            conn.execute(
                "INSERT INTO refresh_jobs "
                "(job_id, ticker, session_date, state, created_at) "
                "VALUES ('busy', 'MSFT', '2026-01-06', 'running', 'now')"
            )
        response = client.post(
            "/api/data/refresh", json={"ticker": "AAPL", "date": "2026-01-06"}
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "REFRESH_BUSY"}


def test_refresh_failure_is_redacted(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("CATALYST_DATA_REFRESH", "1")
    secret_marker = "provider-secret-value-for-redaction-test"

    def fail(*args, **kwargs):
        raise RuntimeError(f"provider token={secret_marker} rejected")

    monkeypatch.setattr(data_refresh, "_run_refresh", fail)
    monkeypatch.setattr(data_refresh, "_start", lambda fn: fn())
    with client:
        created = client.post(
            "/api/data/refresh", json={"ticker": "AAPL", "date": "2026-01-06"}
        )
        job = client.get(f"/api/data/refresh/{created.json()['job_id']}")

    assert created.status_code == 202
    assert job.json()["state"] == "failed"
    assert "[REDACTED_SECRET]" in json.dumps(job.json())
    assert secret_marker not in json.dumps(job.json())


def test_mark_interrupted_jobs_updates_running_rows(tmp_path):
    body_store = tmp_path / "bodies.sqlite3"
    conn = store.open_writer(body_store)
    conn.close()
    with sqlite3.connect(body_store) as conn:
        conn.execute(
            "CREATE TABLE refresh_jobs (job_id TEXT PRIMARY KEY, ticker TEXT, "
            "session_date TEXT, state TEXT, created_at TEXT, finished_at TEXT, "
            "report_json TEXT, error TEXT)"
        )
        conn.execute(
            "INSERT INTO refresh_jobs VALUES ('running', 'AAPL', '2026-01-06', "
            "'running', 'now', NULL, NULL, NULL)"
        )

    data_refresh.mark_interrupted_jobs(body_store)

    with sqlite3.connect(body_store) as conn:
        state = conn.execute(
            "SELECT state FROM refresh_jobs WHERE job_id='running'"
        ).fetchone()[0]
    assert state == "interrupted"


def test_unconfigured_stores_return_503(monkeypatch):
    for name in (
        "CATALYST_MARKET_CONTEXT_DB",
        "CATALYST_NEWS_BODIES_DB",
        "CATALYST_PEER_MAP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")
    app = FastAPI()
    app.include_router(data_refresh.router)
    with TestClient(
        app, headers={"Authorization": "Bearer test-admin-token"}
    ) as client:
        response = client.get("/api/data/status?ticker=AAPL&date=2026-01-06")
    assert response.status_code == 503
    assert response.json() == {"detail": "DATA_STORES_NOT_CONFIGURED"}


def test_main_registers_data_refresh_router():
    source = Path(__file__).parents[1] / "catalyst_app" / "main.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    router_imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "catalyst_app.routers.data_refresh"
        and any(alias.name == "router" and alias.asname == "data_refresh_router" for alias in node.names)
        for node in module.body
    )
    registered = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "include_router"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "data_refresh_router"
        for node in ast.walk(module)
    )
    assert router_imported
    assert registered
