from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from catalyst_data.ingestion import tavily_ledger
from catalyst_data.ingestion.collectors import web_search
from catalyst_data.ingestion.tavily_ledger import TavilyCallContext

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def _context(tmp_path, *, tier="anonymous", limit=50, enabled=True):
    return TavilyCallContext(
        db_path=tmp_path / "runtime.sqlite3",
        tier=tier,
        daily_limit=limit,
        public_enabled=enabled,
    )


def _used(tmp_path, day=None):
    day = day or NOW.date().isoformat()
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as conn:
        return tavily_ledger.used_for_day(conn, day)


def test_guard_reserves_settles_success_and_releases_failure(tmp_path):
    ctx = _context(tmp_path)

    with tavily_ledger.guard(ctx, now=NOW):
        pass
    assert _used(tmp_path) == 1

    with pytest.raises(RuntimeError, match="network"):
        with tavily_ledger.guard(ctx, now=NOW):
            raise RuntimeError("network")
    assert _used(tmp_path) == 1


def test_public_switch_blocks_before_the_call_while_admin_ignores_it(tmp_path):
    calls: list[str] = []
    public = _context(tmp_path, tier="anonymous", enabled=False)
    with pytest.raises(web_search.WebSearchError, match="web_search_public_disabled"):
        with tavily_ledger.guard(public, now=NOW):
            calls.append("public")
    assert calls == [] and _used(tmp_path) == 0

    admin = _context(tmp_path, tier="admin", enabled=False)
    with tavily_ledger.guard(admin, now=NOW):
        calls.append("admin")
    assert calls == ["admin"] and _used(tmp_path) == 1


def test_two_reservations_at_the_limit_do_not_overshoot(tmp_path):
    ctx = _context(tmp_path, limit=1)
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()
        try:
            reservation = tavily_ledger.reserve(ctx, now=NOW)
        except web_search.WebSearchError as exc:
            return exc.code
        tavily_ledger.settle(reservation, now=NOW)
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: attempt(), range(2)))

    assert sorted(results) == ["ok", "web_search_budget_exhausted"]
    assert _used(tmp_path) == 1


def test_counts_are_scoped_to_the_utc_day(tmp_path):
    ctx = _context(tmp_path)
    reservation = tavily_ledger.reserve(ctx, now=NOW)
    tavily_ledger.settle(reservation, now=NOW)
    today = NOW.date().isoformat()
    next_day = (NOW + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = next_day.date().isoformat()
    assert _used(tmp_path, today) == 1
    assert _used(tmp_path, tomorrow) == 0
    with tavily_ledger.guard(ctx, now=next_day):
        pass
    assert _used(tmp_path, tomorrow) == 1


def test_search_leads_counts_api_calls_but_not_cache_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(web_search, "_company_name", lambda _ticker: "Tesla, Inc.")
    ctx = _context(tmp_path, limit=10)
    calls: list[str] = []

    def search(query, key, *, start, end):
        calls.append(query)
        return {"results": []}

    kwargs = dict(
        server_key="server-key", search_fn=search, fetcher=object(),
        db_path=tmp_path / "web.sqlite3", tavily_context=ctx, now=NOW,
    )
    first = web_search.search_leads("TSLA", "2026-04-23", **kwargs)
    second = web_search.search_leads("TSLA", "2026-04-23", **kwargs)

    assert first["cached"] is False and len(calls) == 3
    assert second["cached"] is True and second["credits_used"] == 0
    assert _used(tmp_path) == 3


def test_limit_reached_returns_empty_reason_without_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(web_search, "_company_name", lambda _ticker: "Tesla, Inc.")
    ctx = _context(tmp_path, limit=1)
    calls: list[str] = []

    def search(query, key, *, start, end):
        calls.append(query)
        return {"results": []}

    result = web_search.search_leads(
        "TSLA", "2026-04-23", server_key="server-key", search_fn=search,
        fetcher=object(), db_path=tmp_path / "web.sqlite3", tavily_context=ctx, now=NOW,
    )

    assert result["leads"] == []
    assert result["reason"] == "web_search_budget_exhausted"
    assert len(calls) == 1 and _used(tmp_path) == 1


def test_public_switch_off_returns_reason_without_a_call(tmp_path, monkeypatch):
    monkeypatch.setattr(web_search, "_company_name", lambda _ticker: "Tesla, Inc.")
    ctx = _context(tmp_path, enabled=False)
    calls: list[str] = []

    def search(query, key, *, start, end):
        calls.append(query)
        return {"results": []}

    result = web_search.search_leads(
        "TSLA", "2026-04-23", server_key="server-key", search_fn=search,
        fetcher=object(), db_path=tmp_path / "web.sqlite3", tavily_context=ctx, now=NOW,
    )

    assert result["leads"] == []
    assert result["reason"] == "web_search_public_disabled"
    assert calls == [] and _used(tmp_path) == 0


def test_admin_override_counts_without_enforcing_the_public_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(web_search, "_company_name", lambda _ticker: "Tesla, Inc.")
    ctx = _context(tmp_path, tier="admin", limit=1, enabled=False)
    calls: list[str] = []

    def search(query, key, *, start, end):
        calls.append(query)
        return {"results": []}

    result = web_search.search_leads(
        "TSLA", "2026-04-23", server_key="server-key", search_fn=search,
        fetcher=object(), db_path=tmp_path / "web.sqlite3", tavily_context=ctx, now=NOW,
    )

    assert "reason" not in result and len(calls) == 3
    assert _used(tmp_path) == 3
