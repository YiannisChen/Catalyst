"""Tests for catalyst_agents.backoff — shared retry + event-loop safety."""
import asyncio

import pytest

from catalyst_agents.backoff import (
    _safe_sleep,
    invoke_with_retries,
    MAX_RETRIES,
    BASE_BACKOFF_SECONDS,
)


# ---------------------------------------------------------------------------
# _safe_sleep
# ---------------------------------------------------------------------------

def test_safe_sleep_blocks_outside_event_loop():
    """Normal (non-async) context — _safe_sleep should not raise."""
    _safe_sleep(0)  # zero-duration sleep, just assert no exception


@pytest.mark.asyncio
async def test_safe_sleep_raises_inside_event_loop():
    """Calling _safe_sleep from an async context must raise RuntimeError."""
    with pytest.raises(RuntimeError, match="running asyncio event loop"):
        _safe_sleep(0.01)


# ---------------------------------------------------------------------------
# invoke_with_retries
# ---------------------------------------------------------------------------

class _MockLLM:
    """LLM stub that fails *failures* times then returns *content*."""

    def __init__(self, content: str = '{"ok": true}', failures: int = 0):
        self.content = content
        self.failures = failures
        self.calls = 0

    def invoke(self, prompt: str):
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError(f"attempt {self.calls}")

        class _Resp:
            pass

        r = _Resp()
        r.content = self.content
        return r


def _identity_parse(text: str) -> dict:
    import json
    return json.loads(text)


def test_invoke_with_retries_succeeds_first_try(monkeypatch):
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)
    llm = _MockLLM()
    resp, parsed = invoke_with_retries(llm, "p", parse_fn=_identity_parse)
    assert parsed == {"ok": True}
    assert llm.calls == 1


def test_invoke_with_retries_retries_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", sleeps.append)
    llm = _MockLLM(failures=2)
    resp, parsed = invoke_with_retries(llm, "p", parse_fn=_identity_parse, node_name="Test")
    assert parsed == {"ok": True}
    assert llm.calls == 3
    assert sleeps == [BASE_BACKOFF_SECONDS * 1, BASE_BACKOFF_SECONDS * 2]


def test_invoke_with_retries_exhausted_raises(monkeypatch):
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)
    llm = _MockLLM(failures=MAX_RETRIES)
    with pytest.raises(RuntimeError, match=f"failed after {MAX_RETRIES} attempts"):
        invoke_with_retries(llm, "p", parse_fn=_identity_parse, node_name="Test")
    assert llm.calls == MAX_RETRIES


def test_invoke_with_retries_parse_failure_retries(monkeypatch):
    """Bad parse output triggers retry, not immediate return."""
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)

    call_count = 0

    def flaky_parse(text: str) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ValueError("bad json")
        return {"ok": True}

    llm = _MockLLM()
    resp, parsed = invoke_with_retries(llm, "p", parse_fn=flaky_parse)
    assert parsed == {"ok": True}
    assert llm.calls == 3  # invoked 3 times (first 2 parse fails, 3rd succeeds)
