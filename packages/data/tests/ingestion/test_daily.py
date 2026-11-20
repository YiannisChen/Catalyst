from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from catalyst_data.ingestion.collectors import headline_archive, headlines, sec_filings
from catalyst_data.ingestion.ops import daily

SESSIONS = ["2026-03-02", "2026-03-03"]

# Explicit publication writes these for helpers that still read process env.
_PATHS_ENV = (
    "CATALYST_NEWS_BODIES_DB",
    "CATALYST_MARKET_CONTEXT_DB",
    "CATALYST_PEER_MAP",
    "CATALYST_UPDATE_JOBS_DB",
    "CATALYST_HEADLINE_ARCHIVE_DB",
    "CATALYST_SNAPSHOT_DB",
)


def _release_paths_env() -> None:
    import os

    for name in _PATHS_ENV:
        os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def isolated_sec_submissions_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path / "sec-submissions")


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "universe", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(daily, "price_symbols", lambda p: ["AAA", "BBB", "SPY"])
    return daily.Paths(market_db=tmp_path / "m.sqlite3", bodies_db=tmp_path / "b.sqlite3", peer_map={},
                       snapshot_db=tmp_path / "s.db", jobs_db=tmp_path / "jobs.sqlite3",
                       archive_db=tmp_path / "a.sqlite3")


def _plan():
    return {"start": SESSIONS[0], "end": SESSIONS[-1], "last_session": SESSIONS[-1],
            "plan_key": "k" * 64}


def _fake_stages(monkeypatch, calls, *, fail=()):
    def stage(name):
        def fn(rt, *args):
            # SEC receives the job id for gap-ledger progress, but the simulated
            # stage outcome is keyed by its logical source item (the ticker).
            logical_args = args[:1] if name == "sec" else args
            calls.append((name,) + logical_args)
            if (name,) + tuple(a for a in logical_args if not isinstance(a, list)) in fail:
                raise RuntimeError("boom")
            if name == "movers":
                return "OK", {"movers": [{"ticker": "AAA", "return_pct": 3.1}]}
            return "OK", {}
        return fn
    for name in ("prices", "sec", "movers", "news", "verify"):
        monkeypatch.setattr(daily, f"stage_{name}", stage(name))
    monkeypatch.setattr(daily, "stage_deferred", lambda rt: calls.append(("deferred",)) or ("OK", {}))
    for name in ():
        monkeypatch.setattr(daily, f"stage_{name}", stage(name))


def _rt(paths):
    return daily.Runtime(paths=paths, job_sessions=SESSIONS)


def _empty_sec_body_store(path: Path) -> Path:
    from catalyst_data.storage import body_versions, temporal_evidence

    conn = sqlite3.connect(path)
    body_versions.ensure_schema(conn)
    temporal_evidence.ensure_schema(conn)
    conn.commit()
    conn.close()
    return path


def test_order_expansion_and_completion(paths, monkeypatch):
    calls = []
    _fake_stages(monkeypatch, calls)
    job = daily.start(paths, _plan())
    assert daily.start(paths, _plan()) == job  # idempotent for an unfinished plan
    result = daily.run(paths, job, rt=_rt(paths), log=lambda _: None)
    assert result["status"] == "COMPLETED"
    stages = [c[0] for c in calls]
    assert stages[:5] == ["prices"] * 3 + ["sec"] * 2
    # per session: movers, then its news, then its verify; oldest session first
    assert calls[5:] == [
        ("movers", "2026-03-02"), ("news", "AAA", "2026-03-02"), ("verify", "2026-03-02", ["AAA"]),
        ("movers", "2026-03-03"), ("news", "AAA", "2026-03-03"), ("verify", "2026-03-03", ["AAA"]),
        ("deferred",),
    ]
    assert daily.progress(paths, job)["stages"]["news"] == {"OK": 2}


def test_run_logs_item_and_stage_timings(paths, monkeypatch):
    calls = []
    _fake_stages(monkeypatch, calls, fail={("prices", "BBB")})
    timestamps = iter((10.0, 11.2, 20.0, 22.7))
    monkeypatch.setattr(daily.time, "monotonic", lambda: next(timestamps))
    job = daily.start(paths, _plan())
    logs = []

    result = daily.run(paths, job, max_items=2, rt=_rt(paths), log=logs.append)

    assert logs[:2] == [
        "prices AAA OK [1.2s]",
        "prices BBB FAILED (RuntimeError: boom) [2.7s]",
    ]
    assert result["ready_snapshot"]["status"] == "ok"
    assert logs[-1] == "stage timing: prices(n=2 total=3.9s max=2.7s failed=1)"
    assert result["stage_seconds"] == {"prices": 3.9}


def test_resume_after_crash_and_retry_cap_gives_partial(paths, monkeypatch):
    calls = []
    _fake_stages(monkeypatch, calls, fail={("sec", "BBB")})
    job = daily.start(paths, _plan())
    first = daily.run(paths, job, max_items=4, rt=_rt(paths), log=lambda _: None)
    assert first["status"] == "PENDING" and first["processed"] == 4
    # a crash leaves an item RUNNING and the lease expired: the next run resets it
    conn = sqlite3.connect(paths.jobs_db)
    conn.execute("UPDATE job_items SET status='RUNNING' WHERE job_id=? AND stage='movers' "
                 "AND item_key='2026-03-02'", (job,))
    conn.execute("UPDATE jobs SET lease_owner=NULL")
    conn.commit()
    conn.close()
    result = daily.run(paths, job, rt=_rt(paths), log=lambda _: None)
    assert result["status"] == "PARTIAL" and result["failed"] == 1
    assert calls.count(("sec", "BBB")) == daily.MAX_ATTEMPTS
    # the retries come after the remaining work, not back to back
    assert calls.index(("sec", "BBB")) < calls.index(("movers", "2026-03-03")) < len(calls) - 1
    failures = daily.progress(paths, job)["failures"]
    assert failures[0]["item"] == "BBB" and "boom" in failures[0]["error"]
    # retry_failed resets the item; with the fault gone the job completes
    assert daily.retry_failed(paths, job) == 1
    _fake_stages(monkeypatch, calls)
    assert daily.run(paths, job, rt=_rt(paths), log=lambda _: None)["status"] == "COMPLETED"


def test_one_shot_retry_closes_persistently_failed_job_as_partial(paths):
    job = daily.start(paths, _plan())
    conn = daily.open_jobs(paths.jobs_db)
    conn.execute("UPDATE job_items SET status='OK' WHERE job_id=?", (job,))
    conn.execute("UPDATE job_items SET status='FAILED', attempts=1 WHERE job_id=? AND stage='prices' "
                 "AND item_key=(SELECT item_key FROM job_items WHERE job_id=? AND stage='prices' LIMIT 1)",
                 (job, job))
    conn.commit()
    owner = daily._execution_owner()
    assert daily._acquire_lease(conn, job, owner)
    conn.close()
    daily._finish_one_shot_retry(paths, job, owner)
    state = daily.progress(paths, job)
    assert state["status"] == "PARTIAL"
    assert state["failures"][0]["attempts"] == daily.MAX_ATTEMPTS


def test_lease_blocks_a_second_runner(paths, monkeypatch):
    _fake_stages(monkeypatch, [])
    job = daily.start(paths, _plan())
    conn = daily.open_jobs(paths.jobs_db)
    assert daily._acquire_lease(conn, job, "other:1")
    with pytest.raises(RuntimeError, match="lease"):
        daily.run(paths, job, rt=_rt(paths), log=lambda _: None)


def test_owner_dead_recognizes_legacy_and_tokenized_local_ids(monkeypatch):
    monkeypatch.setattr(daily.socket, "gethostname", lambda: "worker")

    def missing_process(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr(daily.os, "kill", missing_process)
    assert daily._owner_dead("worker:999999")
    assert daily._owner_dead("worker:999999:" + "a" * 32)
    assert not daily._owner_dead("worker:999999:not-a-token")
    assert not daily._owner_dead("remote:999999:" + "a" * 32)


def test_same_process_cannot_reenter_same_job_with_fresh_run(paths, monkeypatch):
    _fake_stages(monkeypatch, [])
    job = daily.start(paths, _plan())
    conn = daily.open_jobs(paths.jobs_db)
    owner = daily._execution_owner()
    assert daily._acquire_lease(conn, job, owner)
    conn.close()

    with pytest.raises(RuntimeError, match="lease"):
        daily.run(paths, job, rt=_rt(paths), log=lambda _: None)

    conn = daily.open_jobs(paths.jobs_db)
    assert conn.execute("SELECT lease_owner FROM jobs WHERE job_id=?", (job,)).fetchone() == (owner,)
    conn.close()


def test_run_releases_owned_lease_when_runtime_setup_fails(paths, monkeypatch):
    job = daily.start(paths, _plan())
    monkeypatch.setattr(daily, "Runtime", lambda *a, **k: (_ for _ in ()).throw(ValueError("runtime setup")))

    with pytest.raises(ValueError, match="runtime setup"):
        daily.run(paths, job, log=lambda _: None)

    conn = daily.open_jobs(paths.jobs_db)
    assert conn.execute("SELECT lease_owner FROM jobs WHERE job_id=?", (job,)).fetchone() == (None,)
    conn.close()


def test_run_releases_owned_lease_when_heartbeat_cannot_start(paths, monkeypatch):
    job = daily.start(paths, _plan())
    monkeypatch.setattr(
        daily.threading.Thread, "start",
        lambda _thread: (_ for _ in ()).throw(RuntimeError("heartbeat start failed")),
    )

    with pytest.raises(RuntimeError, match="heartbeat start failed"):
        daily.run(paths, job, rt=_rt(paths), log=lambda _: None)

    conn = daily.open_jobs(paths.jobs_db)
    assert conn.execute("SELECT lease_owner FROM jobs WHERE job_id=?", (job,)).fetchone() == (None,)
    conn.close()


def test_explicit_held_lease_allows_scheduled_style_reentry(paths, monkeypatch):
    _fake_stages(monkeypatch, [])
    job = daily.start(paths, _plan())
    owner = daily._execution_owner()
    conn = daily.open_jobs(paths.jobs_db)
    assert daily._acquire_lease(conn, job, owner)
    conn.close()

    first = daily.run(paths, job, max_items=0, rt=_rt(paths), log=lambda _: None,
                      keep_lease=True, held_lease_owner=owner)
    second = daily.run(paths, job, max_items=0, rt=_rt(paths), log=lambda _: None,
                       keep_lease=True, held_lease_owner=owner)
    assert first["status"] == second["status"] == "PENDING"
    with pytest.raises(RuntimeError, match="lease"):
        daily.run(paths, job, max_items=0, rt=_rt(paths), log=lambda _: None)

    conn = daily.open_jobs(paths.jobs_db)
    assert conn.execute("SELECT lease_owner FROM jobs WHERE job_id=?", (job,)).fetchone() == (owner,)
    conn.close()


def test_held_lease_rejects_wrong_or_expired_identity(paths):
    job = daily.start(paths, _plan())
    owner = daily._execution_owner()
    conn = daily.open_jobs(paths.jobs_db)
    assert daily._acquire_lease(conn, job, owner)
    conn.close()

    with pytest.raises(RuntimeError, match="lease was lost"):
        daily.run(paths, job, max_items=0, rt=_rt(paths), log=lambda _: None,
                  keep_lease=True, held_lease_owner=daily._execution_owner())

    conn = daily.open_jobs(paths.jobs_db)
    conn.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE job_id=?", (job,))
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="lease was lost"):
        daily.run(paths, job, max_items=0, rt=_rt(paths), log=lambda _: None,
                  keep_lease=True, held_lease_owner=owner)

    conn = daily.open_jobs(paths.jobs_db)
    assert conn.execute("SELECT lease_owner FROM jobs WHERE job_id=?", (job,)).fetchone() == (owner,)
    conn.close()


