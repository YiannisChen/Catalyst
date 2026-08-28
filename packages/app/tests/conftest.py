"""App test fixtures (M6 corrective).

The V1.1 router fail-closes when a server_env provider-specific env key is
missing, so app tests that POST server_env runs simulate a configured runtime
by default. Tests that exercise the fail-closed path explicitly delete the
key with monkeypatch.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _configured_provider_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a provider-specific env key for server_env test requests.

    ``load_env_files`` mirrors canonical keys to lowercase aliases
    (``openai_api_key``) via ``setdefault``; the fixture sets both so the
    canonical and alias views agree and later ``load_env_files`` calls cannot
    resurrect a stale value from a prior test.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-provider")
    monkeypatch.setenv("openai_api_key", "sk-test-openai-provider")
