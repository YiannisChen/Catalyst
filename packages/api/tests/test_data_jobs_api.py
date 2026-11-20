from __future__ import annotations

from catalyst_app.routers import data_jobs
from catalyst_data.ingestion.ops import daily
from catalyst_data.ingestion.storage import store
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.sqlite3"
    bodies = tmp_path / "bodies.sqlite3"
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATALYST_UPDATE_JOBS_DB", str(jobs))
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))
    app = FastAPI()
    app.include_router(data_jobs.router)
    return TestClient(app), jobs, bodies


def test_latest_is_no_job_without_initializing_missing_db(tmp_path, monkeypatch):
    client, jobs, _ = _client(tmp_path, monkeypatch)
    with client:
        response = client.get(
            "/api/data/jobs/latest", headers={"Authorization": "Bearer test-admin-token"}
        )
    assert response.json() == {"status": "no_job"}
    assert not jobs.exists()


def test_latest_reads_stage_failures_deferred_and_contiguous_verify_read_only(tmp_path, monkeypatch):
    client, jobs, bodies = _client(tmp_path, monkeypatch)
    conn = daily.open_jobs(jobs)
    conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)", (
        "j1", "k", '{"start":"2026-03-02","end":"2026-03-04"}', "PARTIAL",
        "2026-03-05T00:00:00Z", None, None, None, None))
    conn.executemany("INSERT INTO job_items (job_id,seq,stage,item_key,session_date,status,attempts,last_error) "
                     "VALUES (?,?,?,?,?,?,?,?)", [
        ("j1", 1, "verify", "2026-03-02", "2026-03-02", "OK", 1, None),
        ("j1", 2, "verify", "2026-03-03", "2026-03-03", "FAILED", 3, "broken"),
        ("j1", 3, "news", "2026-03-03:AAA", "2026-03-03", "FAILED", 3, "timeout"),
    ])
    conn.commit()
    conn.close()
    body_conn = store.open_writer(bodies)
    body_conn.executescript(store.DEFERRED_SCHEMA)
    body_conn.execute("INSERT INTO deferred_fetches (url,ticker,session_date,title,published,host,status,deferred_at) "
                      "VALUES ('u','AAA','2026-03-03','t','2026-03-03','news.example','PENDING','now')")
    body_conn.close()
    before = jobs.read_bytes()
    with client:
        response = client.get(
            "/api/data/jobs/latest", headers={"Authorization": "Bearer test-admin-token"}
        )
    payload = response.json()
    assert payload["job_id"] == "j1"
    assert payload["stages"]["verify"] == {"FAILED": 1, "OK": 1}
    assert payload["failures"][0]["error"] == "broken" or payload["failures"][0]["error"] == "timeout"
    assert payload["deferred_by_host"] == {"news.example": {"PENDING": 1}}
    assert payload["last_complete_session"] == "2026-03-02"
    assert jobs.read_bytes() == before


