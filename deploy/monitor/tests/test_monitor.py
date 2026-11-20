import importlib.util
import json
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("monitor", Path(__file__).parents[1] / "monitor.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
AT = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)  # a Wednesday, after the daily job, before the backup check
REGISTRY = json.loads((Path(__file__).parents[3] / "data/manifests/sources_v1.json").read_text())


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):  # the monitor also checks its own disk; tests must not depend on this machine's
    monkeypatch.setattr(M.shutil, "disk_usage", lambda p: namedtuple("U", "total used free")(100, 10, 90))


def _src(store, source, ok=0, empty=0, error=0, disabled=0, last_done="2026-10-21T14:00:00Z", history=None):
    return {"store": store, "source": source, "OK": ok, "EMPTY": empty, "ERROR": error, "DISABLED": disabled,
            "attempts": ok + empty + error, "last_ok_at": last_done, "last_done_at": last_done,
            "history": history or []}


def _healthy():
    """A recent success for every active registry source that is expected to run (late enough for every test)."""
    return [_src(e["store"], e["key"].replace("*", "x"), ok=1,
                 last_done="2026-10-21T16:50:00Z") for e in REGISTRY["sources"]
            if e["status"] in M.ACTIVE and e["expected_hours"] is not None]


def _report(**k):
    base = {"generated_at": "2026-10-21T14:55:00Z",
            "daily_job": {"job_id": "upd-2026-10-20-x", "status": "PARTIAL", "created_at": "2026-10-21T11:00:02Z",
                          "stages": {"news": {"OK": 20}, "deferred": {"FAILED": 1}}},
            "sources": _healthy(), "units": {"failed": []}, "disk": {"free_pct": 80, "free_gb": 100},
            "backup": {"latest": "2026-10-20", "manifest_at": "2026-10-20T15:01:00Z"}, "registry": REGISTRY}
    if "sources" in k:  # replace the healthy rows of the same keys
        given = {(x["store"], x["source"]) for x in k["sources"]}
        k["sources"] = [x for x in base["sources"] if (x["store"], x["source"]) not in given] + k["sources"]
    base.update(k)
    return base


def _tiers(found):
    return {k: p["tier"] for k, p in found.items()}


def test_a_healthy_report_has_no_problem_and_deferred_failures_are_ignored():
    assert M.problems(_report(), None, AT) == {}


def test_unreachable_stale_and_daily_problems():
    assert list(M.problems(None, "timeout", AT)) == ["unreachable"]
    assert "stale_report" in M.problems(_report(generated_at="2026-10-21T13:00:00Z"), None, AT)
    late = _report(daily_job={"job_id": "upd-2026-10-20-x", "status": "RUNNING", "created_at": "2026-10-21T11:00Z"})
    assert "daily_not_done" in M.problems(late, None, AT)
    old = _report(daily_job={"job_id": "upd-2026-10-19-x", "status": "PARTIAL", "created_at": "2026-10-20T11:00Z"})
    assert "daily_not_started" in M.problems(old, None, AT)
    failed = _report(daily_job={**_report()["daily_job"], "stages": {"news": {"FAILED": 22}}})
    assert M.problems(failed, None, AT)["daily_failed:news"]["tier"] == 1
    assert "no_registry" in M.problems(_report(registry=None), None, AT)


def test_daily_postprocess_failures_are_urgent_monitor_problems(tmp_path, monkeypatch):
    report = _report(daily_pipeline={
        "ready_snapshot": {"status": "failed", "reason": "snapshot write failed"},
        "corpus_fts": {"status": "failed", "reason": "low disk"},
    })

    found = M.problems(report, None, AT)

    assert found["daily_postprocess:ready_snapshot"]["tier"] == 1
    assert found["daily_postprocess:corpus_fts"]["tier"] == 1

    sent = []
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "fetch_report", lambda: (report, None))
    monkeypatch.setattr(M, "now", lambda: AT)
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))
    assert M.check() == 0
    assert len(sent) == 1 and "[Catalyst] 告警：" in sent[0][0]
    assert "ready_range 快照失败" in sent[0][1]
    assert "正文 FTS 重建失败" in sent[0][1]


