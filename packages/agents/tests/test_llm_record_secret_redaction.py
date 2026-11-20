from __future__ import annotations

import json

import pytest
from catalyst_agents.llm import record

TEST_API_KEY = "byok-test-secret-9f3a6e2d1c7b"


class FailingClient:
    model_name = "fixture-model"

    def __init__(self) -> None:
        self._api_key = TEST_API_KEY

    def bind(self, **_kwargs):
        return self

    def invoke(self, _messages, **_options):
        raise RuntimeError(f"authorization failed for {self._api_key}")


def test_recorded_provider_error_redacts_client_key(tmp_path, monkeypatch) -> None:
    record_path = tmp_path / "llm-record.jsonl"
    monkeypatch.setenv("CATALYST_LLM_RECORD", str(record_path))
    record._STORES.clear()

    with pytest.raises(RuntimeError):
        record.wrap(FailingClient()).invoke("safe prompt", api_key=TEST_API_KEY)

    stored = record_path.read_text(encoding="utf-8")
    assert TEST_API_KEY not in stored
    row = json.loads(stored)
    assert row["request"]["options"]["api_key"] == "[REDACTED_SECRET]"
    assert row["error"]["type"] == "RuntimeError"
    assert row["error"]["message"] == "authorization failed for [REDACTED_SECRET]"


@pytest.mark.parametrize("status", [401, 402, 403])
def test_account_error_is_not_recorded_and_existing_row_is_replay_miss(tmp_path, monkeypatch, status):
    class Response:
        status_code = status

    class APIStatusError(Exception):
        response = Response()

    class AccountClient(FailingClient):
        def invoke(self, _messages, **_options):
            raise APIStatusError("account error")

    record_path = tmp_path / "account.jsonl"
    monkeypatch.setenv("CATALYST_LLM_RECORD", str(record_path))
    record._STORES.clear()
    with pytest.raises(APIStatusError):
        record.wrap(AccountClient()).invoke("safe prompt")
    assert not record_path.exists()

    key = record.request_key("fixture-model", {}, {}, "safe prompt")
    row = {"key": key, "n": 0, "error": {"type": "APIStatusError", "message": "account",
                                              "status_code": status}}
    replay_path = tmp_path / "replay.jsonl"
    replay_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    store = record.Store(str(replay_path), "replay")
    with pytest.raises(record.ReplayMiss, match="account-error"):
        record.Recorded(AccountClient(), store).invoke("safe prompt")


def test_real_provider_402_recording_rows_replay_as_misses():
    fixture = __import__("pathlib").Path(__file__).parents[3] / "scripts/fixtures/provider_402_recording_rows.jsonl"
    rows = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3
    class ReplayClient:
        model_name = rows[0]["request"]["model"]
        def bind(self, **_kwargs):
            return self
        def invoke(self, *_args, **_kwargs):
            raise AssertionError("account-error recording must not dispatch or return")
    store = record.Store(str(fixture), "replay")
    for row in rows:
        request = row["request"]
        with pytest.raises(record.ReplayMiss, match="account-error"):
            record.Recorded(ReplayClient(), store, bound=request.get("bound", {})).invoke(
                request["messages"], **request.get("options", {})
            )


def test_normal_event_dater_rows_with_token_usage_401_402_403_are_not_account_errors():
    from pathlib import Path
    fixture = Path(__file__).parents[3] / "scripts/fixtures/normal_event_dater_replies.jsonl"
    rows = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4
    assert all(record.account_error_status(row.get("error"), row=row) is None for row in rows)