def test_run_does_not_commit_item_or_release_after_lease_takeover(paths, monkeypatch):
    job = daily.start(paths, _plan())
    takeover = daily._execution_owner()

    def take_over_then_return(*_args):
        conn = daily.open_jobs(paths.jobs_db)
        conn.execute("UPDATE jobs SET lease_owner=?, lease_expires_at='2099-01-01T00:00:00Z' "
                     "WHERE job_id=?", (takeover, job))
        conn.commit()
        conn.close()
        return "OK", {}

    monkeypatch.setattr(daily, "_dispatch", take_over_then_return)
    with pytest.raises(RuntimeError, match="lease was lost"):
        daily.run(paths, job, max_items=1, rt=_rt(paths), log=lambda _: None)

    conn = daily.open_jobs(paths.jobs_db)
    assert conn.execute("SELECT lease_owner FROM jobs WHERE job_id=?", (job,)).fetchone() == (takeover,)
    assert conn.execute("SELECT status FROM job_items WHERE job_id=? ORDER BY seq LIMIT 1",
                        (job,)).fetchone() == ("RUNNING",)
    conn.close()


def test_archived_selector_reads_archive_and_falls_back_live(tmp_path, monkeypatch):
    conn = headline_archive.open_store(tmp_path / "a.sqlite3")
    conn.execute("INSERT INTO archive_windows VALUES ('AAA','2026-03-01','2026-03-07','OK',2,1,'','x')")
    for i, (when, title) in enumerate([("2026-03-03T14:00:00Z", "AAA raises guidance"),
                                       ("2026-03-04T14:00:00Z", "AAA later story")]):
        conn.execute("INSERT INTO finnhub_news VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("AAA", i, when, title, f"https://x/{i}", "Yahoo", None, None, None, "{}", "x"))

    class Feed:
        def items(self, ticker, session_date):
            return [{"url": "https://p/1", "title": "AAA wins deal", "published": "2026-03-03T15:00:00Z",
                     "provider": "polygon"}], "OK"

    monkeypatch.setattr(headlines, "_names", lambda ticker, title: title.startswith(ticker))
    live = []
    monkeypatch.setattr(headlines, "finnhub_headlines_with_status",
                        lambda *a: live.append(a) or ([], "EMPTY"))
    select = daily.archived_selector(conn, Feed(), "2026-03-03")
    picked, result = select("AAA", "2026-03-02T21:00:00Z", "2026-03-03T21:00:00Z", 10)
    assert result == "OK"
    assert [p["title"] for p in picked] == ["AAA wins deal", "AAA raises guidance"]  # in window only
    select("ZZZ", "2026-03-02T21:00:00Z", "2026-03-03T21:00:00Z", 10)
    assert live and live[0][0] == "ZZZ"  # not archived -> live Finnhub (Polygon from the feed)


def test_sec_stage_buckets_filings_into_session_windows(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda ticker, tickers_json=None: "0000000001")
    listed = []

    def list_8k(cik, start, cutoff, *, http_get, user_agent):
        listed.append((start, cutoff))
        return [{"accession": "a1", "accepted_at": "2026-03-03T12:00:00Z", "items": ["2.02"],
                 "primary_document": "x", "description": "results", "company": "AAA Inc"}]

    def rows_for_filing(cik, ticker, filing, *, http_get, user_agent, now=None, window=None, cache_dir=None):
        assert window == (headlines.session_window(SESSIONS[0])[0], headlines.session_window(SESSIONS[-1])[1])
        return [{"asset_id": "web:1", "text": "Revenue rose. " * 80, "url": "https://www.sec.gov/x.htm",
                 "title": "AAA 8-K", "publisher": "SEC filing", "source_class": "issuer_disclosure",
                 "eligible_at": filing["accepted_at"], "fetched_at": "2026-03-04T00:00:00Z",
                 "body_sha256": "h", "tickers": [ticker]}], False

    monkeypatch.setattr(sec_filings, "candidates_for_window", list_8k)
    monkeypatch.setattr(sec, "rows_for_filing", rows_for_filing)
    result, detail = daily.stage_sec(daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-1")
    assert len(listed) == 1  # one submissions read for the whole range
    assert result == "OK" and detail["filings"] == 1 and detail["rows"] == 1
    conn = sqlite3.connect(paths.bodies_db)
    ledger_rows = conn.execute("SELECT session_date, status FROM intake_log WHERE source='sec' "
                               "ORDER BY session_date").fetchall()
    assert ledger_rows == [("2026-03-02", "EMPTY"), ("2026-03-03", "OK")]
    # second run: both sessions are complete in the ledger -> skipped
    assert daily.stage_sec(daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-1")[0] == "SKIPPED"


def test_sec_stage_records_target_gap_rows_with_verified_coverage(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec
    from catalyst_data.ingestion.collectors.headlines import session_window

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda ticker, tickers_json=None: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: [])

    result, _ = daily.stage_sec(
        daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-1"
    )

    assert result == "EMPTY"
    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT session_date,ticker,source_id,category,scope_kind,scope_id,status,reason,"
        "requested_from,requested_until,coverage_from,coverage_until,pagination_complete,"
        "logical_task_id,progress_state,target_count,completed_count "
        "FROM category_gaps ORDER BY session_date"
    ).fetchall()
    conn.close()
    assert rows == [
        (session, "AAA", "daily:sec", "sec_filings", "target", "AAA", "EMPTY", None,
         *session_window(session), *session_window(session), 1, "job-1:sec:AAA", "completed", 2, 2)
        for session in SESSIONS
    ]


def test_sec_stage_records_gap_rows_for_already_completed_intake(paths, monkeypatch):
    from catalyst_data.ingestion import ledger
    from catalyst_data.ingestion.collectors import sec
    from catalyst_data.ingestion.collectors.headlines import session_window
    from catalyst_data.ingestion.ops import refresh
    from catalyst_data.ingestion.storage import store

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda ticker, tickers_json=None: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: pytest.fail("completed intake refetched"))
    with store.open_writer(paths.bodies_db) as conn:
        ledger.ensure_ledger(conn)
        for session, status in zip(SESSIONS, ("EMPTY", "OK")):
            ledger.record_attempt(
                conn, subject="AAA", session_date=session, source="sec", status=status,
                headlines=0, bodies=0, attempted_at=refresh._attempt_time(None),
                policy_version=daily.SEC_INTAKE_POLICY_VERSION,
            )

    result, _ = daily.stage_sec(
        daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-resume"
    )

    assert result == "SKIPPED"
    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT session_date,category,status,requested_from,requested_until,coverage_from,"
        "pagination_complete,logical_task_id,progress_state,target_count,completed_count "
        "FROM category_gaps ORDER BY session_date"
    ).fetchall()
    conn.close()
    assert rows == [
        (session, "sec_filings", status, *session_window(session), session_window(session)[0], 1,
         "job-resume:sec:AAA", "completed", 2, 2)
        for session, status in zip(SESSIONS, ("EMPTY", "OK"))
    ]


def test_completed_sec_intake_requires_latest_attempt_under_sec_paged_policy(paths):
    conn = sqlite3.connect(paths.bodies_db)
    conn.execute("""CREATE TABLE intake_log (
        subject TEXT, session_date TEXT, source TEXT, policy_version TEXT,
        attempted_at TEXT, headlines INTEGER, bodies INTEGER, status TEXT,
        PRIMARY KEY(subject, session_date, source, policy_version, attempted_at))""")
    conn.executemany(
        "INSERT INTO intake_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("AAA", "2026-03-02", "sec", "m9_intake_v1", "2026-03-03T12:00:00Z", 0, 0, "OK"),
            ("AAA", "2026-03-02", "sec", "m9_intake_v1", "2026-03-04T12:00:00Z", 0, 0, "ERROR"),
            ("BBB", "2026-03-02", "sec", "sec_paged_v1", "2026-03-04T12:00:00Z", 0, 0, "EMPTY"),
            ("CCC", "2026-03-02", "sec", "sec_paged_v1", "2026-03-04T12:00:00Z", 0, 0, "OK"),
            ("CCC", "2026-03-02", "sec", "sec_paged_v1", "2026-03-04T13:00:00Z", 0, 0, "ERROR"),
        ],
    )
    conn.commit()
    conn.close()

    assert ("AAA", "2026-03-02", "sec") not in daily.completed_sec_intake(paths.bodies_db)
    completed = daily.completed_sec_intake(paths.bodies_db)
    assert ("BBB", "2026-03-02", "sec") in completed
    assert ("CCC", "2026-03-02", "sec") not in completed


def test_sec_stage_records_done_and_todo_sessions_in_mixed_window(paths, monkeypatch):
    from catalyst_data.ingestion import ledger
    from catalyst_data.ingestion.collectors import sec
    from catalyst_data.ingestion.ops import refresh
    from catalyst_data.ingestion.storage import store

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: [])
    with store.open_writer(paths.bodies_db) as conn:
        ledger.ensure_ledger(conn)
        ledger.record_attempt(
            conn, subject="AAA", session_date=SESSIONS[0], source="sec", status="OK",
            headlines=1, bodies=1,
            attempted_at=refresh._attempt_time(None),
            policy_version=daily.SEC_INTAKE_POLICY_VERSION,
        )

    daily.stage_sec(daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-mixed")

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT session_date,status,logical_task_id,progress_state FROM category_gaps "
        "ORDER BY session_date"
    ).fetchall()
    conn.close()
    assert rows == [
        (SESSIONS[0], "OK", "job-mixed:sec:AAA", "completed"),
        (SESSIONS[1], "EMPTY", "job-mixed:sec:AAA", "completed"),
    ]


def test_sec_complete_empty_writes_neutral_full_text_coverage(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_a, **_k: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_a, **_k: [])
    daily.stage_sec(daily.Runtime(paths=paths, job_sessions=[SESSIONS[0]]), "AAA", job_id="coverage-sec")
    with sqlite3.connect(paths.bodies_db) as conn:
        row = conn.execute(
            "SELECT source_id, product, subject_kind, subject_id, status, "
            "enumeration_complete, expected_target_count, acquired_target_count "
            "FROM source_coverage_facts"
        ).fetchone()
    assert row == ("daily:sec", "full_text", "ticker", "AAA", "EMPTY", 1, 0, 0)