@pytest.mark.parametrize("check", ["page_versions", "dead_links", "time_checks", "cross_checks", "never_ok", "units"])
def test_health_read_errors_are_d23_alerts_and_ok_empty_is_not(tmp_path, monkeypatch, check):
    error_report = _report(**{check: {"status": "error", "reason": "missing_store"}})

    found = M.problems(error_report, None, AT)

    key = f"health_check_unavailable:{check}"
    problem = found[key]
    assert problem["tier"] == 1 and problem["kind"] == "health_check_unavailable"
    assert "检查不可用：missing_store" in problem["text"]
    healthy = M.problems(_report(page_versions={"status": "ok"}, dead_links={"status": "ok"},
                                 time_checks={"status": "ok"}, cross_checks={"status": "ok"},
                                 never_ok={"status": "ok", "items": []}, units={"status": "ok", "failed": []}), None, AT)
    assert not any(k.startswith("health_check_unavailable:") for k in healthy)
    assert healthy == {}


@pytest.mark.parametrize("check", ["page_versions", "dead_links", "time_checks", "cross_checks", "never_ok", "units"])
def test_daily_summary_displays_health_check_unavailability(tmp_path, monkeypatch, check):
    report = _report(**{check: {"status": "error", "reason": "missing_store"}})
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "fetch_report", lambda: (report, None))
    monkeypatch.setattr(M, "now", lambda: AT)
    sent = []
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))

    M.daily()

    daily = next(body for subject, body in sent if "日报" in subject)
    assert f"{check} 检查不可用：missing_store" in daily


def test_unknown_health_errors_have_their_own_alert_description():
    report = _report(units={"status": "error", "errors": {"failed_units": "systemctl denied"},
                           "failed": [], "activating": ["catalyst-collect-x.service"],
                           "timers": ["catalyst-health.timer"], "not_installed": ["catalyst-health.timer"]})

    found = M.problems(report, None, AT)

    unavailable = found["health_check_unavailable:units:failed_units"]
    assert unavailable["tier"] == 1
    assert unavailable["kind"] == "health_check_unavailable"
    assert "影响" not in unavailable["text"]
    assert "timer_missing:catalyst-health.timer" in found


def test_unavailable_checks_do_not_resolve_prior_alerts_or_reset_unit_failure_clock(tmp_path, monkeypatch):
    prior = (AT - timedelta(hours=1)).isoformat()
    state = {"alerted": {"time:Benzinga": prior, "cross:sec_vs_wire": prior,
                         "never_ok:alpaca_news:news:poll": prior,
                         "unit:catalyst-collect-x.service": prior},
             "unit_failed_since": {"unit:catalyst-collect-x.service": (AT - timedelta(hours=3)).isoformat()}}
    report = _report(time_checks={"status": "error", "reason": "locked"},
                     cross_checks={"status": "error", "reason": "locked"},
                     never_ok={"status": "error", "items": [], "errors": {"alpaca_news": "locked"}},
                     units={"status": "error", "errors": {"failed_units": "systemctl denied"},
                            "failed": [], "activating": [], "timers": []})
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    M.STATE.write_text(json.dumps(state))
    monkeypatch.setattr(M, "fetch_report", lambda: (report, None))
    monkeypatch.setattr(M, "now", lambda: AT)
    sent = []
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))

    M.check()

    saved = json.loads(M.STATE.read_text())
    assert set(state["alerted"]).issubset(saved["alerted"])
    assert saved["unit_failed_since"] == state["unit_failed_since"]
    assert not any("已恢复" in subject for subject, _body in sent)
    assert any("检查不可用" in body for _subject, body in sent)


