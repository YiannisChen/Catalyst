from __future__ import annotations

import hashlib
import logging
import sqlite3

import pytest
from catalyst_data.ingestion import tavily_pool
from catalyst_data.ingestion.collectors import web_search


def test_keys_from_env_parses_precedence_quotes_and_deduplicates(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", " 'tvly-test-aaaa' , \"tvly-test-bbbb\",tvly-test-aaaa,, ")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-fallback")
    assert tavily_pool.keys_from_env() == ["tvly-test-aaaa", "tvly-test-bbbb"]
    monkeypatch.delenv("TAVILY_API_KEYS")
    assert tavily_pool.keys_from_env() == ["tvly-test-fallback"]


def test_fingerprint_uses_suffix_and_short_sha256():
    key = "tvly-test-aaaa"
    assert tavily_pool.fingerprint(key) == f"aaaa-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def test_pool_rotates_on_432_and_records_success(tmp_path):
    keys = ["tvly-test-aaaa", "tvly-test-bbbb"]
    pool = tavily_pool.TavilyKeyPool(keys, tmp_path / "web.sqlite3")
    calls = []

    class ProviderError(Exception):
        status_code = 432
        response = type("Response", (), {"status_code": 432, "text": "credit limit"})()

    def call(key):
        calls.append(key)
        if key == keys[0]:
            raise ProviderError()
        return {"credits_used": 3}

    assert pool.call(call) == {"credits_used": 3}
    assert calls == keys
    assert pool.status() == [
        {"fingerprint": tavily_pool.fingerprint(keys[0]), "status": "exhausted", "uses": 0, "credits": 0},
        {"fingerprint": tavily_pool.fingerprint(keys[1]), "status": "ok", "uses": 1, "credits": 3},
    ]


def test_pool_marks_401_invalid_and_skips_key(tmp_path):
    keys = ["tvly-test-aaaa", "tvly-test-bbbb"]
    pool = tavily_pool.TavilyKeyPool(keys, tmp_path / "web.sqlite3")

    class ProviderError(Exception):
        response = type("Response", (), {"status_code": 401, "text": "unauthorized"})()

    assert pool.call(lambda key: (_ for _ in ()).throw(ProviderError())
                     if key == keys[0] else {"results": []}) == {"results": []}
    assert pool.status()[0]["status"] == "invalid"


def test_usage_limited_429_rotates_to_next_key(tmp_path):
    keys = ["tvly-test-aaaa", "tvly-test-bbbb"]
    pool = tavily_pool.TavilyKeyPool(keys, tmp_path / "web.sqlite3")

    class ProviderError(Exception):
        response = type("Response", (), {
            "status_code": 429, "text": '{"error":"monthly usage limit reached"}'
        })()

    result = pool.call(lambda key: (_ for _ in ()).throw(ProviderError())
                       if key == keys[0] else {"results": []})
    assert result == {"results": []}
    assert pool.status()[0]["status"] == "exhausted"


def test_all_unusable_keys_raise_safe_error(tmp_path):
    keys = ["tvly-test-aaaa"]
    pool = tavily_pool.TavilyKeyPool(keys, tmp_path / "web.sqlite3")

    class ProviderError(Exception):
        response = type("Response", (), {"status_code": 403, "text": "denied"})()

    with pytest.raises(web_search.WebSearchError, match="tavily_keys_exhausted") as caught:
        pool.call(lambda _key: (_ for _ in ()).throw(ProviderError()))
    assert keys[0] not in str(caught.value)


def test_pool_month_change_resets_status_and_usage(tmp_path, monkeypatch):
    month = ["2026-10"]
    monkeypatch.setattr(tavily_pool, "_current_month", lambda: month[0])
    key = "tvly-test-aaaa"
    pool = tavily_pool.TavilyKeyPool([key], tmp_path / "web.sqlite3")
    pool.call(lambda _key: {"credits_used": 2})
    pool._mark(key, "exhausted")
    month[0] = "2026-11"
    assert pool.status() == [{"fingerprint": tavily_pool.fingerprint(key),
                              "status": "ok", "uses": 0, "credits": 0}]


def test_network_error_does_not_mark_key(tmp_path):
    key = "tvly-test-aaaa"
    pool = tavily_pool.TavilyKeyPool([key], tmp_path / "web.sqlite3")

    def network_failure(_key):
        raise web_search.WebSearchError("tavily_network_error")

    with pytest.raises(web_search.WebSearchError, match="tavily_network_error"):
        pool.call(network_failure)
    assert pool.status()[0]["status"] == "ok"
    assert pool.status()[0]["uses"] == 0


def test_keys_never_appear_in_logs_database_errors_or_status(tmp_path, caplog):
    key = "tvly-test-secretvalue"
    pool = tavily_pool.TavilyKeyPool([key], tmp_path / "web.sqlite3")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(web_search.WebSearchError, match="tavily_network_error") as caught:
            pool.call(lambda _key: (_ for _ in ()).throw(RuntimeError(key)))
    assert key not in caplog.text
    assert key not in str(caught.value)
    assert key not in repr(pool.status())
    conn = sqlite3.connect(tmp_path / "web.sqlite3")
    try:
        rows = conn.execute("SELECT * FROM tavily_key_state").fetchall()
        errors = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert key not in repr(rows)
    assert "tavily_key_state" in repr(errors)


def test_byok_header_uses_single_key_without_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(web_search, "_company_name", lambda _ticker: "Tesla, Inc.")
    monkeypatch.setenv("TAVILY_API_KEYS", "tvly-test-server1,tvly-test-server2")
    seen = []

    def search(_query, key, **_kwargs):
        seen.append(key)
        return {"results": []}

    web_search.search_leads("TSLA", "2026-04-23", api_key="tvly-test-byok", search_fn=search,
                            fetcher=object(), db_path=tmp_path / "web.sqlite3")
    assert seen == ["tvly-test-byok"] * 3
    conn = sqlite3.connect(tmp_path / "web.sqlite3")
    try:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='tavily_key_state'").fetchone()[0] == 0
    finally:
        conn.close()


def test_server_search_leads_rotates_configured_pool_keys(tmp_path, monkeypatch):
    keys = ["tvly-test-aaaa", "tvly-test-bbbb"]
    monkeypatch.setenv("TAVILY_API_KEYS", ",".join(keys))
    monkeypatch.setattr(web_search, "_company_name", lambda _ticker: "Tesla, Inc.")
    seen = []

    def search(_query, key, **_kwargs):
        seen.append(key)
        if key == keys[0]:
            raise web_search.WebSearchError("tavily_key_exhausted")
        return {"results": []}

    web_search.search_leads("TSLA", "2026-04-23", search_fn=search,
                            fetcher=object(), db_path=tmp_path / "web.sqlite3")
    assert seen == [keys[0], keys[1], keys[1], keys[1]]
    pool = tavily_pool.TavilyKeyPool(keys, tmp_path / "tavily_key_state.sqlite3")
    assert [row["status"] for row in pool.status()] == ["exhausted", "ok"]
