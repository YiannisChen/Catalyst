import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from catalyst_data.ingestion import ledger
from catalyst_data.ingestion.ops import health as H


def _ledger_db(path, rows):
    conn = sqlite3.connect(path)
    ledger.ensure_ledger(conn)
    for subject, source, status, at in rows:
        ledger.record_attempt(conn, subject=subject, session_date=at[:10], source=source, status=status,
                              headlines=0, bodies=0, attempted_at=at)
    conn.close()


def test_health_data_dir_preserves_precedence_and_legacy_default(tmp_path):
    assert H.resolve_data_dir({}, repo=tmp_path) == Path("/opt/catalyst/repo/data/m9")
    assert H.resolve_data_dir({
        "CATALYST_DATA_DIR": "legacy-override",
        "CATALYST_RUNTIME_DATA_DIR": "runtime-root",
    }, repo=tmp_path) == Path("legacy-override")
    assert H.resolve_data_dir({"CATALYST_RUNTIME_DATA_DIR": "runtime-root"}, repo=tmp_path) == \
        tmp_path / "runtime-root"
    absolute = tmp_path / "absolute-root"
    assert H.resolve_data_dir({"CATALYST_RUNTIME_DATA_DIR": str(absolute)}, repo=tmp_path) == absolute
    assert not (tmp_path / "runtime-root").exists()
    assert not absolute.exists()


def test_report_counts_each_ledger_by_source_and_keeps_the_last_ok(tmp_path, monkeypatch):
    data, archive = tmp_path / "data", tmp_path / "archive"
    data.mkdir(); archive.mkdir()
    monkeypatch.setattr(H, "DATA", data)
    monkeypatch.setattr(H, "ARCHIVE", archive)
    monkeypatch.setattr(H, "units", lambda: {"failed": [], "timers": []})
    _ledger_db(data / "news_bodies_v2.sqlite3", [("AAPL", "finnhub+polygon", "OK", "2026-10-21T11:10:00Z"),
                                                 ("MSFT", "finnhub+polygon", "ERROR", "2026-10-21T11:11:00Z"),
                                                 ("AAPL", "sec", "EMPTY", "2026-10-21T11:12:00Z")])
    from datetime import datetime, timezone
    r = H.report(datetime(2026, 10, 21, 15, tzinfo=timezone.utc))
    got = {(s["store"], s["source"]): (s.get("OK"), s.get("ERROR"), s.get("last_ok_at")) for s in r["sources"]}
    assert got[("daily", "finnhub+polygon")] == (1, 1, "2026-10-21T11:10:00Z")
    first_observation = next(s["first_observed_at"] for s in r["sources"]
                             if (s["store"], s["source"]) == ("daily", "finnhub+polygon"))
    assert first_observation == "2026-10-21T11:10:00Z"
    assert got[("daily", "sec")][:2] == (0, 0)
    assert not any(store in {"news_archive", "press_rss", "market_archive"} for store, _ in got)
    assert r["disk"]["free_pct"] > 0
    assert all(row["store"] != "market" for row in r["sources"])