def test_unavailable_time_check_does_not_mark_daily_tier_two_alert_recovered(tmp_path, monkeypatch):
    prior_problem = {"tier": 2, "kind": "time", "ref": "D40", "text": "Benzinga offset",
                     "impact": "i", "auto": "a", "human": "h", "first": AT.isoformat(), "last": AT.isoformat()}
    report = _report(time_checks={"status": "error", "reason": "locked"})
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    M.STATE.write_text(json.dumps({"low": {"time:Benzinga": prior_problem}}))
    monkeypatch.setattr(M, "fetch_report", lambda: (report, None))
    monkeypatch.setattr(M, "now", lambda: AT)
    sent = []
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))

    M.daily()

    body = next(body for subject, body in sent if "日报" in subject)
    assert "已恢复" not in body
    assert "Benzinga offset" in body
    assert "time:Benzinga" in json.loads(M.STATE.read_text())["low"]


def test_error_rate_and_silence_for_registered_sources():
    r = _report(sources=[
        _src("daily", "finnhub+polygon", ok=80, error=20),
        _src("alpaca_news", "news:poll", ok=5, error=5, last_done="2026-10-21T11:50:00Z"),
        _src("daily", "sec", ok=3, error=1),
    ])
    assert _tiers(M.problems(r, None, AT)) == {
        "errors:daily:finnhub+polygon": 1,
        "errors:alpaca_news:news:poll": 2,
        "silent:alpaca_news:news:poll": 2,
    }


def _at(day_hour: str, r: dict) -> tuple[dict, datetime]:
    """The report moved to another time, every source done an hour before it."""
    at = datetime.fromisoformat(day_hour + ":00+00:00")
    stamp = f"{day_hour[:10]}T{int(day_hour[11:13]) - 1:02d}:00:00Z"
    return {**r, "generated_at": day_hour + ":00Z",
            "sources": [{**x, "last_done_at": stamp} if "missing_store" not in x else x for x in r["sources"]],
            "backup": {"latest": day_hour[:10], "manifest_at": f"{day_hour[:10]}T09:00:00Z"},
            "daily_job": {**r["daily_job"], "created_at": f"{day_hour[:10]}T11:00:00Z"}}, at


def test_unknown_key_in_a_retained_store_is_logged():
    r = _report(sources=[_src("alpaca_news", "mystery", ok=1)])
    assert _tiers(M.problems(r, None, AT)) == {"unregistered:alpaca_news:mystery": 3}


def test_an_active_source_without_any_ledger_row_is_reported():
    r = _report()
    r["sources"] = [x for x in r["sources"] if x["source"] != "news:poll"]
    assert _tiers(M.problems(r, None, AT)) == {"never_ran:alpaca_news:news:poll": 2}


def test_unit_tiers_and_timer_drift():
    r = _report(units={"failed": ["catalyst-backup.service", "catalyst-collect-calendar.service"],
                       "unregistered": ["catalyst-research-benzinga2.timer"], "not_installed": ["catalyst-collect-calendar"]})
    assert _tiers(M.problems(r, None, AT)) == {
        "unit:catalyst-backup.service": 1, "unit:catalyst-collect-calendar.service": 1,
        "timer_unregistered:catalyst-research-benzinga2.timer": 3, "timer_missing:catalyst-collect-calendar": 2}


def test_every_kind_has_a_failure_entry_and_the_full_text():
    for kind, (tier, ref, impact, auto, human) in M.KINDS.items():
        assert tier in (1, 2, 3) and ref.startswith("D") and impact and auto and human, kind


def test_only_tier_one_is_mailed_and_the_rest_goes_to_the_daily_summary(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "BACKUP", tmp_path / "backup")
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))
    monkeypatch.setattr(M, "now", lambda: AT)
    r = _report(sources=[_src("alpaca_news", "news:poll", ok=5, last_done="2026-10-21T11:00:00Z")])
    monkeypatch.setattr(M, "fetch_report", lambda: (r, None))
    M.check()
    assert sent == []
    monkeypatch.setattr(M, "fetch_report", lambda: (_report(), None))
    M.check()  # recovered before the summary: still listed, marked recovered
    M.daily()
    subject, body = sent[-1]
    assert "1 个问题" in subject and "news:poll" in body and "operations.md D02" in body and "已恢复" in body
    assert json.loads((tmp_path / "state.json").read_text())["low"] == {}
    monkeypatch.setattr(M, "fetch_report", lambda: (_report(sources=[_src("daily", "finnhub+polygon", ok=80, error=20)]), None))
    M.check()
    subject, body = sent[-1]
    assert subject == "[Catalyst] 告警：1 项" and "影响：" in body and "需要人做：" in body


