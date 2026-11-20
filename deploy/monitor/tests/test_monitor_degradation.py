"""Synthetic regression tests for the O1 continuous-failure notices."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("monitor_degradation", Path(__file__).parents[1] / "monitor.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

AT = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)
SOURCE_KEY = "synthetic_source"
SOURCE_ID = f"archive:{SOURCE_KEY}"


def _registry(status="enabled"):
    return {
        "sources": [{
            "id": SOURCE_ID,
            "store": "archive",
            "key": SOURCE_KEY,
            "status": status,
            "tier": 1,
            "expected_hours": None,
            "timers": [],
            "since": "2026-09-01",
        }],
        "infrastructure_timers": {},
    }


def _source(*, last_done_at=None, first_observed_at="2026-10-01T00:00:00Z", empty=0):
    return {
        "store": "archive",
        "source": SOURCE_KEY,
        "OK": 0,
        "EMPTY": empty,
        "ERROR": 0,
        "DISABLED": 0,
        "attempts": empty,
        "last_ok_at": None,
        "last_done_at": last_done_at,
        "first_observed_at": first_observed_at,
        "history": [],
    }


def _report(at, source, status="enabled"):
    return {
        "generated_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "daily_job": {},
        "sources": [source],
        "registry": _registry(status),
        "units": {"failed": []},
        "disk": {"free_pct": 80, "free_gb": 100},
        "backup": {"latest": at.date().isoformat(), "manifest_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")},
    }


def _degradation(found):
    return {
        key: value for key, value in found.items()
        if "degrad" in key.lower()
        or "degrad" in value.get("kind", "").lower()
        or any(marker in value.get("text", "") for marker in ("建议停用", "降级", "未知"))
    }


def test_three_days_since_empty_completion_sends_one_recommendation_without_registry_mutation(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))
    completed = (AT - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = _report(AT, _source(last_done_at=completed, empty=1))
    registry_before = json.loads(json.dumps(report["registry"]))
    monkeypatch.setattr(M, "fetch_report", lambda: (report, None))
    monkeypatch.setattr(M, "now", lambda: AT)

    M.check()
    monkeypatch.setattr(M, "now", lambda: AT + timedelta(minutes=15))
    M.check()
    recommendations = [(subject, body) for subject, body in sent if "建议停用" in body]

    assert len(recommendations) == 1
    assert "[Catalyst] 告警：" in recommendations[0][0]
    assert report["registry"] == registry_before
    assert report["registry"]["sources"][0]["status"] == "enabled"


def test_empty_completion_resets_the_three_day_failure_clock():
    done = (AT - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = _report(AT, _source(last_done_at=done, empty=1))

    assert not _degradation(M.problems(report, None, AT))


def test_missing_first_observation_is_unknown_not_an_inferred_failure_age():
    report = _report(AT, _source(last_done_at=None, first_observed_at=None))

    unknown = _degradation(M.problems(report, None, AT))

    assert unknown
    assert any("未知" in value["text"] for value in unknown.values())
    assert not any("建议停用" in value["text"] for value in unknown.values())


@pytest.mark.parametrize("status", ["disabled", "planned"])
def test_non_active_sources_are_excluded_from_degradation(status):
    completed = (AT - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = _report(AT, _source(last_done_at=completed), status=status)

    assert not _degradation(M.problems(report, None, AT))


def test_seven_day_reminder_is_daily_and_deduplicated_within_utc_day(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "BACKUP", tmp_path / "backup")
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))
    completed = (AT - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = _report(AT, _source(last_done_at=completed))
    monkeypatch.setattr(M, "fetch_report", lambda: (report, None))
    monkeypatch.setattr(M, "now", lambda: AT)

    M.daily()
    M.daily()
    reminders_today = sum("持续失败满 7 天" in body for _, body in sent)

    assert reminders_today == 1

    next_day = AT + timedelta(days=1)
    report = _report(next_day, _source(last_done_at=completed))
    monkeypatch.setattr(M, "fetch_report", lambda: (report, None))
    monkeypatch.setattr(M, "now", lambda: next_day)
    M.daily()
    reminders_next_day = sum("持续失败满 7 天" in body for _, body in sent)

    assert reminders_next_day == 2


def test_recovery_is_notified_once_and_a_new_failure_period_restarts(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "BACKUP", tmp_path / "backup")
    monkeypatch.setattr(M, "send", lambda subject, body: sent.append((subject, body)))
    completed = (AT - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = _report(AT, _source(last_done_at=completed))
    monkeypatch.setattr(M, "fetch_report", lambda: (report, None))
    monkeypatch.setattr(M, "now", lambda: AT)
    M.check()

    recovered_at = AT + timedelta(minutes=15)
    recovered = _report(recovered_at, _source(
        last_done_at=recovered_at.strftime("%Y-%m-%dT%H:%M:%SZ"), empty=1))
    monkeypatch.setattr(M, "fetch_report", lambda: (recovered, None))
    monkeypatch.setattr(M, "now", lambda: recovered_at)
    M.check()
    M.check()
    recovery_mails = [body for subject, body in sent if subject == "[Catalyst] 已恢复"]

    assert len(recovery_mails) == 1
    assert any(SOURCE_ID in body for body in recovery_mails)

    restarted_at = recovered_at + timedelta(days=3)
    restarted = _report(restarted_at, _source(
        last_done_at=recovered_at.strftime("%Y-%m-%dT%H:%M:%SZ")))
    monkeypatch.setattr(M, "fetch_report", lambda: (restarted, None))
    monkeypatch.setattr(M, "now", lambda: restarted_at)
    found = M.problems(restarted, None, restarted_at)

    assert _degradation(found)