def test_manual_update_requires_bearer_token_and_rejects_live_lease(tmp_path, monkeypatch):
    client, jobs, _ = _client(tmp_path, monkeypatch)
    monkeypatch.delenv("CATALYST_ADMIN_TOKEN", raising=False)
    with client:
        missing = client.post("/api/data/update", headers={"X-Admin-Token": "x"})
    assert missing.status_code == 401
    assert missing.json() == {"detail": "admin_unauthenticated"}
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "secret")
    conn = daily.open_jobs(jobs)
    conn.execute("INSERT INTO jobs (job_id,plan_key,plan_json,status,created_at,lease_owner,lease_expires_at) "
                 "VALUES ('busy','k','{}','RUNNING','now','host:1','2999-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    with client:
        busy = client.post(
            "/api/data/update", headers={"Authorization": "Bearer secret"}
        )
    assert busy.status_code == 409


def test_manual_update_spawns_detached_scheduled_command(tmp_path, monkeypatch):
    client, _, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "secret")
    calls = []
    monkeypatch.setattr(data_jobs.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    with client:
        response = client.post(
            "/api/data/update", headers={"Authorization": "Bearer secret"}
        )
    assert response.status_code == 202 and response.json() == {"started": True}
    args, kwargs = calls[0]
    assert args[0][-3:] == ["-m", "catalyst_data.ingestion.ops.daily", "scheduled"]
    assert kwargs["start_new_session"] is True
    assert "packages/data" in kwargs["env"]["PYTHONPATH"]


def test_manual_update_rejects_when_corpus_rebuild_holds_shared_lock(tmp_path, monkeypatch):
    from catalyst_data.corpus.locking import acquire_corpus_lock, release_corpus_lock

    client, _, _ = _client(tmp_path, monkeypatch)
    corpus = tmp_path / "served.sqlite3"
    corpus.touch()
    monkeypatch.setenv("CATALYST_CORPUS_DB", str(corpus))
    monkeypatch.setenv("CATALYST_CORPUS_FTS_REBUILD", "1")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "secret")
    lock_fd = acquire_corpus_lock(corpus)
    assert lock_fd is not None
    try:
        with client:
            response = client.post("/api/data/update", headers={"Authorization": "Bearer secret"})
    finally:
        release_corpus_lock(lock_fd)

    assert response.status_code == 409
    assert response.json()["detail"] == "CORPUS_REBUILD_BUSY"