def test_sec_page_failure_writes_incomplete_neutral_coverage(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_a, **_k: "0000000001")
    def fail(*_a, **_k):
        raise OSError("synthetic page failure")
    monkeypatch.setattr(sec_filings, "candidates_for_window", fail)
    with pytest.raises(RuntimeError, match="pagination"):
        daily.stage_sec(daily.Runtime(paths=paths, job_sessions=[SESSIONS[0]]), "AAA", job_id="coverage-fail")
    with sqlite3.connect(paths.bodies_db) as conn:
        row = conn.execute(
            "SELECT status, enumeration_complete, coverage_from, coverage_until "
            "FROM source_coverage_facts"
        ).fetchone()
    assert row == ("ERROR", 0, None, None)


def test_sec_body_coverage_records_deduplicated_acquired_urls(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_a, **_k: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_a, **_k: [{
        "accession": "a1", "accepted_at": "2026-03-02T15:00:00Z", "items": ["2.02"],
        "primary_document": "x", "description": "results", "company": "AAA Inc",
    }])
    monkeypatch.setattr(sec, "rows_for_filing", lambda _cik, ticker, filing, **_kwargs: ([{
        "asset_id": "web:coverage", "text": "Revenue rose. " * 80,
        "url": "https://www.sec.gov/Archives/coverage.htm", "title": "AAA 8-K",
        "publisher": "SEC filing", "source_class": "issuer_disclosure",
        "eligible_at": filing["accepted_at"], "fetched_at": "2026-03-04T00:00:00Z",
        "body_sha256": "a" * 64, "tickers": [ticker], "accession": "a1",
    }], False))
    daily.stage_sec(daily.Runtime(paths=paths, job_sessions=[SESSIONS[0]]), "AAA", job_id="coverage-body")
    with sqlite3.connect(paths.bodies_db) as conn:
        fact = conn.execute("SELECT status, expected_target_count, acquired_target_count, enumeration_complete "
                            "FROM source_coverage_facts").fetchone()
        target = conn.execute("SELECT identity, acquired FROM source_coverage_targets").fetchone()
    # Known acquired URLs are only a lower bound on the selected target set.
    assert fact == ("OK", 1, 1, 0)
    assert target == ("https://www.sec.gov/Archives/coverage.htm", 1)


def test_sec_daily_buckets_by_index_acceptance_not_late_json_time(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec
    from catalyst_data.ingestion.collectors.headlines import session_window

    session = "2026-03-03"
    window = session_window(session)
    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: [{
        "accession": "a-late", "accepted_at": "2026-03-04T01:30:00Z", "items": ["2.02"],
        "primary_document": "x", "description": "results", "company": "AAA Inc",
    }])
    seen = []

    def rows_for_filing(_cik, ticker, filing, *, window=None, **_kwargs):
        seen.append(window)
        filing["json_accepted_at"] = filing["accepted_at"]
        filing["accepted_at"] = "2026-03-03T20:30:00Z"
        return [{
            "asset_id": "web:sec-late", "text": "Revenue rose. " * 80,
            "url": "https://www.sec.gov/a-late.htm", "title": "AAA 8-K",
            "publisher": "SEC filing", "source_class": "issuer_disclosure",
            "eligible_at": filing["accepted_at"], "fetched_at": "2026-03-04T00:00:00Z",
            "body_sha256": "a" * 64, "tickers": [ticker],
        }], False

    monkeypatch.setattr(sec, "rows_for_filing", rows_for_filing)
    daily.stage_sec(daily.Runtime(paths=paths, job_sessions=[session]), "AAA", job_id="job-index-time")

    assert seen == [window]
    conn = sqlite3.connect(paths.bodies_db)
    intake = conn.execute("SELECT session_date,status FROM intake_log WHERE source='sec'").fetchall()
    stored = conn.execute("SELECT eligible_at FROM web_body_chunks WHERE asset_id='web:sec-late'").fetchall()
    conn.close()
    assert intake == [(session, "OK")]
    assert stored == [("2026-03-03T20:30:00Z",)]


def test_sec_intake_and_gap_row_roll_back_together(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec
    from catalyst_data.ingestion.storage import store

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: [{
        "accession": "a-atomic", "accepted_at": "2026-03-02T15:00:00Z", "items": ["2.02"],
        "primary_document": "x", "description": "results", "company": "AAA Inc",
    }])
    monkeypatch.setattr(sec, "rows_for_filing", lambda _cik, ticker, filing, **_kwargs: ([{
        "asset_id": "web:atomic", "text": "Revenue rose. " * 80,
        "url": "https://www.sec.gov/Archives/a-atomic.htm", "title": "AAA 8-K",
        "publisher": "SEC filing", "source_class": "issuer_disclosure",
        "eligible_at": filing["accepted_at"], "fetched_at": "2026-03-04T00:00:00Z",
        "body_sha256": "b" * 64, "tickers": [ticker], "accession": "a-atomic",
        "structure": {"title": "AAA reports results", "units": []},
    }], False))
    monkeypatch.setattr(
        daily.GapLedger, "record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("gap write failed")),
    )

    with pytest.raises(RuntimeError, match="gap write failed"):
        daily.stage_sec(daily.Runtime(paths=paths, job_sessions=[SESSIONS[0]]), "AAA", job_id="job-atomic")

    conn = sqlite3.connect(paths.bodies_db)
    assert conn.execute("SELECT count(*) FROM intake_log WHERE source='sec'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM web_body_chunks WHERE asset_id='web:atomic'").fetchone()[0] == 0
    units_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sec_exhibit_units'"
    ).fetchone()
    assert units_table is None or conn.execute(
        "SELECT count(*) FROM sec_exhibit_units WHERE url='https://www.sec.gov/Archives/a-atomic.htm'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM category_gaps WHERE ticker='AAA'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM source_coverage_facts").fetchone()[0] == 0
    conn.close()


def test_sec_rebucketed_exhibit_failure_reopens_and_retries_completed_session(paths, monkeypatch):
    from catalyst_data.ingestion import ledger
    from catalyst_data.ingestion.collectors import sec
    from catalyst_data.ingestion.ops import refresh
    from catalyst_data.ingestion.storage import store

    session_dates = ["2026-03-03", "2026-03-04"]
    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: "0000000001")
    filing = {
        "accession": "a-reopen", "accepted_at": "2026-03-04T01:30:00Z", "items": ["2.02"],
        "primary_document": "x", "description": "results", "company": "AAA Inc",
    }
    list_calls = []
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: list_calls.append("called") or [dict(filing)])
    exhibit_attempts = []

    def rows_for_filing(_cik, ticker, candidate, **_kwargs):
        exhibit_attempts.append(candidate["accession"])
        candidate["json_accepted_at"] = candidate["accepted_at"]
        candidate["accepted_at"] = "2026-03-03T20:30:00Z"
        if len(exhibit_attempts) == 1:
            return [], True
        return ([{
            "asset_id": "web:reopen", "text": "Revenue rose. " * 80,
            "url": "https://www.sec.gov/Archives/a-reopen.htm", "title": "AAA 8-K",
            "publisher": "SEC filing", "source_class": "issuer_disclosure",
            "eligible_at": candidate["accepted_at"], "fetched_at": "2026-03-04T00:00:00Z",
            "body_sha256": "c" * 64, "tickers": [ticker],
        }], False)

    monkeypatch.setattr(sec, "rows_for_filing", rows_for_filing)
    with store.open_writer(paths.bodies_db) as conn:
        ledger.ensure_ledger(conn)
        ledger.record_attempt(
            conn, subject="AAA", session_date=session_dates[0], source="sec", status="EMPTY",
            headlines=0, bodies=0, attempted_at=refresh._attempt_time(None),
            policy_version=daily.SEC_INTAKE_POLICY_VERSION,
        )

    runtime = daily.Runtime(paths=paths, job_sessions=session_dates)
    with pytest.raises(RuntimeError, match="exhibit fetch failed"):
        daily.stage_sec(runtime, "AAA", job_id="job-reopen")

    conn = sqlite3.connect(paths.bodies_db)
    first_rows = conn.execute(
        "SELECT session_date,status,progress_state,completed_count,logical_task_id "
        "FROM category_gaps ORDER BY session_date"
    ).fetchall()
    conn.close()
    assert [row[:4] for row in first_rows] == [
        (session_dates[0], "ERROR", "retrying", 0),
        (session_dates[1], "ERROR", "retrying", 0),
    ]
    assert first_rows[0][4].startswith("job-reopen:sec:AAA:reopen:")
    assert first_rows[0][4] == first_rows[1][4]

    result, _ = daily.stage_sec(runtime, "AAA", job_id="job-reopen")
    assert result == "OK"
    assert list_calls == ["called", "called"]
    assert exhibit_attempts == ["a-reopen", "a-reopen"]

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT session_date,status FROM intake_log WHERE source='sec' ORDER BY attempt_id"
    ).fetchall()
    stored = conn.execute(
        "SELECT eligible_at FROM web_body_chunks WHERE asset_id='web:reopen' LIMIT 1"
    ).fetchone()
    conn.close()
    assert rows == [(session_dates[0], "EMPTY"),
                    (session_dates[0], "ERROR"), (session_dates[1], "ERROR"),
                    (session_dates[0], "OK"), (session_dates[1], "EMPTY")]
    assert stored == ("2026-03-03T20:30:00Z",)

    assert daily.stage_sec(runtime, "AAA", job_id="job-reopen")[0] == "SKIPPED"
    assert list_calls == ["called", "called"]


def test_sec_successful_rebucket_reopens_completed_session_and_then_skips(paths, monkeypatch):
    from catalyst_data.ingestion import ledger
    from catalyst_data.ingestion.collectors import sec
    from catalyst_data.ingestion.ops import refresh
    from catalyst_data.ingestion.storage import store

    session_dates = ["2026-03-03", "2026-03-04"]
    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: [{
        "accession": "a-reopen-ok", "accepted_at": "2026-03-04T01:30:00Z", "items": ["2.02"],
        "primary_document": "x", "description": "results", "company": "AAA Inc",
    }])

    def rows_for_filing(_cik, ticker, filing, **_kwargs):
        filing["json_accepted_at"] = filing["accepted_at"]
        filing["accepted_at"] = "2026-03-03T20:30:00Z"
        return ([{
            "asset_id": "web:reopen-ok", "text": "Revenue rose. " * 80,
            "url": "https://www.sec.gov/Archives/a-reopen-ok.htm", "title": "AAA 8-K",
            "publisher": "SEC filing", "source_class": "issuer_disclosure",
            "eligible_at": filing["accepted_at"], "fetched_at": "2026-03-04T00:00:00Z",
            "body_sha256": "d" * 64, "tickers": [ticker],
        }], False)

    monkeypatch.setattr(sec, "rows_for_filing", rows_for_filing)
    with store.open_writer(paths.bodies_db) as conn:
        ledger.ensure_ledger(conn)
        ledger.record_attempt(
            conn, subject="AAA", session_date=session_dates[0], source="sec", status="EMPTY",
            headlines=0, bodies=0, attempted_at=refresh._attempt_time(None),
            policy_version=daily.SEC_INTAKE_POLICY_VERSION,
        )

    runtime = daily.Runtime(paths=paths, job_sessions=session_dates)
    assert daily.stage_sec(runtime, "AAA", job_id="job-reopen-ok")[0] == "OK"

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT session_date,status,progress_state,completed_count FROM category_gaps ORDER BY session_date"
    ).fetchall()
    intake = conn.execute(
        "SELECT session_date,status FROM intake_log WHERE source='sec' ORDER BY attempt_id"
    ).fetchall()
    conn.close()
    assert rows == [(session_dates[0], "OK", "completed", 2),
                    (session_dates[1], "EMPTY", "completed", 2)]
    assert intake == [(session_dates[0], "EMPTY"),
                      (session_dates[0], "OK"), (session_dates[1], "EMPTY")]
    assert daily.stage_sec(runtime, "AAA", job_id="job-reopen-ok")[0] == "SKIPPED"


