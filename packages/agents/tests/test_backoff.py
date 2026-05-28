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


def test_safe_sleep_raises_inside_event_loop():
    """Calling _safe_sleep from an async context must raise RuntimeError."""

    async def _inner():
        with pytest.raises(RuntimeError, match="running asyncio event loop"):
            _safe_sleep(0.01)

    asyncio.run(_inner())


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


def test_invoke_with_retries_parse_failure_appends_json_repair_hint(monkeypatch):
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)

    prompts: list[str] = []

    class _LLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, prompt: str):
            self.calls += 1
            prompts.append(prompt)

            class _Resp:
                pass

            r = _Resp()
            r.content = '{"ok": true}'
            return r

    llm = _LLM()
    parse_calls = 0

    def flaky_parse(text: str) -> dict:
        nonlocal parse_calls
        parse_calls += 1
        if parse_calls == 1:
            raise ValueError("invalid json payload")
        import json
        return json.loads(text)

    invoke_with_retries(llm, "BASE_PROMPT", parse_fn=flaky_parse, node_name="Critic")
    assert llm.calls == 2
    assert prompts[0] == "BASE_PROMPT"
    assert "Return ONLY the JSON object" in prompts[1]


def test_invoke_with_retries_connection_error_keeps_original_prompt(monkeypatch):
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)
    prompts: list[str] = []

    class _LLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, prompt: str):
            self.calls += 1
            prompts.append(prompt)
            raise ConnectionError("socket timeout")

    llm = _LLM()
    with pytest.raises(RuntimeError):
        invoke_with_retries(llm, "BASE_PROMPT", parse_fn=lambda _: {"ok": True}, node_name="Critic")

    assert all(p == "BASE_PROMPT" for p in prompts)


def test_invoke_with_retries_uses_retry_prompt_fn(monkeypatch):
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)
    prompts: list[str] = []

    class _LLM:
        def invoke(self, prompt: str):
            prompts.append(prompt)

            class _Resp:
                pass

            r = _Resp()
            r.content = '{"ok": true}'
            return r

    parse_calls = {"n": 0}

    def flaky_parse(text: str) -> dict:
        parse_calls["n"] += 1
        if parse_calls["n"] == 1:
            raise ValueError("invalid json")
        import json
        return json.loads(text)

    def retry_prompt_fn(base_prompt: str, attempt: int) -> str:
        return f"{base_prompt} // retry={attempt}"

    invoke_with_retries(
        _LLM(),
        "BASE_PROMPT",
        parse_fn=flaky_parse,
        node_name="Critic",
        retry_prompt_fn=retry_prompt_fn,
    )
    assert prompts == ["BASE_PROMPT", "BASE_PROMPT // retry=1"]


def test_invoke_with_retries_without_retry_prompt_fn_keeps_old_behavior(monkeypatch):
    monkeypatch.setattr("catalyst_agents.backoff._safe_sleep", lambda _: None)
    prompts: list[str] = []

    class _LLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, prompt: str):
            self.calls += 1
            prompts.append(prompt)

            class _Resp:
                pass

            r = _Resp()
            r.content = '{"ok": true}'
            return r

    parse_calls = {"n": 0}

    def flaky_parse(text: str) -> dict:
        parse_calls["n"] += 1
        if parse_calls["n"] == 1:
            raise ValueError("invalid json")
        import json
        return json.loads(text)

    invoke_with_retries(_LLM(), "BASE_PROMPT", parse_fn=flaky_parse, node_name="Critic")
    assert prompts[0] == "BASE_PROMPT"
    assert "Return ONLY the JSON object" in prompts[1]