def test_alerts_are_not_repeated_within_six_hours_and_recoveries_are_mailed(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append(subject))
    monkeypatch.setattr(M, "fetch_report", lambda: (None, "down"))
    monkeypatch.setattr(M, "now", lambda: AT)
    M.check(); M.check()
    assert sent == ["[Catalyst] 告警：1 项"]
    monkeypatch.setattr(M, "fetch_report", lambda: (_report(), None))
    M.check()
    assert sent[-1] == "[Catalyst] 已恢复"
    assert json.loads((tmp_path / "state.json").read_text())["alerted"] == {}


def _week(ok, empty=0):
    return [{"OK": ok, "EMPTY": empty, "ERROR": 0} for _ in range(7)]


def test_fewer_ok_than_usual_and_empty_jump():
    broken = _src("alpaca_news", "news:poll", ok=30, empty=170, history=_week(120, 80))
    got = M.problems(_report(sources=[broken]), None, AT)
    assert set(got) == {"usual:alpaca_news:news:poll"}


def test_backup_rules():
    late = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)
    fresh = {"generated_at": "2026-10-21T16:55:00Z"}
    assert set(M.problems(_report(**fresh), None, late)) == {"backup_missing"}
    old = _report(backup={"latest": "2026-10-19", "manifest_at": "2026-10-19T15:01:00Z"})
    assert {"backup_stale"} <= set(M.problems(old, None, AT))
    today = _report(backup={"latest": "2026-10-21", "manifest_at": "2026-10-21T15:01:00Z"}, **fresh)
    assert M.problems(today, None, late) == {}


def test_failed_pull_alerts_until_a_pull_succeeds(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append(body))
    monkeypatch.setattr(M, "now", lambda: AT)
    monkeypatch.setattr(M, "fetch_report", lambda: (_report(), None))
    monkeypatch.setattr(M, "_pull", lambda: (_ for _ in ()).throw(RuntimeError("sha256 mismatch")))
    with pytest.raises(RuntimeError):
        M.pull_backup()
    M.check()
    assert "sha256 mismatch" in sent[-1]
    monkeypatch.setattr(M, "_pull", lambda: ("2026-10-21", 13))
    M.pull_backup()
    M.check()
    assert json.loads((tmp_path / "state.json").read_text())["alerted"] == {}


def test_whole_hour_time_offsets():
    r = _report(time_checks={"CNBC": {"compared": 30, "whole_hour": 30, "imprecise": 2},
                             "Yahoo Finance": {"compared": 200, "whole_hour": 2, "imprecise": 0},
                             "Kitco": {"compared": 5, "whole_hour": 5, "imprecise": 0}})
    assert _tiers(M.problems(r, None, AT)) == {"time:CNBC": 1}


def test_error_and_non_dict_time_check_entries_are_skipped():
    r = _report(time_checks={"failed": {"status": "error", "reason": "database is locked"},
                             "malformed": "database is locked",
                             "CNBC": {"compared": 30, "whole_hour": 30, "disagree": 0}})
    r["cross_checks"] = {"failed": {"status": "error", "reason": "read_timeout"},
                         "malformed": "read_timeout"}
    assert _tiers(M.problems(r, None, AT)) == {"time:CNBC": 1}


def test_an_errored_entry_with_counts_raises_no_offset_alert():
    r = _report(time_checks={"Benzinga": {"status": "error", "reason": "locked", "compared": 30, "whole_hour": 30}})
    r["cross_checks"] = {"sec": {"status": "error", "reason": "locked", "compared": 20, "whole_hour": 20}}
    assert _tiers(M.problems(r, None, AT)) == {}