def test_report_includes_daily_pipeline_failure_state(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone

    data = tmp_path / "data"
    data.mkdir()
    pipeline = data / "daily_pipeline_status.json"
    pipeline.write_text(json.dumps({"ready_snapshot": {"status": "failed"},
                                   "corpus_fts": {"status": "failed"}}))
    monkeypatch.setattr(H, "DATA", data)
    monkeypatch.setattr(H, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(H, "daily_job", lambda: {})
    monkeypatch.setattr(H, "prices", lambda: {})
    monkeypatch.setattr(H, "sources", lambda _now: [])
    monkeypatch.setattr(H, "page_versions", lambda _now: {})
    monkeypatch.setattr(H, "dead_links", lambda: {})
    monkeypatch.setattr(H, "never_ok", lambda _now: [])
    monkeypatch.setattr(H, "units", lambda: {})
    monkeypatch.setattr(H, "disk", lambda: {})
    monkeypatch.setattr(H, "backups", lambda: {})
    monkeypatch.setattr(H, "time_checks", lambda _now: {})
    monkeypatch.setattr(H, "cross_checks", lambda _now: {})
    monkeypatch.setattr(H, "fetching", lambda _now: {})

    result = H.report(datetime(2026, 11, 4, tzinfo=timezone.utc))

    assert result["daily_pipeline"]["ready_snapshot"]["status"] == "failed"
    assert result["daily_pipeline"]["corpus_fts"]["status"] == "failed"


def test_market_store_without_intake_log_is_not_a_ledger_failure(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(H, "DATA", data)
    monkeypatch.setattr(H, "ARCHIVE", tmp_path / "archive")
    conn = sqlite3.connect(data / "market_context.sqlite3")
    conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT)")
    conn.commit()
    conn.close()
    rows = H.sources(datetime(2026, 10, 21, 15, tzinfo=timezone.utc))
    assert rows
    assert all(row["store"] != "market" for row in rows)
    assert all("error" not in row or row["store"] != "market" for row in rows)


def test_dead_links_are_listed_by_host(tmp_path, monkeypatch):
    from catalyst_data.ingestion.storage import store

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(H, "DATA", data)
    assert H.dead_links() == {"status": "error", "reason": "missing_store"}
    conn = store.open_writer(data / "news_bodies_v2.sqlite3")
    store.defer(conn, [{"url": f"https://gone.example/{i}", "title": "t", "published": "p", "ticker": "AAA",
                        "host": "gone.example"} for i in range(2)], "2026-03-03", "2026-03-04T00:00:00Z")
    conn.execute("UPDATE deferred_fetches SET status='DEAD', resolved_at='2026-03-07'")
    conn.commit()
    conn.close()
    assert H.dead_links() == {"status": "ok", "gone.example": {"links": 2, "since": "2026-03-07"}}


def test_never_ok_lists_a_registered_alpaca_news_unit_without_success(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    data, archive = tmp_path / "data", tmp_path / "archive"
    data.mkdir(); archive.mkdir()
    monkeypatch.setattr(H, "DATA", data)
    monkeypatch.setattr(H, "ARCHIVE", archive)
    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    old = lambda hours: (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    rows = [("news:poll", "alpaca_news", "OK", old(80)),
            *[("news:poll", "alpaca_news", "EMPTY", old(h)) for h in range(1, 40)],
            *[("news:backfill", "alpaca_news", "EMPTY", old(h)) for h in range(2, 84, 10)],
            *[("news:poll-new", "alpaca_news", "EMPTY", old(h)) for h in range(1, 7)]]
    _ledger_db(archive / "alpaca_news.sqlite3", rows)
    registry = {"stores": {"alpaca_news": "/opt/catalyst/archive/alpaca_news.sqlite3"}, "sources": []}
    assert H.never_ok(now, registry) == {"status": "ok", "items": [
        {"store": "alpaca_news", "unit": "news:backfill", "attempts": 9, "first_attempt_at": old(82)}]}


def test_units_lists_a_failed_service_that_is_running_again_as_activating(monkeypatch):
    out = {"--failed": "catalyst-collect-press.service loaded failed failed x\n",
           "--state=activating": "catalyst-collect-alpaca-news.service loaded activating start x\n"}

    def fake_run(args, **_kwargs):
        text = next((v for k, v in out.items() if k in args), "")
        return type("R", (), {"stdout": text})()

    monkeypatch.setattr(H.subprocess, "run", fake_run)
    units = H.units()
    assert units["failed"] == ["catalyst-collect-press.service"]
    assert units["activating"] == ["catalyst-collect-alpaca-news.service"]


def test_sec_exhibit_units_health_check_states(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(H, "DATA", data)
    path = data / "news_bodies_v2.sqlite3"

    assert H.sec_exhibit_units() == {"status": "error", "reason": "missing_store"}

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sec_exhibit_units (id INTEGER)")
    conn.execute("INSERT INTO sec_exhibit_units VALUES (1)")
    conn.commit()
    conn.close()
    assert H.sec_exhibit_units() == {"status": "ok", "rows": 1}

    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE sec_exhibit_units")
    conn.commit()
    conn.close()
    assert H.sec_exhibit_units() == {"status": "error", "reason": "missing_table"}

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sec_exhibit_units (id INTEGER)")
    conn.commit()
    conn.close()
    assert H.sec_exhibit_units() == {"status": "error", "reason": "empty"}


def test_sec_exhibit_units_health_check_reports_sqlite_error(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(H, "DATA", data)
    (data / "news_bodies_v2.sqlite3").write_text("not a sqlite database")
    result = H.sec_exhibit_units()
    assert result["status"] == "error"
    assert result["reason"] not in {"missing_table", "empty", "missing_store"}


def test_health_store_connection_timeout_is_reported_as_error_not_missing(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from catalyst_data.storage.connect import SQLiteReadTimeoutError

    data = tmp_path / "data"
    data.mkdir()
    store = data / "news_bodies_v2.sqlite3"
    store.touch()
    monkeypatch.setattr(H, "DATA", data)
    monkeypatch.setattr(H, "connect_readonly", lambda *_a, **_k: (_ for _ in ()).throw(
        SQLiteReadTimeoutError("database is locked")))
    result = H.sources(datetime(2026, 10, 21, tzinfo=timezone.utc),
                       {"stores": {"daily": str(store)}, "sources": []})
    assert result[0]["error"].startswith("read_timeout:")
    assert "missing_store" not in result[0]


def test_read_failures_use_status_reason_shape(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(H, "DATA", data)
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(H, "ARCHIVE", archive)
    for path in (data / "update_jobs.sqlite3", data / "market_context.sqlite3",
                 data / "page_versions.sqlite3", data / "news_bodies_v2.sqlite3",
                 archive / "alpaca_news.sqlite3"):
        path.touch()

    def fail_read(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(H, "connect_readonly", lambda *_a, **_k: fail_read(None))
    now = datetime(2026, 10, 21, 15, tzinfo=timezone.utc)
    readers = (H.daily_job, H.prices, lambda: H.page_versions(now), H.dead_links,
               lambda: H.time_checks(now), lambda: H.cross_checks(now))
    for read in readers:
        result = read()
        assert result["status"] == "error"
        assert result["reason"]


def _make_health_store(tmp_path, monkeypatch, check, mode):
    data, archive = tmp_path / "data", tmp_path / "archive"
    data.mkdir(); archive.mkdir()
    monkeypatch.setattr(H, "DATA", data)
    monkeypatch.setattr(H, "ARCHIVE", archive)
    if check == "never_ok":
        path = archive / "alpaca_news.sqlite3"
        monkeypatch.setattr(H.source_registry, "load", lambda: {
            "stores": {"alpaca_news": str(path)}, "sources": []})
        if mode == "sql":
            sqlite3.connect(path).close()
        elif mode == "empty":
            _ledger_db(path, [])
        return lambda: H.never_ok(datetime(2026, 11, 4, tzinfo=timezone.utc))

    if check == "cross_checks":
        body = data / "news_bodies_v2.sqlite3"
        wire = archive / "alpaca_news.sqlite3"
        if mode != "missing":
            conn = sqlite3.connect(body)
            if mode == "empty":
                conn.execute("CREATE TABLE web_body_chunks (publisher, title, eligible_at)")
            else:
                conn.execute("CREATE TABLE unrelated (x)")
            conn.commit(); conn.close()
        if mode != "missing":
            conn = sqlite3.connect(wire)
            if mode == "empty":
                conn.executescript("CREATE TABLE news (id, created_at, headline); CREATE TABLE news_symbols (id, symbol);")
            else:
                conn.execute("CREATE TABLE unrelated (x)")
            conn.commit(); conn.close()
        return lambda: H.cross_checks(datetime(2026, 11, 4, tzinfo=timezone.utc))

    filename = {
        "page_versions": "page_versions.sqlite3", "dead_links": "news_bodies_v2.sqlite3",
        "time_checks": "page_versions.sqlite3",
    }[check]
    path = data / filename
    if mode == "sql":
        sqlite3.connect(path).close()
    elif mode == "empty":
        conn = sqlite3.connect(path)
        if check == "page_versions":
            conn.execute("CREATE TABLE page_versions (path, modified_after_cutoff, declared_modified, fetched_at)")
        elif check == "dead_links":
            conn.execute("CREATE TABLE deferred_fetches (host, status, resolved_at)")
        else:
            conn.execute("CREATE TABLE body_times (publisher, api_published, page_published, flag, recorded_at)")
        conn.commit(); conn.close()
    if check == "page_versions":
        return lambda: H.page_versions(datetime(2026, 11, 4, tzinfo=timezone.utc))
    if check == "dead_links":
        return H.dead_links
    return lambda: H.time_checks(datetime(2026, 11, 4, tzinfo=timezone.utc))


@pytest.mark.parametrize("check", ["page_versions", "dead_links", "time_checks", "cross_checks", "never_ok"])
@pytest.mark.parametrize("mode", ["missing", "sql", "empty"])
def test_health_checks_distinguish_read_failures_from_empty(tmp_path, monkeypatch, check, mode):
    read = _make_health_store(tmp_path, monkeypatch, check, mode)

    result = read()

    if mode == "empty":
        assert result["status"] == "ok"
        payload = result.get("items", {k: v for k, v in result.items() if k != "status"})
        if check == "cross_checks":
            assert payload == {"sec_vs_wire": {"compared": 0, "whole_hour": 0}}
        else:
            assert payload == {} or payload == []
    else:
        assert result["status"] == "error"
        if mode == "missing" and check not in {"cross_checks", "never_ok"}:
            assert result["reason"] == "missing_store"
        else:
            assert result["reason"]


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_units_reports_systemctl_failures(monkeypatch, failure):
    def fail_run(*_args, **_kwargs):
        if failure == "timeout":
            raise H.subprocess.TimeoutExpired("systemctl", 20)
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "systemctl denied"})()

    monkeypatch.setattr(H.subprocess, "run", fail_run)

    result = H.units()

    assert result["status"] == "error"
    assert failure in result["reason"] or "systemctl" in result["reason"]


def test_units_keeps_successful_command_results_when_another_command_fails(monkeypatch):
    def fake_run(command, **_kwargs):
        if "--failed" in command:
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "failed query"})()
        stdout = ("catalyst-collect-alpaca.service loaded activating start x\n" if "--state=activating" in command
                  else "NEXT LEFT LAST PASSED UNIT ACTIVATES\nx x x x catalyst-health.timer catalyst-health.service\n")
        return type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    monkeypatch.setattr(H.subprocess, "run", fake_run)

    result = H.units()

    assert result["status"] == "error"
    assert result["failed"] == []
    assert result["activating"] == ["catalyst-collect-alpaca.service"]
    assert "catalyst-health.timer" in result["timers"]
    assert "failed_units" in result["errors"]
    assert result["commands"]["failed_units"]["status"] == "error"
    assert result["commands"]["timers"]["status"] == "ok"


def test_units_does_not_infer_missing_timers_when_timer_query_fails(monkeypatch):
    def fake_run(command, **_kwargs):
        if "list-timers" in command:
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "permission denied"})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(H.subprocess, "run", fake_run)

    result = H.units()

    assert result["errors"].get("timers")
    assert "timers" not in result
    assert "not_installed" not in result
    assert "unregistered" not in result


def test_never_ok_ignores_retired_store_paths(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    data, archive = tmp_path / "data", tmp_path / "archive"
    data.mkdir(); archive.mkdir()
    monkeypatch.setattr(H, "DATA", data)
    monkeypatch.setattr(H, "ARCHIVE", archive)
    good = archive / "alpaca_news.sqlite3"
    _ledger_db(good, [("news:poll", "alpaca_news", "EMPTY", "2026-10-30T12:00:00Z") for _ in range(5)])
    broken = archive / "press_releases.sqlite3"
    sqlite3.connect(broken).close()
    monkeypatch.setattr(H.source_registry, "load", lambda: {"stores": {
        "alpaca_news": str(good), "press_rss": str(broken)}, "sources": []})
    result = H.never_ok(datetime(2026, 11, 4, tzinfo=timezone.utc))
    assert result["status"] == "ok"
    assert result["items"] == [{"store": "alpaca_news", "unit": "news:poll", "attempts": 5,
                                 "first_attempt_at": "2026-10-30T12:00:00Z"}]
    assert "press_rss" not in result.get("errors", {})