def test_sec_same_job_retry_keeps_task_progress_monotonic(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: [{
        "accession": "a-retry", "accepted_at": "2026-03-02T15:00:00Z", "items": ["2.02"],
        "primary_document": "x", "description": "results", "company": "AAA Inc",
    }])
    monkeypatch.setattr(sec, "rows_for_filing", lambda *_args, **_kwargs: ([], True))
    runtime = daily.Runtime(paths=paths, job_sessions=SESSIONS)

    with pytest.raises(RuntimeError, match="exhibit fetch failed"):
        daily.stage_sec(runtime, "AAA", job_id="job-retry")

    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sec, "rows_for_filing", lambda *_args, **_kwargs: pytest.fail("no filing expected"))
    daily.stage_sec(runtime, "AAA", job_id="job-retry")
    assert daily.stage_sec(runtime, "AAA", job_id="job-retry")[0] == "SKIPPED"

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT session_date,completed_count FROM category_gaps ORDER BY session_date"
    ).fetchall()
    conn.close()
    assert rows == [(SESSIONS[0], 2), (SESSIONS[1], 2)]


def test_sec_stage_failure_is_retrying_without_counting_session_complete(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda ticker, tickers_json=None: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: [
        {"accession": "a1", "accepted_at": "2026-03-02T15:00:00Z", "items": ["2.02"],
         "primary_document": "x", "description": "results", "company": "AAA Inc"},
    ])
    monkeypatch.setattr(sec, "rows_for_filing", lambda *_args, **_kwargs: ([], True))

    with pytest.raises(RuntimeError, match="exhibit fetch failed"):
        daily.stage_sec(daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-2")

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT session_date,status,reason,coverage_from,coverage_until,pagination_complete,"
        "logical_task_id,progress_state,target_count,completed_count FROM category_gaps ORDER BY session_date"
    ).fetchall()
    conn.close()
    assert rows == [
        ("2026-03-02", "ERROR", "ERROR", None, None, False, "job-2:sec:AAA", "retrying", 2, 0),
        ("2026-03-03", "ERROR", "ERROR", None, None, False, "job-2:sec:AAA", "retrying", 2, 0),
    ]


def test_sec_stage_unmapped_ticker_is_not_recorded_as_empty(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: pytest.fail("unmapped ticker queried SEC"))

    with pytest.raises(RuntimeError, match="SEC CIK mapping unavailable"):
        daily.stage_sec(daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-unmapped")

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT status,reason,progress_state,completed_count FROM category_gaps ORDER BY session_date"
    ).fetchall()
    conn.close()
    assert rows == [("ERROR", "ERROR", "retrying", 0), ("ERROR", "ERROR", "retrying", 0)]


def test_sec_missing_user_agent_is_recorded_as_not_fetched(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: pytest.fail("SEC identity lookup ran"))

    with pytest.raises(RuntimeError, match="SEC_USER_AGENT not set"):
        daily.stage_sec(daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-disabled")

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute("SELECT status,reason,progress_state,completed_count FROM category_gaps").fetchall()
    conn.close()
    assert rows == [("NOT_FETCHED", "DISABLED", "retrying", 0),
                    ("NOT_FETCHED", "DISABLED", "retrying", 0)]


def test_sec_identity_lookup_failure_is_recorded(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("mapping")))

    with pytest.raises(RuntimeError, match="SEC CIK lookup failed"):
        daily.stage_sec(daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-map-error")

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute("SELECT status,reason,progress_state,completed_count FROM category_gaps").fetchall()
    conn.close()
    assert rows == [("ERROR", "ERROR", "retrying", 0), ("ERROR", "ERROR", "retrying", 0)]


def test_sec_submissions_error_is_recorded_before_stage_fails(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))

    with pytest.raises(RuntimeError, match="SEC submissions pagination failed"):
        daily.stage_sec(daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-submissions")

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute("SELECT status,reason,progress_state,completed_count FROM category_gaps").fetchall()
    conn.close()
    assert rows == [("ERROR", "ERROR", "retrying", 0), ("ERROR", "ERROR", "retrying", 0)]


def test_invalid_sec_filing_values_are_recorded_as_error_not_empty(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: "0000000001")
    payload = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000000001-26-000001"],
                "acceptanceDateTime": ["2026-03-02"],
                "form": ["8-K"],
                "items": ["2.02"],
            },
            "files": [],
        }
    }
    monkeypatch.setattr(sec, "_response_json", lambda _response: payload)

    with pytest.raises(RuntimeError, match="SEC submissions pagination failed"):
        daily.stage_sec(
            daily.Runtime(
                paths=paths,
                job_sessions=SESSIONS,
                sec_get=lambda *_args, **_kwargs: object(),
            ),
            "AAA",
            job_id="job-invalid-sec-values",
        )

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT status,reason,progress_state,completed_count FROM category_gaps "
        "ORDER BY session_date"
    ).fetchall()
    conn.close()
    assert rows == [("ERROR", "ERROR", "retrying", 0), ("ERROR", "ERROR", "retrying", 0)]


def test_sec_partial_exhibit_failure_does_not_mark_session_ok(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import sec

    monkeypatch.setenv("SEC_USER_AGENT", "test agent")
    monkeypatch.setattr(sec, "cik_for", lambda *_args, **_kwargs: "0000000001")
    monkeypatch.setattr(sec_filings, "candidates_for_window", lambda *_args, **_kwargs: [
        {"accession": "a1", "accepted_at": "2026-03-02T15:00:00Z", "items": ["2.02"],
         "primary_document": "x", "description": "results", "company": "AAA Inc"},
        {"accession": "a2", "accepted_at": "2026-03-02T16:00:00Z", "items": ["2.02"],
         "primary_document": "y", "description": "results", "company": "AAA Inc"},
    ])
    monkeypatch.setattr(sec, "rows_for_filing", lambda _cik, _ticker, filing, **_kwargs:
                        ([{"asset_id": "web:a1", "text": "Revenue rose. " * 80,
                           "url": "https://www.sec.gov/a1.htm", "title": "AAA 8-K",
                           "publisher": "SEC filing", "source_class": "issuer_disclosure",
                           "eligible_at": filing["accepted_at"], "fetched_at": "2026-03-04T00:00:00Z",
                           "body_sha256": "a" * 64, "tickers": ["AAA"]}], False)
                        if filing["accession"] == "a1" else ([], True))

    with pytest.raises(RuntimeError, match="exhibit fetch failed"):
        daily.stage_sec(daily.Runtime(paths=paths, job_sessions=SESSIONS), "AAA", job_id="job-partial")

    conn = sqlite3.connect(paths.bodies_db)
    rows = conn.execute(
        "SELECT session_date,status,reason,progress_state,completed_count FROM category_gaps ORDER BY session_date"
    ).fetchall()
    conn.close()
    assert rows == [
        ("2026-03-02", "ERROR", "ERROR", "retrying", 0),
        ("2026-03-03", "ERROR", "ERROR", "retrying", 0),
    ]


def test_polygon_feed_filters_by_ticker_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    calls = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def http_get(url, params, timeout):
        calls.append(url)
        return Response({"results": [
            {"id": "1", "published_utc": "2026-03-03T14:00:00Z", "title": "AAA up", "article_url": "u1",
             "tickers": ["AAA"]},
            {"id": "2", "published_utc": "2026-03-03T15:00:00Z", "title": "BBB down", "article_url": "u2",
             "tickers": ["BBB", "CCC"]}]})

    conn = sqlite3.connect(tmp_path / "a.sqlite3")
    feed = daily.PolygonFeed(conn, http_get=http_get, sleep=lambda s: None)
    assert feed.items("AAA", "2026-03-03") == (
        [{"url": "u1", "title": "AAA up", "published": "2026-03-03T14:00:00Z", "provider": "polygon"}], "OK")
    assert feed.items("CCC", "2026-03-03")[0][0]["url"] == "u2"
    assert len(calls) == 1  # one feed request per session, then the cache
    assert feed.items("ZZZ", "2026-03-03") == ([], "EMPTY")


def test_grouped_polygon_fills_missing_yfinance_session_and_caches(tmp_path, monkeypatch):
    import pandas as pd
    from catalyst_data.ingestion.collectors import market

    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    market._clear_polygon_cache()
    calls = []

    class Response:
        status_code = 200
        def json(self):
            return {"results": [{"T": "AAA", "o": 10, "h": 12, "l": 9, "c": 11,
                                 "v": 100, "t": 1772481600000}]}

    def http_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    frame = pd.DataFrame({"Open": [8], "High": [9], "Low": [7], "Close": [8.5], "Volume": [50]},
                         index=pd.to_datetime(["2026-03-03"]))
    db = market.open_market_writer(tmp_path / "market.sqlite3")
    written = market.load_bars(
        db, ["AAA"], "2026-03-02", "2026-03-03", now=__import__("datetime").datetime(2026, 3, 4, tzinfo=__import__("datetime").timezone.utc),
        download=lambda *a, **k: frame, polygon_get=http_get, polygon_sleep=lambda _: None,
        fallback_sessions=["2026-03-02", "2026-03-03"],
    )
    assert written["AAA"] == 2
    rows = db.execute("SELECT date, close, source FROM ohlcv ORDER BY date").fetchall()
    assert len(rows) == 2 and rows[1][1:] == (8.5, "yfinance")
    assert rows[0][2] == "polygon"
    assert "grouped/locale/us/market/stocks/2026-03-02" in calls[0][0]


def test_polygon_yfinance_close_check_reports_median_relative_difference(tmp_path):
    from catalyst_data.ingestion.collectors import market

    db = market.open_market_writer(tmp_path / "market.sqlite3")
    db.executemany("INSERT INTO ohlcv VALUES (?,?,?,?,?,?,?,?)", [
        ("AAA", "2026-03-02", 1, 1, 1, 100, 1, "yfinance"),
        ("AAA", "2026-03-03", 1, 1, 1, 100, 1, "yfinance"),
    ])
    assert market.compare_polygon_closes(db, "AAA", [
        {"date": "2026-03-02", "close": 102}, {"date": "2026-03-03", "close": 98},
    ]) == {"symbol": "AAA", "overlap": 2, "median_absolute_relative_difference": 0.02}


def test_returns_use_consecutive_closes_across_price_sources(tmp_path):
    # yfinance (auto_adjust=False) and Polygon (adjusted=true) agree to within 0.01% (measured
    # 2026-10-09), so a Polygon-filled session after yfinance sessions still yields a return.
    import sqlite3

    from catalyst_data.ingestion import scope

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)")
    conn.execute("INSERT INTO ohlcv VALUES ('AAA','2026-03-02',1,1,1,100,1,'yfinance')")
    conn.execute("INSERT INTO ohlcv VALUES ('AAA','2026-03-03',1,1,1,102,1,'polygon')")
    assert round(scope.session_return(conn, "AAA", "2026-03-03"), 6) == 2.0