def test_low_eventual_fetch_success_is_tier_two_and_gdelt_never_alerts():
    r = _report(fetching={"seekingalpha": {"requests": 400, "first_try_ok": 0.80, "ok_after_retries": 0.90, "eventual_ok": 0.91},
                          "alpaca": {"requests": 400, "first_try_ok": 0.97, "ok_after_retries": 1.0, "eventual_ok": 1.0},
                          "gdelt": {"requests": 100, "first_try_ok": 0.2, "ok_after_retries": 0.3, "eventual_ok": 0.3},
                          "rss": {"requests": 10, "first_try_ok": 0.5, "ok_after_retries": 0.5, "eventual_ok": 0.5}})
    assert _tiers(M.problems(r, None, AT)) == {"fetch:seekingalpha": 2}


def test_legacy_mixed_eventual_cannot_hide_failed_request_calls():
    r = _report(fetching={"alpaca": {"requests": 400, "first_try_ok": 0.5,
                                     "ok_after_retries": 0.5, "eventual_ok": 1.0}})
    assert _tiers(M.problems(r, None, AT)) == {"fetch:alpaca": 2}


def test_layered_fetch_alert_uses_task_denominator_and_names_it():
    item = {"tasks": {"registered": 100, "succeeded": 80, "failed": 10, "pending": 10, "rate": 0.8},
            "requests": {"attempts": 400, "successes": 350, "rate": 0.875},
            "targets": {"registered": 0, "acquired": 0, "rate": None}}
    r = _report(fetching={"schema_version": 2, "sources": {"alpaca_news:news:poll": item}})
    problems = M.problems(r, None, AT)
    assert _tiers(problems) == {"fetch:alpaca_news:news:poll": 2}
    assert "任务" in problems["fetch:alpaca_news:news:poll"]["text"]


def test_a_unit_that_never_had_an_ok_is_a_tier_2_problem():
    row = {"store": "alpaca_news", "unit": "news:poll", "attempts": 2175,
           "first_attempt_at": "2026-10-21T17:01:37Z"}
    found = M.problems(_report(never_ok=[row]), None, AT)
    key = "never_ok:alpaca_news:news:poll"
    assert found[key]["tier"] == 2 and "2175" in found[key]["text"]
    assert not [k for k in M.problems(_report(), None, AT) if k.startswith("never_ok")]


def test_a_collector_unit_that_keeps_failing_is_mailed_although_its_source_is_tier_two(tmp_path, monkeypatch):
    """Fault injection (10-29): alpaca news exited non-zero every hour for a day and nothing was mailed."""
    sent = []
    clock = {"at": AT}
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "BACKUP", tmp_path / "backup")
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))
    monkeypatch.setattr(M, "now", lambda: clock["at"])
    failing = _report(units={"failed": ["catalyst-collect-alpaca-news.service"]})
    monkeypatch.setattr(M, "fetch_report", lambda: (failing, None))
    M.check()
    assert sent == []  # a single failure is still a summary line
    clock["at"] = AT + timedelta(hours=M.UNIT_STUCK_HOURS, minutes=5)
    M.check()
    assert len(sent) == 1 and "catalyst-collect-alpaca-news.service" in sent[0][1] and "连续失败" in sent[0][1]
    clock["at"] += timedelta(minutes=10)
    M.check()
    assert len(sent) == 1  # not repeated within REALERT_HOURS
    monkeypatch.setattr(M, "fetch_report", lambda: (_report(), None))  # one good run clears the clock
    M.check()
    assert json.loads((tmp_path / "state.json").read_text())["unit_failed_since"] == {}