def test_history_and_schedule_are_public(tmp_path, monkeypatch):
    import sqlite3

    from catalyst_app.routers import data_jobs
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    jobs = tmp_path / "jobs.sqlite3"
    conn = sqlite3.connect(jobs)
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, plan_key TEXT, plan_json TEXT, status TEXT, "
                 "created_at TEXT, started_at TEXT, finished_at TEXT, lease_owner TEXT, lease_expires_at TEXT)")
    conn.execute("CREATE TABLE job_items (job_id TEXT, seq INTEGER, stage TEXT, item_key TEXT, session_date TEXT, "
                 "status TEXT, attempts INTEGER, last_error TEXT, started_at TEXT, finished_at TEXT, result_json TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('j1','k','{}','PARTIAL','2026-11-17T11:00:00Z','2026-11-17T11:00:00Z',"
                 "'2026-11-17T11:30:00Z',NULL,NULL)")
    conn.execute("INSERT INTO job_items VALUES ('j1',1,'deferred','all',NULL,'FAILED',3,'x',NULL,NULL,NULL)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("CATALYST_UPDATE_JOBS_DB", str(jobs))
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_DAILY_UPDATE_UTC", "11:00")
    app = FastAPI()
    app.include_router(data_jobs.router)
    with TestClient(app) as client:
        history = client.get("/api/data/jobs/history").json()
        schedule = client.get("/api/data/schedule").json()
    assert history["items"] == [{"job_id": "j1", "status": "PARTIAL", "created_at": "2026-11-17T11:00:00Z",
                                 "started_at": "2026-11-17T11:00:00Z", "finished_at": "2026-11-17T11:30:00Z",
                                 "duration_seconds": 1800, "failed_items": 1}]
    assert schedule["daily_update_utc"] == "11:00" and schedule["next_run_at"].endswith("T11:00:00Z")


def test_a_dead_updater_is_not_reported_as_running(tmp_path, monkeypatch):
    import socket
    import sqlite3

    from catalyst_app.routers import data_jobs

    jobs = tmp_path / "jobs.sqlite3"
    conn = sqlite3.connect(jobs)
    conn.execute("CREATE TABLE jobs (job_id TEXT, lease_owner TEXT, lease_expires_at TEXT)")
    # The updater writes host:pid:token; pid 999999 is not alive.
    conn.execute("INSERT INTO jobs VALUES ('j', ?, '2999-01-01T00:00:00Z')", (f"{socket.gethostname()}:999999:abc",))
    conn.commit()
    conn.close()
    assert data_jobs._has_live_lease(jobs) is False
    conn = sqlite3.connect(jobs)
    conn.execute("UPDATE jobs SET lease_owner=?", (f"{socket.gethostname()}:{__import__('os').getpid()}:abc",))
    conn.commit()
    conn.close()
    assert data_jobs._has_live_lease(jobs) is True


def test_public_failure_text_drops_queries_keys_and_paths():
    from catalyst_app.routers.data_jobs import _public_error

    text = ("GET https://api.polygon.io/v2/aggs?apiKey=SECRET123 failed; token=abc; "
            "file /opt/catalyst/repo/data/m9/x.sqlite3 missing")
    cleaned = _public_error(text)
    assert "SECRET123" not in cleaned and "abc" not in cleaned and "/opt/catalyst" not in cleaned
    assert "https://api.polygon.io/v2/aggs" in cleaned


def test_schedule_accepts_seconds_and_skips_new_york_weekends(monkeypatch):
    from datetime import datetime as real_datetime
    from datetime import timezone as tz

    from catalyst_app.routers import data_jobs

    class Friday(real_datetime):
        @classmethod
        def now(cls, tzinfo=None):
            return real_datetime(2026, 11, 20, 12, tzinfo=tz.utc)  # Friday after the run

    monkeypatch.setattr(data_jobs, "datetime", Friday)
    monkeypatch.setenv("CATALYST_DAILY_UPDATE_UTC", "11:00:00")
    result = data_jobs.schedule()
    assert result == {"daily_update_utc": "11:00", "next_run_at": "2026-11-23T11:00:00Z"}  # Monday


def test_latest_job_reports_progress_items_and_an_estimate(tmp_path, monkeypatch):
    import json
    import sqlite3

    from catalyst_app.routers import data_jobs

    jobs = tmp_path / "jobs.sqlite3"
    conn = sqlite3.connect(jobs)
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, plan_key TEXT, plan_json TEXT, status TEXT, "
                 "created_at TEXT, started_at TEXT, finished_at TEXT, lease_owner TEXT, lease_expires_at TEXT)")
    conn.execute("CREATE TABLE job_items (job_id TEXT, seq INTEGER, stage TEXT, item_key TEXT, session_date TEXT, "
                 "status TEXT, attempts INTEGER, last_error TEXT, started_at TEXT, finished_at TEXT, result_json TEXT)")
    plan = {"first_session": "2026-11-13", "last_session": "2026-11-17", "items_to_do": {"prices": 2, "news": 3},
            "eta_seconds": [100, 300], "eta_assumptions": {"seconds_per_item": {"news": [60.0, 180.0]}}}
    conn.execute("INSERT INTO jobs VALUES ('j','k',?, 'RUNNING','2026-11-18T09:00:00Z','2026-11-18T09:00:00Z',NULL,NULL,NULL)",
                 (json.dumps(plan),))
    rows = [(1, "prices", "AAPL", "OK", "2026-11-18T09:00:00Z", "2026-11-18T09:00:02Z"),
            (2, "prices", "MSFT", "PENDING", None, None)]
    for seq, stage, key, status, start, end in rows:
        conn.execute("INSERT INTO job_items VALUES ('j',?,?,?,NULL,?,0,NULL,?,?,NULL)", (seq, stage, key, status, start, end))
    conn.commit()
    conn.close()
    monkeypatch.setenv("CATALYST_UPDATE_JOBS_DB", str(jobs))
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(tmp_path / "missing.sqlite3"))
    job = data_jobs._latest_job()
    data_jobs.LatestJobResponse.model_validate(job)
    assert (job["items_done"], job["items_total"], job["active_seconds"]) == (1, 2, 2)
    assert job["recent_items"][0]["item"] == "AAPL" and job["recent_items"][0]["seconds"] == 2.0
    assert job["plan_summary"]["items_to_do"] == {"prices": 2, "news": 3}
    # 1 price left at its observed 2 s + 3 news not yet created at the plan's 120 s each
    assert job["remaining_seconds"] == 362
    assert job["running"] is False  # no lease: the updater is gone