def test_scheduled_skips_while_another_job_holds_live_lease(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(daily.headline_archive, "REPO", tmp_path)
    monkeypatch.setenv("CATALYST_RUNTIME_DATA_DIR", "runtime-state")
    conn = daily.open_jobs(paths.jobs_db)
    conn.execute("INSERT INTO jobs (job_id,plan_key,plan_json,status,created_at,lease_owner,lease_expires_at) "
                 "VALUES ('busy','k','{}','RUNNING','now','remote:1','2999-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(daily, "plan", lambda *a, **k: pytest.fail("plan must not start under live lease"))
    result = daily.scheduled(paths)
    assert result == {"started": False, "reason": "live_lease",
                      "message": "another job holds a live lease"}
    assert "live lease" in (tmp_path / "runtime-state/daily_logs/scheduled.log").read_text()


def test_scheduled_is_noop_when_latest_closed_sessions_are_verified(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(daily.headline_archive, "REPO", repo)
    monkeypatch.setattr(daily.market, "last_closed_session", lambda now=None: "2026-03-04")
    paths = daily.Paths(tmp_path / "m.db", tmp_path / "b.db", {}, None,
                        tmp_path / "jobs.db", tmp_path / "a.db")
    daily.open_jobs(paths.jobs_db).close()
    monkeypatch.setattr(daily, "_missing_sessions", lambda *a, **k: [])
    result = daily.scheduled(paths)
    assert result["started"] is False and result["reason"] == "up_to_date"
    assert result["ready_snapshot"]["status"] == "ok"
    assert Path(result["ready_snapshot"]["path"]).name == "ready_range.json"


def test_scheduled_propagates_body_exception(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(headline_archive, "REPO", tmp_path)
    monkeypatch.setattr(daily.market, "last_closed_session", lambda now=None: SESSIONS[-1])
    monkeypatch.setattr(daily, "_missing_sessions", lambda *a, **k: SESSIONS)
    monkeypatch.setattr(daily, "plan", lambda *a, **k: _plan())
    monkeypatch.setattr(daily, "run", lambda *a, **k: (_ for _ in ()).throw(ValueError("body failed")))

    with pytest.raises(ValueError, match="body failed"):
        daily.scheduled(paths)



def test_scheduled_releases_lease_when_relay_probe_raises(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(headline_archive, "REPO", tmp_path)
    monkeypatch.setattr(daily.market, "last_closed_session", lambda now=None: SESSIONS[-1])
    monkeypatch.setattr(daily, "_missing_sessions", lambda *a, **k: SESSIONS)
    monkeypatch.setattr(daily, "plan", lambda *a, **k: _plan())
    monkeypatch.setattr(
        daily.fetch, "relay_status", lambda: (_ for _ in ()).throw(RuntimeError("relay probe failed"))
    )

    with pytest.raises(RuntimeError, match="relay probe failed"):
        daily.scheduled(paths)

    conn = daily.open_jobs(paths.jobs_db)
    try:
        lease_owner, lease_expires_at = conn.execute(
            "SELECT lease_owner,lease_expires_at FROM jobs WHERE job_id LIKE 'upd-%'"
        ).fetchone()
    finally:
        conn.close()
    assert lease_owner is None and lease_expires_at is None


def test_scheduled_releases_lease_when_heartbeat_start_raises(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(headline_archive, "REPO", tmp_path)
    monkeypatch.setattr(daily.market, "last_closed_session", lambda now=None: SESSIONS[-1])
    monkeypatch.setattr(daily, "_missing_sessions", lambda *a, **k: SESSIONS)
    monkeypatch.setattr(daily, "plan", lambda *a, **k: _plan())
    monkeypatch.setattr(
        daily.threading.Thread, "start",
        lambda _thread: (_ for _ in ()).throw(RuntimeError("thread start failed")),
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        daily.scheduled(paths)

    conn = daily.open_jobs(paths.jobs_db)
    try:
        lease_owner, lease_expires_at = conn.execute(
            "SELECT lease_owner,lease_expires_at FROM jobs WHERE job_id LIKE 'upd-%'"
        ).fetchone()
    finally:
        conn.close()
    assert lease_owner is None and lease_expires_at is None


def test_scheduled_releases_lease_when_asset_capture_raises(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(headline_archive, "REPO", tmp_path)
    monkeypatch.setattr(daily.market, "last_closed_session", lambda now=None: SESSIONS[-1])
    monkeypatch.setattr(daily, "_missing_sessions", lambda *a, **k: SESSIONS)
    monkeypatch.setattr(daily, "plan", lambda *a, **k: _plan())
    acquire = daily._acquire_lease

    def acquire_then_deny_asset_read(conn, job_id, owner):
        assert acquire(conn, job_id, owner)
        conn.set_authorizer(
            lambda action, table, *_args: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and table == "job_assets"
            else sqlite3.SQLITE_OK
        )
        return True

    monkeypatch.setattr(daily, "_acquire_lease", acquire_then_deny_asset_read)

    with pytest.raises(sqlite3.DatabaseError):
        daily.scheduled(paths)

    conn = daily.open_jobs(paths.jobs_db)
    try:
        lease_owner, lease_expires_at = conn.execute(
            "SELECT lease_owner,lease_expires_at FROM jobs WHERE job_id LIKE 'upd-%'"
        ).fetchone()
    finally:
        conn.close()
    assert lease_owner is None and lease_expires_at is None


def test_scheduled_success_runs_main_and_web_stages(paths, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(headline_archive, "REPO", tmp_path)
    monkeypatch.setattr(daily.market, "last_closed_session", lambda now=None: SESSIONS[-1])
    monkeypatch.setattr(daily, "_missing_sessions", lambda *a, **k: SESSIONS)
    monkeypatch.setattr(daily, "plan", lambda *a, **k: _plan())
    calls = []
    run_leases = []

    def run_under_held_lease(*args, **kwargs):
        run_leases.append((args[1], kwargs.get("held_lease_owner"), kwargs.get("keep_lease")))
        return calls.append("main") or {"status": "PARTIAL"}

    monkeypatch.setattr(daily, "run", run_under_held_lease)
    monkeypatch.setattr(daily, "progress", lambda *a, **k: {"status": "PARTIAL"})
    monkeypatch.setattr(daily, "retry_failed", lambda *a, **k: 1)
    monkeypatch.setattr(daily, "_finish_one_shot_retry", lambda *a, **k: None)
    monkeypatch.setattr(daily, "_scheduled_web_discovery", lambda *a, **k: calls.append("web") or [])

    result = daily.scheduled(paths)

    assert result["started"] is True
    assert result["status"] == "PARTIAL"
    assert result["ready_snapshot"]["status"] == "ok"
    assert calls == ["main", "main", "web"]
    assert len(run_leases) == 2
    assert {entry[0] for entry in run_leases} == {result["job_id"]}
    assert run_leases[0][1] and run_leases[0][1] == run_leases[1][1]
    assert all(entry[2] is True for entry in run_leases)


def test_daily_postprocess_failures_are_in_summary_and_persisted(paths, tmp_path, monkeypatch):
    from catalyst_data.ingestion.ops import daily

    paths = daily.Paths(**(paths.__dict__ | {"pipeline_status_path": tmp_path / "pipeline.json"}))
    monkeypatch.setattr(daily, "_build_ready_snapshot_best_effort", lambda *_: {
        "status": "failed", "reason": "snapshot error"})
    monkeypatch.setattr(daily, "_build_corpus_fts_best_effort", lambda *_: {
        "status": "failed", "reason": "FTS error"})
    summary = {"job_id": "job-1", "status": "COMPLETED"}

    result = daily._finalize_daily_summary(paths, summary, lambda _: None)

    assert result["status"] == "COMPLETED"
    assert result["ready_snapshot"]["status"] == "failed"
    assert result["corpus_fts"]["status"] == "failed"
    persisted = json.loads(paths.pipeline_status_path.read_text())
    assert persisted["ready_snapshot"]["status"] == "failed"
    assert persisted["corpus_fts"]["status"] == "failed"


@pytest.mark.parametrize("reason_code", ["locked", "not_current", "already_published"])
def test_skipped_corpus_fts_attempt_preserves_previous_failure(paths, tmp_path, monkeypatch, reason_code):
    from catalyst_data.ingestion.ops import daily

    status_path = tmp_path / "pipeline.json"
    previous = {"status": "failed", "reason_code": "low_disk", "reason": "not enough space"}
    status_path.write_text(json.dumps({"corpus_fts": previous}))
    paths = daily.Paths(**(paths.__dict__ | {"pipeline_status_path": status_path}))
    monkeypatch.setattr(daily, "_build_ready_snapshot_best_effort", lambda *_: {"status": "ok"})
    attempt = {"status": "skipped", "reason_code": reason_code}
    monkeypatch.setattr(daily, "_build_corpus_fts_best_effort", lambda *_: attempt)

    result = daily._finalize_daily_summary(paths, {"job_id": "done", "status": "COMPLETED"}, lambda _: None)

    assert result["corpus_fts"] == previous
    assert result["corpus_fts_attempt"] == attempt
    persisted = json.loads(status_path.read_text())
    assert persisted["corpus_fts"] == previous
    assert persisted["corpus_fts_attempt"] == attempt


def test_incomplete_daily_summary_preserves_previous_corpus_fts_failure(paths, tmp_path, monkeypatch):
    from catalyst_data.ingestion.ops import daily

    status_path = tmp_path / "pipeline.json"
    prior = {"status": "failed", "reason_code": "low_disk", "reason": "disk floor"}
    status_path.write_text(json.dumps({"corpus_fts": prior, "ready_snapshot": {"status": "failed"}}))
    paths = daily.Paths(**(paths.__dict__ | {"pipeline_status_path": status_path}))
    monkeypatch.setattr(daily, "_build_ready_snapshot_best_effort", lambda *_: {"status": "ok"})
    monkeypatch.setattr(daily, "_build_corpus_fts_best_effort", lambda *_: pytest.fail("must not rebuild"))
    summary = {"job_id": "partial-1", "status": "PARTIAL"}

    result = daily._finalize_daily_summary(paths, summary, lambda _: None)

    assert result["corpus_fts"] == prior
    persisted = json.loads(status_path.read_text())
    assert persisted["corpus_fts"] == prior
    assert persisted["ready_snapshot"]["status"] == "ok"
    assert result["corpus_fts_attempt"]["reason_code"] == "daily_not_completed"
    assert persisted["corpus_fts_attempt"]["reason_code"] == "daily_not_completed"


def test_daily_run_keeps_job_lease_until_postprocessing_finishes(paths, monkeypatch):
    calls = []
    _fake_stages(monkeypatch, calls)
    job = daily.start(paths, _plan())

    def finalize(the_paths, summary, _log):
        conn = daily.open_jobs(the_paths.jobs_db)
        lease = conn.execute("SELECT lease_owner FROM jobs WHERE job_id=?", (job,)).fetchone()[0]
        conn.close()
        assert lease
        return summary

    monkeypatch.setattr(daily, "_finalize_daily_summary", finalize)

    result = daily.run(paths, job, rt=_rt(paths), log=lambda _: None)

    assert result["status"] == "COMPLETED"
    conn = daily.open_jobs(paths.jobs_db)
    assert conn.execute("SELECT lease_owner FROM jobs WHERE job_id=?", (job,)).fetchone()[0] is None
    conn.close()


def test_corpus_fts_low_disk_skips_builder_before_any_write(paths, tmp_path, monkeypatch):

    from catalyst_data.corpus import streaming_publication
    from catalyst_data.ingestion.ops import daily

    corpus = tmp_path / "corpus.sqlite3"
    alpaca = tmp_path / "alpaca.sqlite3"
    bodies = _empty_sec_body_store(tmp_path / "bodies.sqlite3")
    sqlite3.connect(corpus).close()
    sqlite3.connect(alpaca).close()
    with sqlite3.connect(corpus) as corpus_setup:
        corpus_setup.execute("CREATE TABLE corpus_publication_builds (build_id TEXT, status TEXT)")
    paths = daily.Paths(**(paths.__dict__ | {
        "corpus_db": corpus,
        "alpaca_news_db": alpaca,
        "bodies_db": bodies,
        "corpus_min_free_gb": 10,
        "corpus_fts_rebuild_enabled": True,
    }))
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda _: SimpleNamespace(free=9_000_000_000))
    monkeypatch.setattr(daily, "_current_certified_snapshot_identity", lambda _: ("old", "c" * 64))
    monkeypatch.setattr(daily, "_generation_payload_bytes", lambda *_: 0)
    monkeypatch.setattr(streaming_publication, "_build_id", lambda _: "new-build")
    monkeypatch.setattr(streaming_publication, "build_streaming_corpus_and_lexical_index",
                        lambda *args, **kwargs: pytest.fail("builder must not start below threshold"))
    before = corpus.read_bytes()

    result = daily._build_corpus_fts_best_effort(paths, lambda _: None)

    assert result["status"] == "failed"
    assert result["reason_code"] == "low_disk"
    assert corpus.read_bytes() == before


def test_corpus_fts_requires_space_for_prior_generation_and_safety_margin(paths, tmp_path, monkeypatch):

    from catalyst_data.corpus import streaming_publication
    from catalyst_data.ingestion.ops import daily

    corpus = tmp_path / "corpus.sqlite3"
    alpaca = tmp_path / "alpaca.sqlite3"
    bodies = _empty_sec_body_store(tmp_path / "bodies.sqlite3")
    sqlite3.connect(corpus).close()
    sqlite3.connect(alpaca).close()
    with sqlite3.connect(corpus) as corpus_setup:
        corpus_setup.execute("CREATE TABLE corpus_publication_builds (build_id TEXT, status TEXT)")
    paths = daily.Paths(**(paths.__dict__ | {
        "corpus_db": corpus,
        "alpaca_news_db": alpaca,
        "bodies_db": bodies,
        "corpus_min_free_gb": 0.5,
        "corpus_fts_rebuild_enabled": True,
    }))
    monkeypatch.setattr(daily, "_current_certified_snapshot_identity", lambda _: ("old", "c" * 64))
    monkeypatch.setattr(daily, "_generation_payload_bytes", lambda *_: 100_000_000)
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda _: SimpleNamespace(free=619_999_999))
    monkeypatch.setattr(streaming_publication, "build_streaming_corpus_and_lexical_index",
                        lambda *args, **kwargs: pytest.fail("builder must not start without estimated space"))
    monkeypatch.setattr(streaming_publication, "_build_id", lambda _: "new-build")
    before = corpus.read_bytes()

    result = daily._build_corpus_fts_best_effort(paths, lambda _: None)

    assert result["status"] == "failed"
    assert result["reason_code"] == "low_disk"
    assert result["required_free_bytes"] == 620_000_000
    assert corpus.read_bytes() == before


def test_corpus_fts_opens_alpaca_read_only_and_uses_snapshot_identity(paths, tmp_path, monkeypatch):

    from catalyst_data.corpus import streaming_publication
    from catalyst_data.ingestion.ops import daily

    corpus = tmp_path / "corpus.sqlite3"
    alpaca = tmp_path / "alpaca.sqlite3"
    bodies = _empty_sec_body_store(tmp_path / "bodies.sqlite3")
    corpus_setup = sqlite3.connect(corpus)
    corpus_setup.execute("CREATE TABLE corpus_publication_builds (build_id TEXT, status TEXT)")
    corpus_setup.commit()
    corpus_setup.close()
    conn = sqlite3.connect(alpaca)
    conn.execute("CREATE TABLE news (id INTEGER)")
    conn.commit()
    conn.close()
    paths = daily.Paths(**(paths.__dict__ | {
        "corpus_db": corpus,
        "alpaca_news_db": alpaca,
        "bodies_db": bodies,
        "corpus_min_free_gb": 0,
        "corpus_fts_rebuild_enabled": True,
    }))
    current_identity = {"build_id": "previous"}
    monkeypatch.setattr(
        daily,
        "_current_certified_snapshot_identity",
        lambda _: (current_identity["build_id"], "c" * 64),
    )
    monkeypatch.setattr(daily, "_generation_payload_bytes", lambda *_: 0)
    monkeypatch.setattr(streaming_publication, "_build_id", lambda _: "build-1")
    monkeypatch.setattr("catalyst_data.corpus.retirement.prune_corpus_builds", lambda *_a, **_k: [])
    called = {}

    def fake_build(_conn, **kwargs):
        called.update(kwargs)
        with pytest.raises(sqlite3.OperationalError):
            kwargs["alpaca_news_conn"].execute("CREATE TABLE must_not_write (id INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            kwargs["sec_bodies_conn"].execute("CREATE TABLE must_not_write (id INTEGER)")
        _conn.execute(
            "INSERT INTO corpus_publication_builds (build_id,status) VALUES ('build-1','published')"
        )
        _conn.commit()
        current_identity["build_id"] = "build-1"
        return SimpleNamespace(
            build_id="build-1",
            corpus=SimpleNamespace(document_count=5, chunk_count=8),
            lexical=SimpleNamespace(mode_served="fts5", row_count=8),
        )

    monkeypatch.setattr(streaming_publication, "build_streaming_corpus_and_lexical_index", fake_build)

    result = daily._build_corpus_fts_best_effort(paths, lambda _: None)

    assert result["status"] == "ok", result
    assert called["certified_snapshot_identity"] == "c" * 64
    assert called["alpaca_source_snapshot_identity"].startswith("sha256:")
    assert called["sec_bodies_identity"].included_count == 0
    assert result["build_id"] == "build-1"


def test_corpus_fts_is_disabled_by_default_without_touching_corpus(paths, tmp_path, monkeypatch):
    from catalyst_data.ingestion.ops import daily

    corpus = tmp_path / "must-not-be-created.sqlite3"
    paths = daily.Paths(**(paths.__dict__ | {"corpus_db": corpus, "alpaca_news_db": None}))
    logs = []

    result = daily._build_corpus_fts_best_effort(paths, logs.append)

    assert result == {"status": "skipped", "reason_code": "disabled"}
    assert not corpus.exists()
    assert logs == []


def test_enabled_corpus_fts_requires_explicit_database_paths(paths):
    from catalyst_data.ingestion.ops import daily

    paths = daily.Paths(**(paths.__dict__ | {
        "corpus_db": None,
        "alpaca_news_db": None,
        "corpus_fts_rebuild_enabled": True,
    }))
    logs = []

    result = daily._build_corpus_fts_best_effort(paths, logs.append)

    assert result["status"] == "failed"
    assert result["reason_code"] == "not_configured"
    assert len(logs) == 1


def test_builder_returning_existing_published_generation_does_not_change_current(paths, tmp_path, monkeypatch):

    from catalyst_data.corpus import streaming_publication
    from catalyst_data.ingestion.ops import daily

    corpus = tmp_path / "corpus.sqlite3"
    alpaca = tmp_path / "alpaca.sqlite3"
    bodies = _empty_sec_body_store(tmp_path / "bodies.sqlite3")
    conn = sqlite3.connect(corpus)
    conn.execute("CREATE TABLE corpus_publication_builds (build_id TEXT, status TEXT)")
    conn.execute("INSERT INTO corpus_publication_builds VALUES ('already-built','published')")
    conn.commit()
    conn.close()
    sqlite3.connect(alpaca).close()
    paths = daily.Paths(**(paths.__dict__ | {
        "corpus_db": corpus, "alpaca_news_db": alpaca, "corpus_min_free_gb": 1,
        "bodies_db": bodies,
        "corpus_fts_rebuild_enabled": True,
    }))
    monkeypatch.setattr(daily, "_current_certified_snapshot_identity", lambda _: ("still-current", "c" * 64))
    monkeypatch.setattr(daily, "_generation_payload_bytes", lambda *_: 0)
    monkeypatch.setattr(streaming_publication, "_build_id", lambda _: "already-built")
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    monkeypatch.setattr(streaming_publication, "build_streaming_corpus_and_lexical_index",
                        lambda *_a, **_k: pytest.fail("published generation must not be rebuilt"))

    result = daily._build_corpus_fts_best_effort(paths, lambda _: None)

    assert result == {"status": "skipped", "reason_code": "already_published", "build_id": "already-built"}


@pytest.mark.parametrize(
    ("current_after_error", "should_rollback"),
    [("new-build", True), ("manual-choice", False)],
)
def test_corpus_failure_rolls_back_only_its_still_current_publication(
        paths, tmp_path, monkeypatch, current_after_error, should_rollback):
    from catalyst_data.corpus import rollback, streaming_publication
    from catalyst_data.ingestion.ops import daily

    corpus = tmp_path / "corpus.sqlite3"
    alpaca = tmp_path / "alpaca.sqlite3"
    bodies = _empty_sec_body_store(tmp_path / "bodies.sqlite3")
    conn = sqlite3.connect(corpus)
    conn.execute("CREATE TABLE corpus_publication_builds (build_id TEXT, status TEXT)")
    conn.commit()
    conn.close()
    sqlite3.connect(alpaca).close()
    paths = daily.Paths(**(paths.__dict__ | {
        "corpus_db": corpus, "alpaca_news_db": alpaca, "corpus_min_free_gb": 0,
        "bodies_db": bodies,
        "corpus_fts_rebuild_enabled": True,
    }))
    current = {"build_id": "previous"}
    monkeypatch.setattr(daily, "_current_certified_snapshot_identity", lambda _: (current["build_id"], "c" * 64))
    monkeypatch.setattr(daily, "_generation_payload_bytes", lambda *_: 0)
    monkeypatch.setattr(streaming_publication, "_build_id", lambda _: "new-build")
    monkeypatch.setattr("catalyst_data.corpus.retirement.purge_incomplete_builds", lambda *_a, **_k: [])
    rollback_calls = []

    def fake_rollback(db_path, **kwargs):
        rollback_calls.append((db_path, kwargs))

    monkeypatch.setattr(rollback, "rollback_database", fake_rollback)

    def failed_after_publish(db_conn, **_kwargs):
        db_conn.execute("INSERT INTO corpus_publication_builds VALUES ('new-build','published')")
        db_conn.commit()
        current["build_id"] = current_after_error
        raise RuntimeError("simulated failure after publication")

    monkeypatch.setattr(streaming_publication, "build_streaming_corpus_and_lexical_index", failed_after_publish)

    result = daily._build_corpus_fts_best_effort(paths, lambda _: None)

    assert result["status"] == "failed"
    assert bool(rollback_calls) is should_rollback
    if should_rollback:
        assert rollback_calls[0][1] == {
            "to_build_id": "previous", "expected_current_build_id": "new-build", "lock_held": True,
        }


@pytest.mark.parametrize(
    ("current_after_error", "should_rollback"),
    [("new-build", True), ("manual-choice", False)],
)
def test_corpus_failure_rolls_back_only_its_still_current_publication(
        paths, tmp_path, monkeypatch, current_after_error, should_rollback):
    from catalyst_data.corpus import rollback, streaming_publication
    from catalyst_data.ingestion.ops import daily

    corpus = tmp_path / "corpus.sqlite3"
    alpaca = tmp_path / "alpaca.sqlite3"
    bodies = _empty_sec_body_store(tmp_path / "bodies.sqlite3")
    conn = sqlite3.connect(corpus)
    conn.execute("CREATE TABLE corpus_publication_builds (build_id TEXT, status TEXT)")
    conn.commit()
    conn.close()
    sqlite3.connect(alpaca).close()
    paths = daily.Paths(**(paths.__dict__ | {
        "corpus_db": corpus, "alpaca_news_db": alpaca, "corpus_min_free_gb": 0,
        "bodies_db": bodies,
        "corpus_fts_rebuild_enabled": True,
    }))
    current = {"build_id": "previous"}
    monkeypatch.setattr(daily, "_current_certified_snapshot_identity", lambda _: (current["build_id"], "c" * 64))
    monkeypatch.setattr(daily, "_generation_payload_bytes", lambda *_: 0)
    monkeypatch.setattr(streaming_publication, "_build_id", lambda _: "new-build")
    monkeypatch.setattr("catalyst_data.corpus.retirement.purge_incomplete_builds", lambda *_a, **_k: [])
    rollback_calls = []

    def fake_rollback(db_path, **kwargs):
        rollback_calls.append((db_path, kwargs))

    monkeypatch.setattr(rollback, "rollback_database", fake_rollback)

    def failed_after_publish(db_conn, **_kwargs):
        db_conn.execute("INSERT INTO corpus_publication_builds VALUES ('new-build','published')")
        db_conn.commit()
        current["build_id"] = current_after_error
        raise RuntimeError("simulated failure after publication")

    monkeypatch.setattr(streaming_publication, "build_streaming_corpus_and_lexical_index", failed_after_publish)

    result = daily._build_corpus_fts_best_effort(paths, lambda _: None)

    assert result["status"] == "failed"
    assert bool(rollback_calls) is should_rollback
    if should_rollback:
        assert rollback_calls[0][1] == {
            "to_build_id": "previous", "expected_current_build_id": "new-build", "lock_held": True,
        }


def test_paths_from_env_requires_explicit_corpus_paths_and_rebuild_flag(tmp_path, monkeypatch):
    for name in ("CATALYST_CORPUS_DB", "CATALYST_ALPACA_NEWS_DB", "CATALYST_CORPUS_FTS_REBUILD",
                 "CATALYST_CORPUS_VACUUM"):
        monkeypatch.delenv(name, raising=False)
    peer_map = tmp_path / "peer.json"
    peer_map.write_text("{}")
    monkeypatch.setenv("CATALYST_PEER_MAP", str(peer_map))

    try:
        paths = daily.Paths.from_env(repo=tmp_path)

        assert paths.corpus_fts_rebuild_enabled is False
        assert paths.corpus_vacuum_after_prune is False
        assert paths.corpus_db is None
        assert paths.alpaca_news_db is None

        corpus, alpaca = tmp_path / "served.sqlite3", tmp_path / "archive.sqlite3"
        monkeypatch.setenv("CATALYST_CORPUS_DB", str(corpus))
        monkeypatch.setenv("CATALYST_ALPACA_NEWS_DB", str(alpaca))
        monkeypatch.setenv("CATALYST_CORPUS_FTS_REBUILD", "true")
        monkeypatch.setenv("CATALYST_CORPUS_VACUUM", "1")
        paths = daily.Paths.from_env(repo=tmp_path)
        assert paths.corpus_fts_rebuild_enabled is True
        assert paths.corpus_vacuum_after_prune is True
        assert paths.corpus_db == corpus
        assert paths.alpaca_news_db == alpaca
    finally:
        _release_paths_env()


def test_scheduled_web_discovery_runs_cnbc_roundups_per_session_best_effort(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import cnbc_roundups

    calls = []

    def fake_discover_day(session_date):
        calls.append(session_date)
        if session_date == "2026-03-03":
            raise RuntimeError("temporary roundup failure")
        return {"session_date": session_date, "status": "COMPLETED",
                "pages": 2, "segments": 3, "errors": 0, "credits_used": 0}

    monkeypatch.setattr(cnbc_roundups, "discover_day", fake_discover_day)
    messages = []
    result = daily._scheduled_web_discovery(
        paths, ["2026-03-02", "2026-03-03", "2026-03-04"], log=messages.append)

    assert calls == ["2026-03-02", "2026-03-03", "2026-03-04"]
    assert [row["session_date"] for row in result] == ["2026-03-02", "2026-03-04"]
    assert all(row["credits_used"] == 0 for row in result)
    assert messages == ["web discovery stage skipped for 2026-03-03: RuntimeError"]


def test_missing_sessions_returns_oldest_first_and_caps_at_five(paths):
    conn = daily.open_jobs(paths.jobs_db)
    days = daily.sessions_between("2026-01-05", "2026-03-06")
    conn.executemany("INSERT INTO jobs (job_id,plan_key,plan_json,status,created_at) VALUES (?,?,?,?,?)",
                     [(f"j{i}", str(i), "{}", "COMPLETED", "now") for i in range(len(days))])
    conn.executemany("INSERT INTO job_items (job_id,seq,stage,item_key,session_date,status) "
                     "VALUES (?,?,?,?,?,?)", [
        (f"j{i}", i, "verify", day, day, "OK")
        for i, day in enumerate(days) if day not in {"2026-03-04", "2026-03-06"}
    ])
    conn.commit()
    conn.close()
    assert daily._missing_sessions(paths, "2026-03-06", 8) == ["2026-03-04", "2026-03-06"]


def test_mover_threshold_matches_the_production_policy():
    import importlib.util

    if importlib.util.find_spec("catalyst_app") is None:
        pytest.skip("app package not on path")
    from catalyst_app.runtime.manifest_factory import production_observation_policy

    assert production_observation_policy().material_target_return_pct == daily.MOVER_MIN_ABS_RETURN_PCT


def test_paths_from_env_exports_the_stores_for_downstream_helpers(tmp_path, monkeypatch):
    peer_map = tmp_path / "peer.json"
    peer_map.write_text("{}")
    (tmp_path / "data/m9").mkdir(parents=True)
    (tmp_path / "data/m9/news_bodies_v2.sqlite3").touch()
    import os

    from catalyst_data.retrieval import web_bodies

    try:
        for name in _PATHS_ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CATALYST_PEER_MAP", str(peer_map))
        paths = daily.Paths.from_env(repo=tmp_path)
        assert "CATALYST_NEWS_BODIES_DB" not in os.environ
        assert "CATALYST_MARKET_CONTEXT_DB" not in os.environ
        daily.publish_paths_environment(paths, repo=tmp_path)
        assert os.environ["CATALYST_NEWS_BODIES_DB"] == str(paths.bodies_db)
        assert os.environ["CATALYST_MARKET_CONTEXT_DB"] == str(paths.market_db)
        assert web_bodies.store_path() == paths.bodies_db
    finally:
        # Pop after the body. delenv would record a leaked value and restore it
        # on undo, which is what left the body-store path in the process.
        _release_paths_env()


def test_news_stage_fails_the_item_when_a_source_answered_error(paths, monkeypatch):
    from catalyst_data.ingestion.ops import refresh

    report = refresh.RefreshReport(ticker="AAA", session_date="2026-03-03", before=["news:peer:CMI"])
    report.actions.append({"step": "finnhub+polygon", "subject": "CMI", "status": "ERROR",
                           "headlines": 0, "bodies": 0, "detail": ""})
    monkeypatch.setattr(refresh, "refresh_session", lambda *a, **k: report)
    rt = daily.Runtime(paths=paths, job_sessions=SESSIONS, refresh_deps=lambda d: None)
    with pytest.raises(RuntimeError, match="source ERROR: finnhub\\+polygon:CMI"):
        daily.stage_news(rt, "AAA", "2026-03-03")


def test_unanswered_host_is_deferred_then_fetched_when_it_answers(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import fetch
    from catalyst_data.ingestion.ops import refresh
    from catalyst_data.ingestion.storage import store

    class Down(Exception):
        pass

    state = {"up": False}

    def fetch_page(self, url):
        if not state["up"] and "yahoo" in url:
            raise fetch.HostUnavailable("finance.yahoo.com", "robots.txt ReadTimeout")
        return url, "<html>page</html>"

    monkeypatch.setattr(fetch.Fetcher, "fetch_page", fetch_page)
    monkeypatch.setattr(fetch, "extract", lambda html, title: "Body text. " * 80)
    monkeypatch.setattr(refresh.web_bodies, "publisher_for_url",
                        lambda url: "Yahoo Finance" if "yahoo" in url else "Kitco News")
    monkeypatch.setattr(store.web_bodies, "publisher_for_url",
                        lambda url: "Yahoo Finance" if "yahoo" in url else "Kitco News")
    items = [{"url": "https://finance.yahoo.com/a", "title": "AAA up", "published": "2026-03-03T14:00:00Z",
              "ticker": "AAA"},
             {"url": "https://www.kitco.com/b", "title": "AAA gold", "published": "2026-03-03T15:00:00Z",
              "ticker": "AAA"}]
    deferred = []
    rows = refresh._company_rows(fetch.Fetcher(), items, None, deferred)
    assert [r["url"] for r in rows] == ["https://www.kitco.com/b"]  # the other host still lands
    assert deferred[0]["url"] == "https://finance.yahoo.com/a" and deferred[0]["host"] == "finance.yahoo.com"
    conn = store.open_writer(paths.bodies_db)
    store.defer(conn, deferred, "2026-03-03", "2026-03-04T00:00:00Z")
    conn.close()

    first = daily.retry_deferred(paths)
    assert (first["fetched"], first["no_body"], first["still_pending"], first["hosts_down"]) == (
        0, 0, 1, ["finance.yahoo.com"])
    assert first["newly_dead"] == [] and first["dead_total"] == 0
    status, detail = daily.stage_deferred(daily.Runtime(paths=paths, job_sessions=SESSIONS))
    assert status == "EMPTY" and detail["still_pending"] == 1
    assert detail["hosts_down"] == ["finance.yahoo.com"]
    state["up"] = True
    assert daily.retry_deferred(paths)["fetched"] == 1
    conn = sqlite3.connect(paths.bodies_db)
    assert conn.execute("SELECT status FROM deferred_fetches").fetchone() == ("DONE",)
    assert conn.execute("SELECT count(*) FROM web_headlines WHERE ticker='AAA'").fetchone() == (1,)


def _queue_dead_host_links(paths, hosts_and_urls):
    from catalyst_data.ingestion.storage import store

    conn = store.open_writer(paths.bodies_db)
    store.defer(conn, [{"url": u, "title": "t", "published": "2026-03-03T14:00:00Z", "ticker": "AAA", "host": h}
                       for h, u in hosts_and_urls], "2026-03-03", "2026-03-04T00:00:00Z")
    conn.close()


def test_a_link_whose_host_fails_on_three_days_is_dead_and_stops_holding_the_job_partial(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import fetch

    def fetch_page(self, url):
        raise fetch.HostUnavailable("gone.example", "page ConnectionError")

    monkeypatch.setattr(fetch.Fetcher, "fetch_page", fetch_page)
    _queue_dead_host_links(paths, [("gone.example", "https://gone.example/a"), ("gone.example", "https://gone.example/b")])
    day = lambda d: datetime(2026, 3, d, 11, tzinfo=timezone.utc)  # noqa: E731

    first = daily.retry_deferred(paths, now=day(5))
    again = daily.retry_deferred(paths, now=day(5))  # a stage retry the same day does not count again
    assert (first["still_pending"], again["still_pending"], again["dead_total"]) == (2, 2, 0)
    assert daily.retry_deferred(paths, now=day(6))["still_pending"] == 2
    third = daily.retry_deferred(paths, now=day(7))
    assert third["still_pending"] == 0 and third["dead_total"] == 2
    assert sorted(x["url"] for x in third["newly_dead"]) == ["https://gone.example/a", "https://gone.example/b"]
    assert daily.stage_deferred(daily.Runtime(paths=paths, job_sessions=SESSIONS))[0] == "EMPTY"  # no longer raises
    assert daily.retry_deferred(paths, now=day(8))["newly_dead"] == []  # and is not tried again


def test_a_relay_that_stays_down_for_days_never_kills_a_link(paths, monkeypatch):
    """The relay is our own Mac tunnel: a long weekend without it says nothing about the site."""
    from catalyst_data.ingestion.collectors import fetch
    from catalyst_data.ingestion.storage import store

    monkeypatch.setattr(fetch.Fetcher, "fetch_page",
                        lambda self, url: (_ for _ in ()).throw(fetch.RelayUnavailable("www.benzinga.com", "relay unreachable")))
    _queue_dead_host_links(paths, [("www.benzinga.com", "https://www.benzinga.com/a"),
                                   ("www.benzinga.com", "https://www.benzinga.com/b")])
    for d in (5, 6, 7, 8, 9):
        result = daily.retry_deferred(paths, now=datetime(2026, 3, d, 11, tzinfo=timezone.utc))
        assert (result["still_pending"], result["dead_total"], result["relay_down"]) == (2, 0, ["www.benzinga.com"])
    conn = store.open_writer(paths.bodies_db)
    assert conn.execute("SELECT status, fail_days FROM deferred_fetches").fetchall() == [("PENDING", 0)] * 2
    conn.close()
    with pytest.raises(RuntimeError, match=r"relay unavailable for: www.benzinga.com$"):
        daily.stage_deferred(daily.Runtime(paths=paths, job_sessions=SESSIONS))


def test_a_down_relay_does_not_spare_a_dead_site_the_fail_day(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import fetch
    from catalyst_data.ingestion.storage import store

    def fetch_page(self, url):
        if "benzinga" in url:
            raise fetch.RelayUnavailable("www.benzinga.com", "relay unreachable")
        raise fetch.HostUnavailable("gone.example", "x")

    monkeypatch.setattr(fetch.Fetcher, "fetch_page", fetch_page)
    _queue_dead_host_links(paths, [("www.benzinga.com", "https://www.benzinga.com/a"),
                                   ("gone.example", "https://gone.example/a")])
    for d in (5, 6, 7):
        result = daily.retry_deferred(paths, now=datetime(2026, 3, d, 11, tzinfo=timezone.utc))
    conn = store.open_writer(paths.bodies_db)
    assert dict(conn.execute("SELECT host, status FROM deferred_fetches")) == {"www.benzinga.com": "PENDING", "gone.example": "DEAD"}
    conn.close()
    assert result["relay_down"] == ["www.benzinga.com"]


def test_dead_links_can_be_revived_when_the_site_returns(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import fetch
    from catalyst_data.ingestion.storage import store

    monkeypatch.setattr(fetch.Fetcher, "fetch_page",
                        lambda self, url: (_ for _ in ()).throw(fetch.HostUnavailable("gone.example", "x")))
    _queue_dead_host_links(paths, [("gone.example", "https://gone.example/a")])
    for d in (5, 6, 7):
        daily.retry_deferred(paths, now=datetime(2026, 3, d, 11, tzinfo=timezone.utc))
    conn = store.open_writer(paths.bodies_db)
    assert conn.execute("SELECT status FROM deferred_fetches").fetchone() == ("DEAD",)
    assert store.revive(conn, "gone.example") == 1
    assert conn.execute("SELECT status, fail_days FROM deferred_fetches").fetchone() == ("PENDING", 0)
    conn.close()


def test_a_queue_made_before_the_failure_columns_is_upgraded_with_the_days_links_have_waited(paths):
    from catalyst_data.ingestion.storage import store

    conn = sqlite3.connect(paths.bodies_db)
    conn.execute("CREATE TABLE deferred_fetches (url TEXT NOT NULL, ticker TEXT NOT NULL, session_date TEXT NOT NULL, "
                 "title TEXT NOT NULL, published TEXT NOT NULL, host TEXT NOT NULL, reason TEXT, status TEXT NOT NULL, "
                 "attempts INTEGER NOT NULL DEFAULT 0, deferred_at TEXT NOT NULL, resolved_at TEXT, "
                 "PRIMARY KEY (url, ticker))")
    conn.execute("INSERT INTO deferred_fetches VALUES ('u','AAA','2026-03-03','t','p','gone.example',NULL,'PENDING',60,"
                 "'2026-03-04T00:00:00Z',NULL)")
    conn.commit()
    store.ensure_deferred(conn)
    assert conn.execute("SELECT fail_days >= 60 FROM deferred_fetches").fetchone() == (1,)
    store.ensure_deferred(conn)  # idempotent
    # a link queued today is not charged a second time by the run that upgrades the table
    conn.execute("INSERT INTO deferred_fetches (url,ticker,session_date,title,published,host,status,attempts,deferred_at,"
                 "fail_days,last_fail_day) VALUES ('v','AAA','2026-03-03','t','p','gone.example','PENDING',2,"
                 "strftime('%Y-%m-%dT%H:%M:%SZ','now'),0,NULL)")
    assert store.charge_failed_day(conn, "v", "AAA", conn.execute("SELECT date('now')").fetchone()[0]) is False
    assert conn.execute("SELECT fail_days FROM deferred_fetches WHERE url='v'").fetchone() == (1,)
    conn.close()


def test_robots_timeout_is_not_a_refusal(monkeypatch):
    import requests
    from catalyst_data.ingestion.collectors import fetch

    f = fetch.Fetcher()

    def boom(*a, **k):
        raise requests.ReadTimeout("slow")

    monkeypatch.setattr(f.session, "get", boom)
    monkeypatch.setattr(f, "_wait", lambda host: None)
    with pytest.raises(fetch.HostUnavailable):
        f.fetch_page("https://finance.yahoo.com/x")
    # the host is remembered as down for this Fetcher: no second 15 s wait
    with pytest.raises(fetch.HostUnavailable):
        f.allowed("https://finance.yahoo.com/y")


def test_a_lease_held_by_a_dead_local_process_is_taken_over(paths, monkeypatch):
    import socket

    _fake_stages(monkeypatch, [])
    job = daily.start(paths, _plan())
    conn = daily.open_jobs(paths.jobs_db)
    assert daily._acquire_lease(conn, job, f"{socket.gethostname()}:999999")  # no such pid
    result = daily.run(paths, job, rt=_rt(paths), log=lambda _: None)
    assert result["status"] == "COMPLETED"


def test_price_stage_retries_and_fails_a_known_symbol_that_returns_nothing(paths, monkeypatch):
    from catalyst_data.ingestion.collectors import market

    conn = market.open_market_writer(paths.market_db)
    conn.execute("INSERT INTO ohlcv VALUES ('AAA','2026-02-27',1,1,1,1,1,'yfinance')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(daily, "PRICE_RETRY_SLEEP_S", 0)
    calls = []

    def flaky(conn, symbols, start, through, now=None):
        calls.append(symbols[0])
        if len(calls) < 3:
            return {symbols[0]: 0}
        conn.execute("INSERT OR REPLACE INTO ohlcv VALUES ('AAA','2026-03-03',1,1,1,1,1,'yfinance')")
        return {symbols[0]: 1}

    rt = daily.Runtime(paths=paths, job_sessions=SESSIONS, load_bars=flaky)
    assert daily.stage_prices(rt, "AAA")[0] == "OK" and len(calls) == 3

    rt = daily.Runtime(paths=paths, job_sessions=["2026-03-04"], load_bars=lambda *a, **k: {"AAA": 0})
    with pytest.raises(RuntimeError, match="no bars"):
        daily.stage_prices(rt, "AAA")


def test_price_stage_backfills_history_that_starts_after_the_lookback(paths):
    from catalyst_data.ingestion.collectors import market

    conn = market.open_market_writer(paths.market_db)
    for day in ("2026-03-02", "2026-03-03"):  # stored only for a later window
        conn.execute(f"INSERT INTO ohlcv VALUES ('AAA','{day}',1,1,1,1,1,'yfinance')")
    conn.commit()
    conn.close()
    seen = []
    rt = daily.Runtime(paths=paths, job_sessions=SESSIONS,
                       load_bars=lambda conn, symbols, start, through, now=None: seen.append(start) or {"AAA": 1})
    assert daily.stage_prices(rt, "AAA")[0] == "OK"
    assert seen == ["2025-11-02"]  # first session minus the 120-day lookback, not a skip


def test_paths_without_the_snapshot_fall_back_to_the_market_store(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("CATALYST_SNAPSHOT_DB", str(tmp_path / "absent.db"))
    try:
        paths = daily.Paths.from_env()
        assert paths.snapshot_db is None
        assert os.environ["CATALYST_SNAPSHOT_DB"] == str(tmp_path / "absent.db")
        daily.publish_paths_environment(paths)
        assert "CATALYST_SNAPSHOT_DB" not in __import__("os").environ
    finally:
        _release_paths_env()