def test_a_damaged_unit_clock_does_not_stop_the_check(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "BACKUP", tmp_path / "backup")
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))
    monkeypatch.setattr(M, "now", lambda: AT)
    (tmp_path / "state.json").write_text(json.dumps({"unit_failed_since": {"unit:catalyst-collect-alpaca-news.service": "garbage"}}))
    monkeypatch.setattr(M, "fetch_report", lambda: (_report(units={"failed": ["catalyst-collect-alpaca-news.service"]}), None))
    M.check()  # must not raise
    since = json.loads((tmp_path / "state.json").read_text())["unit_failed_since"]
    assert since["unit:catalyst-collect-alpaca-news.service"] == AT.isoformat()


def test_a_failed_unit_that_is_running_again_keeps_its_failure_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "BACKUP", tmp_path / "backup")
    monkeypatch.setattr(M, "send", lambda subject, body: None)
    clock = {"at": AT}
    monkeypatch.setattr(M, "now", lambda: clock["at"])
    name = "catalyst-collect-alpaca-news.service"
    monkeypatch.setattr(M, "fetch_report", lambda: (_report(units={"failed": [name]}), None))
    M.check()
    clock["at"] = AT + timedelta(minutes=30)  # it is started again and is still activating: not in 'failed'
    monkeypatch.setattr(M, "fetch_report", lambda: (_report(units={"failed": [], "activating": [name]}), None))
    M.check()
    assert json.loads((tmp_path / "state.json").read_text())["unit_failed_since"] == {f"unit:{name}": AT.isoformat()}
    clock["at"] = AT + timedelta(minutes=40)  # it finished well: recovery clears the clock
    monkeypatch.setattr(M, "fetch_report", lambda: (_report(), None))
    M.check()
    assert json.loads((tmp_path / "state.json").read_text())["unit_failed_since"] == {}


def test_sec_exhibit_units_error_is_tier_one_and_ok_is_silent():
    failed = M.problems(_report(sec_exhibit_units={"status": "error", "reason": "missing_table"}), None, AT)
    assert failed["sec_exhibit_units"]["tier"] == 1
    assert "正文库 SEC 附件单元检查失败（missing_table）" in failed["sec_exhibit_units"]["text"]
    assert "核对 sec_exhibit_units 行数" in failed["sec_exhibit_units"]["human"]
    healthy = M.problems(_report(sec_exhibit_units={"status": "ok", "rows": 12}), None, AT)
    assert "sec_exhibit_units" not in healthy


@pytest.mark.parametrize("health_key", ["sec_exhibit_units"])
@pytest.mark.parametrize("next_report", ["none", "stale", "missing_key"])
def test_store_health_alert_is_kept_until_a_fresh_explicit_healthy_result(
    tmp_path, monkeypatch, health_key, next_report,
):
    sent = []
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "BACKUP", tmp_path / "backup")
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))
    monkeypatch.setattr(M, "now", lambda: AT)
    M.STATE.write_text(json.dumps({"alerted": {health_key: AT.isoformat()}}))
    if next_report == "none":
        monkeypatch.setattr(M, "fetch_report", lambda: (None, "ssh down"))
    elif next_report == "stale":
        monkeypatch.setattr(M, "fetch_report", lambda: (_report(
            generated_at=(AT - timedelta(hours=1)).isoformat(),
        ), None))
    else:
        monkeypatch.setattr(M, "fetch_report", lambda: (_report(), None))

    M.check()

    assert health_key in json.loads(M.STATE.read_text())["alerted"]
    assert all(subject != "[Catalyst] 已恢复" for subject, _ in sent)


def test_store_health_alert_recovers_after_fresh_sec_ok(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "BACKUP", tmp_path / "backup")
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))
    monkeypatch.setattr(M, "now", lambda: AT)
    alerted = {"sec_exhibit_units": AT.isoformat()}
    M.STATE.write_text(json.dumps({"alerted": alerted}))
    monkeypatch.setattr(M, "fetch_report", lambda: (_report(
        sec_exhibit_units={"status": "ok", "rows": 1},
    ), None))

    M.check()

    assert sent[0][0] == "[Catalyst] 已恢复"
    assert set(json.loads(M.STATE.read_text())["alerted"]) == set()
