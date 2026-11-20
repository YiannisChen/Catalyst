from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from catalyst_app.routers import admin_notify
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def notify(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_QUOTA_HASH_KEY", "salt")
    for name, value in {"CATALYST_SMTP_HOST": "smtp.example", "CATALYST_SMTP_USER": "bot@example.com",
                        "CATALYST_SMTP_PASSWORD": "pw", "CATALYST_ADMIN_NOTIFY_EMAIL": "owner@example.com"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(admin_notify, "runtime_db_path", lambda: tmp_path / "runtime.sqlite3")
    monkeypatch.setattr(admin_notify, "_GLOBAL_CLOSED_UNTIL", [None])
    sent = []
    monkeypatch.setattr(admin_notify, "_send", lambda config, subject, body: sent.append((subject, body)))

    class _Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(admin_notify.threading, "Thread", _Thread)
    clock = {"now": datetime(2026, 11, 18, 12, tzinfo=timezone.utc)}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(admin_notify, "datetime", _Clock)
    app = FastAPI()
    app.include_router(admin_notify.router)
    return TestClient(app), sent, clock


def _ask(client, ip, reason="data_stale"):
    return client.post("/api/data/notify-admin", json={"reason": reason}, headers={"x-forwarded-for": ip})


def test_one_request_per_caller_a_day_three_a_day_two_hours_apart(notify, monkeypatch):
    client, sent, clock = notify
    callers = iter(["a", "b", "c", "d", "e"])
    current = {"ip": "a"}
    monkeypatch.setattr(admin_notify, "_caller_key", lambda request: current["ip"])

    assert client.post("/api/data/notify-admin", json={"reason": "data_stale"}).status_code == 202
    assert client.post("/api/data/notify-admin", json={"reason": "data_stale"}).status_code == 429  # same caller
    current["ip"] = next(callers) + "2"
    refused = client.post("/api/data/notify-admin", json={"reason": "other"})
    assert refused.status_code == 429  # under 2 h since the last email
    assert refused.json() == {"detail": "notify_rate_limited", "next_allowed_at": "2026-11-18T14:00:00Z"}
    for hours in (2, 4):
        clock["now"] += timedelta(hours=2)
        current["ip"] = f"caller-{hours}"
        assert client.post("/api/data/notify-admin", json={"reason": "update_failed"}).status_code == 202
    clock["now"] += timedelta(hours=3)
    current["ip"] = "caller-late"
    assert client.post("/api/data/notify-admin", json={"reason": "other"}).status_code == 429  # 3 sent today
    assert len(sent) == 3
    status = client.get("/api/data/notify-admin").json()
    assert status["available"] is True and status["next_allowed_at"] is not None


def test_unconfigured_mail_is_unavailable(notify, monkeypatch):
    client, sent, _ = notify
    monkeypatch.delenv("CATALYST_SMTP_HOST")
    assert client.get("/api/data/notify-admin").json() == {"available": False, "next_allowed_at": None}
    assert client.post("/api/data/notify-admin", json={"reason": "other"}).status_code == 503
    assert sent == []


def test_email_carries_only_server_facts(notify, monkeypatch):
    client, sent, _ = notify
    monkeypatch.setattr(admin_notify, "_caller_key", lambda request: "x")
    assert client.post("/api/data/notify-admin", json={"reason": "other", "note": "spam"}).status_code == 422
    assert client.post("/api/data/notify-admin", json={"reason": "other"}).status_code == 202
    subject, body = sent[0]
    assert "visitor asked" in subject and "spam" not in body and "Time (UTC): 2026-11-18T12:00:00Z" in body


def test_a_failed_email_gives_the_global_slot_back(notify, monkeypatch):
    client, sent, clock = notify
    current = {"ip": "a"}
    monkeypatch.setattr(admin_notify, "_caller_key", lambda request: current["ip"])

    def boom(config, subject, body):
        raise OSError("smtp down")

    monkeypatch.setattr(admin_notify, "_send", boom)
    assert client.post("/api/data/notify-admin", json={"reason": "other"}).status_code == 202
    monkeypatch.setattr(admin_notify, "_send", lambda config, subject, body: sent.append(body))
    current["ip"] = "b"
    assert client.post("/api/data/notify-admin", json={"reason": "other"}).status_code == 202  # no 2 h wait
    current["ip"] = "a"
    assert client.post("/api/data/notify-admin", json={"reason": "other"}).status_code == 429  # a's own slot used
