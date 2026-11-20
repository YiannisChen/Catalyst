"""Fault injection for the data layer's unattended operation (docs/data/operations.md): each failure is simulated with
stubs and temporary stores (never the server's), carried through the collector, the health report and the monitor,
and the outcome is checked against the design: what the system does by itself, and which tier the alert gets."""
import importlib.util
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from catalyst_data.ingestion import ledger, sources
from catalyst_data.ingestion.collectors import fetch
from catalyst_data.ingestion.ops import health as H

REPO = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("monitor", REPO / "deploy/monitor/monitor.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
NOW = datetime(2026, 12, 2, 15, 0, tzinfo=timezone.utc)  # a Wednesday, long after every source's ``since``


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Empty data and archive directories, every active source healthy an hour ago, a roomy disk, no systemd."""
    data, archive = tmp_path / "data", tmp_path / "archive"
    data.mkdir(); archive.mkdir()
    monkeypatch.setattr(H, "DATA", data)
    monkeypatch.setattr(H, "ARCHIVE", archive)
    monkeypatch.setattr(H, "BACKUP", tmp_path / "backup")
    monkeypatch.setattr(H, "units", lambda: {"failed": [], "timers": [], "unregistered": [], "not_installed": []})
    monkeypatch.setattr(M.shutil, "disk_usage", lambda p: type("U", (), {"total": 100, "free": 90})())
    at = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reg = sources.load()
    for store, path in reg["stores"].items():
        conn = sqlite3.connect(H._store_path(store, path))
        ledger.ensure_ledger(conn)
        for e in reg["sources"]:
            if e["store"] == store and e["status"] in sources.ACTIVE and e["expected_hours"] is not None:
                _healthy_row(conn, store, e["key"].replace("*", "x"), at)
        conn.commit(); conn.close()
    # Keep the healthy baseline valid for the body-store integrity check.
    with sqlite3.connect(data / "news_bodies_v2.sqlite3") as conn:
        conn.execute("CREATE TABLE sec_exhibit_units (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO sec_exhibit_units (id) VALUES (1)")
    # The report now checks these optional health tables explicitly. Give the baseline a valid empty schema;
    # the fault cases below still corrupt/delete stores or inject real read errors.
    conn = sqlite3.connect(data / "page_versions.sqlite3")
    conn.execute("CREATE TABLE page_versions (path TEXT, modified_after_cutoff INTEGER, declared_modified TEXT, fetched_at TEXT)")
    conn.execute("CREATE TABLE body_times (publisher TEXT, api_published TEXT, page_published TEXT, flag TEXT, recorded_at TEXT)")
    conn.commit(); conn.close()
    conn = sqlite3.connect(data / "news_bodies_v2.sqlite3")
    conn.execute("CREATE TABLE deferred_fetches (host TEXT, status TEXT, resolved_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS web_body_chunks (title TEXT, publisher TEXT, eligible_at TEXT)")
    conn.commit(); conn.close()
    return data, archive


def _healthy_row(conn, store, key, at):
    if store == "daily":
        subject, source = "X", key
    else:
        subject, source = key, store
    ledger.record_attempt(conn, subject=subject, session_date=at[:10], source=source, status="OK",
                          headlines=1, bodies=1, attempted_at=at)


def judge(**override):
    """The health report as the server would write it, judged by the monitor; {key: tier}."""
    report = H.report(NOW)
    report.update({"daily_job": {"created_at": NOW.strftime("%Y-%m-%dT11:00:00Z"), "status": "PARTIAL", "stages": {}},
                   "backup": {"latest": NOW.date().isoformat(), "manifest_at": NOW.strftime("%Y-%m-%dT09:00:00Z")}},
                  **override)
    return {k: p["tier"] for k, p in M.problems(report, None, NOW).items()}


def test_baseline_is_quiet(server):
    assert judge() == {}


# --- D01 transient failures: network errors, 429, 5xx, timeouts -------------------------------------------------

class _Session:
    def __init__(self, answers):
        self.answers, self.calls, self.headers = list(answers), 0, {}

    def get(self, url, **_):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return type("R", (), {"status_code": a, "headers": {}, "is_redirect": False, "url": url, "text": ""})()


def test_429_and_5xx_are_retried_with_backoff(monkeypatch):
    slept = []
    monkeypatch.setattr(fetch.time, "sleep", slept.append)
    f = fetch.Fetcher(_Session([429, 503, 200]))
    f.next_slot = __import__("collections").defaultdict(float)
    assert f.get("https://example.test/a").status_code == 200
    assert [s for s in slept if s >= 5] == [5, 10]  # 5 s, then 10 s (the 1 s host pacing aside)


def test_a_host_that_does_not_answer_is_marked_down_for_the_run(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    session = _Session([requests.ConnectionError()] * 3)
    f = fetch.Fetcher(session)
    f.next_slot = __import__("collections").defaultdict(float)
    with pytest.raises(fetch.HostUnavailable):
        f.get("https://down.test/a")
    with pytest.raises(fetch.HostUnavailable):  # circuit open: no further request this run
        f.get("https://down.test/b")
    assert session.calls == 3


# --- D30 stores: corrupt, locked, deleted ------------------------------------------------------------------------

def test_a_deleted_store_is_tier_one(server):
    _, archive = server
    (archive / "alpaca_news.sqlite3").unlink()
    assert judge() == {"store:alpaca_news": 1}


# --- D40 upstream data errors: whole-hour time offsets -----------------------------------------------------------

def test_whole_hour_offsets_from_a_publisher_are_tier_one(server):
    data, _ = server
    conn = sqlite3.connect(data / "page_versions.sqlite3")
    conn.execute("CREATE TABLE IF NOT EXISTS body_times (publisher TEXT, api_published TEXT, page_published TEXT, flag TEXT, "
                 "recorded_at TEXT)")
    conn.executemany("INSERT INTO body_times VALUES (?,?,?,?,?)",
                     [("Benzinga", "2026-12-02T09:00:00Z", "2026-12-02T13:00:00Z", "suspected_tz", "2026-12-02T12:00:00Z")]
                     * 25 + [("Yahoo", "a", "b", "agree", "2026-12-02T12:00:00Z")] * 40)
    conn.commit(); conn.close()
    assert judge() == {"time:Benzinga": 1}


# --- D50-D53 infrastructure ----------------------------------------------------------------------------------------

def test_disk_nearly_full_is_tier_one(server):
    assert judge(disk={"free_pct": 8, "free_gb": 10}) == {"disk": 1}


def test_a_crashed_collector_unit_takes_its_sources_tier(server):
    failed = {"failed": ["catalyst-collect-alpaca-news.service", "catalyst-health.service"], "timers": [],
              "unregistered": [], "not_installed": []}
    assert judge(units=failed) == {"unit:catalyst-collect-alpaca-news.service": 2, "unit:catalyst-health.service": 1}


def test_no_backup_today_is_tier_one(server):
    late = NOW.replace(hour=17)
    report = {**H.report(NOW), "generated_at": late.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "daily_job": {"created_at": NOW.strftime("%Y-%m-%dT11:00:00Z"), "status": "PARTIAL"},
              "backup": {"latest": "2026-12-01", "manifest_at": "2026-12-01T15:01:00Z"}}
    got = {k: p["tier"] for k, p in M.problems(report, None, late).items()}
    assert got["backup_missing"] == 1


def test_the_data_server_unreachable_is_tier_one():
    assert {k: p["tier"] for k, p in M.problems(None, "ssh: connect timed out", NOW).items()} == {"unreachable": 1}


# --- D20 missed runs ------------------------------------------------------------------------------------------------

def test_daily_and_slower_timers_catch_up_after_a_reboot():
    """Persistent=true makes systemd run a missed timer once at boot; the collectors then fill only what is missing
    (minute bars: the last 5 sessions; the daily job: at most 5 sessions, daily.MAX_SESSIONS)."""
    for f in sources.unit_paths(sources.DEPLOY, "catalyst-*.timer"):
        text = f.read_text()
        cals = re.findall(r"OnCalendar=(.+)", text)
        sub_daily = len(cals) > 1 or re.search(r"\*:|/\d|,\d\d:", cals[0]) is not None
        assert sub_daily or "Persistent=true" in text, f.name


# --- D43 one event, two independent sources, a whole-hour apart -------------------------------------------------

def test_an_8k_four_hours_off_the_wire_is_tier_one(server):
    data, archive = server
    conn = sqlite3.connect(data / "news_bodies_v2.sqlite3")
    conn.execute("CREATE TABLE IF NOT EXISTS web_body_chunks (title TEXT, publisher TEXT, eligible_at TEXT)")
    wire = sqlite3.connect(archive / "alpaca_news.sqlite3")
    wire.execute("CREATE TABLE IF NOT EXISTS news (id INTEGER, created_at TEXT, headline TEXT)")
    wire.execute("CREATE TABLE IF NOT EXISTS news_symbols (id INTEGER, symbol TEXT)")
    for i in range(12):  # accepted 15:10 EST, the 8-K's stored time 5 h late, as before the SEC fix
        conn.execute("INSERT INTO web_body_chunks VALUES (?,?,?)",
                     (f"Co{i} (T{chr(65 + i)}) Form 8-K EX-99.1: Results", "SEC filing", "2026-12-01T01:10:00Z"))
        wire.execute("INSERT INTO news VALUES (?,?,?)", (i, "2026-11-30T20:10:00Z", f"Co{i} Reports Q3 Results"))
        wire.execute("INSERT INTO news_symbols VALUES (?,?)", (i, f"T{chr(65 + i)}"))
    conn.commit(); wire.commit(); conn.close(); wire.close()
    assert judge() == {"cross:sec_vs_wire": 1}
