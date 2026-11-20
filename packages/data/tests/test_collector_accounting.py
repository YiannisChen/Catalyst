"""Collector accounting boundaries, synthetic SQLite and offline provider doubles."""
from contextlib import contextmanager

import pytest
from catalyst_data.ingestion import resilient
from catalyst_data.ingestion.collectors import alpaca


@pytest.fixture
def accounting(monkeypatch):
    events = []

    class Work:
        def expect_target(self, target):
            events.append(("expect", target))

        def acquire_target(self, target):
            events.append(("acquire", target))

        def discovery_complete(self, complete):
            events.append(("discovery", complete))

        def finish(self, status):
            events.append(("finish", status))

    @contextmanager
    def task(source_id, **kwargs):
        events.append(("task", source_id, kwargs.get("request_coverage")))
        yield Work()

    monkeypatch.setattr(resilient, "task", task, raising=False)
    return events


def article(content="<p>full body</p>"):
    return {"id": 42, "created_at": "2026-10-23T12:00:00Z", "updated_at": "2026-10-23T12:01:00Z",
            "headline": "synthetic", "content": content, "symbols": ["XYZ"]}


def test_alpaca_targets_expected_before_store_acquired_after_commit(tmp_path, monkeypatch, accounting):
    conn = alpaca.connect(tmp_path / "news.sqlite3")
    monkeypatch.setattr(alpaca, "news_pages", lambda *args, **kwargs: iter([[article()]]))
    store = alpaca.store_news

    def checked_store(conn, items, seen):
        assert any(e[0] == "expect" for e in accounting)
        assert not any(e[0] == "acquire" for e in accounting)
        return store(conn, items, seen)

    monkeypatch.setattr(alpaca, "store_news", checked_store)
    assert alpaca.poll(conn)["status"] == "OK"
    assert accounting[0][:2] == ("task", "alpaca_news:news:poll")
    assert [e[1] for e in accounting if e[0] == "expect"] == [e[1] for e in accounting if e[0] == "acquire"]
    assert any("42" in e[1] and "2026-10-23T12:01:00Z" in e[1] for e in accounting if e[0] == "expect")
    assert accounting[-1] == ("finish", "OK")
    assert not conn.in_transaction


def test_alpaca_empty_body_keeps_expected_target_unacquired(tmp_path, monkeypatch, accounting):
    conn = alpaca.connect(tmp_path / "news.sqlite3")
    monkeypatch.setattr(alpaca, "news_pages", lambda *args, **kwargs: iter([[article("<p> </p>")]]))
    alpaca.poll(conn)
    assert any(e[0] == "expect" for e in accounting)
    assert not any(e[0] == "acquire" for e in accounting)
    assert ("finish", "OK") in accounting


def test_alpaca_pagination_failure_marks_incomplete(tmp_path, monkeypatch, accounting):
    conn = alpaca.connect(tmp_path / "news.sqlite3")

    def pages(*args, **kwargs):
        yield [article()]
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(alpaca, "news_pages", pages)
    assert alpaca.poll(conn, log=lambda *_: None)["status"] == "ERROR"
    assert ("discovery", False) in accounting
    assert accounting[-1] == ("finish", "ERROR")
    assert not any(e[0] == "acquire" for e in accounting)


def test_alpaca_backfill_allocates_task_per_day(tmp_path, monkeypatch, accounting):
    conn = alpaca.connect(tmp_path / "news.sqlite3")
    monkeypatch.setattr(alpaca, "news_pages", lambda *args, **kwargs: iter([[]]))
    alpaca.backfill(conn, ["2026-10-21", "2026-10-22"], log=lambda *_: None)
    tasks = [e for e in accounting if e[0] == "task"]
    assert len(tasks) == 2
    assert all(e[1] == "alpaca_news:backfill" for e in tasks)
