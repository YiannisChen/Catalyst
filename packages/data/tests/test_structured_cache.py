from __future__ import annotations

from datetime import datetime, timezone

import pytest
from catalyst_data.ingestion.structured_cache import StructuredCache
from catalyst_data.ingestion.structured_http import StructuredClient
from catalyst_data.ingestion.structured_types import FactRow, FetchReceipt
from catalyst_data.storage.errors import StoreReadError


def _now():
    return datetime(2026, 10, 15, tzinfo=timezone.utc)


def test_missing_configured_store_raises_without_creating_file(tmp_path):
    path = tmp_path / "missing.sqlite"

    with pytest.raises(StoreReadError, match="structured facts store"):
        StructuredCache(path, offline=True)

    assert not path.exists()


def test_unreadable_store_raises_and_keeps_existing_bytes(tmp_path):
    path = tmp_path / "unreadable.sqlite"
    original = b"not a sqlite database"
    path.write_bytes(original)

    with pytest.raises(StoreReadError, match="structured facts schema"):
        StructuredCache(path, offline=True)

    assert path.read_bytes() == original


def test_present_store_reads_existing_rows_without_reinitializing(tmp_path):
    path = tmp_path / "cache.sqlite"
    cache = StructuredCache(path, create=True)
    row = FactRow("grade", "TGT", datetime(2026, 10, 14, tzinfo=timezone.utc), None, "DATE", "fmp", {})
    client = _client(cache, [], [(200, '{"rows": [1]}')])
    receipt = client.get("fmp", "x", {}, secret_param="apikey", secret="s")
    cache.put(receipt, (row,))
    cache.conn.close()

    reopened = StructuredCache(path, offline=True)
    assert reopened.rows("grade", "TGT") == (row,)
    reopened.conn.close()


def _client(cache, calls, bodies, sleeps=None):
    def http_get(url, params):
        calls.append((url, params))
        return bodies.pop(0)

    return StructuredClient(
        cache, http_get, sleep=sleeps.append if sleeps is not None else (lambda _seconds: None),
        clock=lambda: len(calls) * 10.0,
    )


def test_offline_miss_is_unavailable_without_http(tmp_path):
    calls = []
    client = _client(StructuredCache(tmp_path / "cache.sqlite", offline=True, create=True), calls, [])
    receipt = client.get("fred", "release/dates", {}, secret_param="api_key", secret="s")
    assert receipt.status == "UNAVAILABLE"
    assert calls == []
    assert client.cache.get(receipt.request_hash) is None


def test_first_ok_write_wins(tmp_path):
    calls = []
    client = _client(StructuredCache(tmp_path / "cache.sqlite", create=True), calls, [(200, '{"a": 1}')])
    receipt = client.get("finnhub", "x", {}, secret_param="token", secret="s")
    replacement = FetchReceipt(
        receipt.request_hash, receipt.provider, receipt.endpoint, receipt.params,
        "ERROR", receipt.fetched_at, receipt.response_json, "failed",
    )
    assert client.cache.put(replacement).status == "OK"
    assert client.cache.put(receipt).status == "OK"


def test_empty_and_error_are_distinct(tmp_path):
    cache = StructuredCache(tmp_path / "cache.sqlite", create=True)
    calls = []
    client = _client(cache, calls, [(200, "[]")])
    empty = client.get("finnhub", "x", {}, secret_param="token", secret="s")
    assert empty.status == "EMPTY"
    calls = []
    client = _client(cache, calls, [(500, ""), (500, ""), (500, "")], sleeps=[])
    error = client.get("finnhub", "y", {}, secret_param="token", secret="s")
    assert error.status == "ERROR"
    assert error.status != empty.status


def test_secret_is_not_stored(tmp_path):
    calls = []
    client = _client(StructuredCache(tmp_path / "cache.sqlite", create=True), calls, [(403, "")])
    receipt = client.get("finnhub", "x", {}, secret_param="token", secret="SUPERSECRET")
    assert receipt.params == {}
    assert "SUPERSECRET" not in receipt.error
    text = (tmp_path / "cache.sqlite").read_bytes() + (tmp_path / "cache.sqlite-wal").read_bytes()
    assert b"SUPERSECRET" not in text
    assert b"auth_or_plan:403" in text


def test_429_twice_then_200(tmp_path):
    calls = []
    sleeps = []
    client = _client(
        StructuredCache(tmp_path / "cache.sqlite", create=True), calls,
        [(429, ""), (429, ""), (200, '{"ok": 1}')], sleeps=sleeps,
    )
    receipt = client.get("fmp", "x", {}, secret_param="apikey", secret="s")
    assert receipt.status == "OK"
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_403_is_error_after_one_call(tmp_path):
    calls = []
    client = _client(StructuredCache(tmp_path / "cache.sqlite", create=True), calls, [(403, "")])
    receipt = client.get("fred", "x", {}, secret_param="api_key", secret="s")
    assert receipt.status == "ERROR"
    assert len(calls) == 1


def test_rows_returns_tz_aware_facts(tmp_path):
    cache = StructuredCache(tmp_path / "cache.sqlite", create=True)
    row = FactRow("grade", "TGT", datetime(2026, 10, 14, tzinfo=timezone.utc), None, "DATE", "fmp", {})
    client = _client(cache, [], [(200, '{"rows": [1]}')])
    receipt = client.get("fmp", "x", {}, secret_param="apikey", secret="s")
    cache.put(receipt, (row,))
    fetched = cache.rows("grade", "TGT")
    assert len(fetched) == 1
    assert fetched[0].event_at.tzinfo is not None
