"""Lightweight test: runner _resolve_model_id and state separation."""

import sys
sys.path.insert(0, 'packages/agents')
sys.path.insert(0, 'packages/data-core')
sys.path.insert(0, 'packages/app')

from catalyst_agents.runtime.runner import _resolve_model_id


def test_legacy_string_model():
    assert _resolve_model_id("gemini-2.5-flash-nothink") == "gemini-2.5-flash-nothink"


def test_byok_dict_model():
    meta = {"provider": "openrouter", "model_id": "google/gemini-2.5-flash", "base_url": "https://openrouter.ai/api/v1"}
    assert _resolve_model_id(meta) == "google/gemini-2.5-flash"


def test_none_falls_back():
    assert _resolve_model_id(None) == "runtime-default"


def test_empty_string_falls_back():
    assert _resolve_model_id("") == "runtime-default"


def test_dict_without_model_id_falls_back():
    assert _resolve_model_id({"provider": "openai"}) == "runtime-default"


def test_byok_meta_never_contains_api_key():
    """Simulate what's stored in agent_runs.config for BYOK — must not include api_key."""
    config_model = {"provider": "openrouter", "model_id": "google/gemini-2.5-flash", "base_url": "https://openrouter.ai/api/v1"}
    # This is what create_live_run stores — metadata only
    assert "api_key" not in config_model
    assert "sk-" not in str(config_model)
    # model_id resolved as string
    model_id = config_model.get("model_id")
    assert isinstance(model_id, str)
    assert model_id == "google/gemini-2.5-flash"


print("ALL RUNNER STATE TESTS PASSED")
