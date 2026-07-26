"""Tests for provider_validator — mocked network, no real provider calls."""

import sys
sys.path.insert(0, 'packages/agents')
sys.path.insert(0, 'packages/data-core')
sys.path.insert(0, 'packages/app')

import pytest
from unittest.mock import patch, MagicMock

from catalyst_app.provider_validator import validate_provider, ProviderValidationResult


API_KEY = "sk-test-secret-key-123"


def test_unknown_provider_without_base_url():
    result = validate_provider(
        provider="nonexistent_provider",
        model_id="some-model",
        api_key=API_KEY,
        base_url=None,
    )
    assert result.ok is False
    assert result.status == "invalid"
    assert "custom_openai_compatible" in result.message.lower()
    assert API_KEY not in result.message


def test_custom_openai_compatible_with_base_url():
    """custom_openai_compatible requires base_url — tested here for invalid without."""
    result = validate_provider(
        provider="custom_openai_compatible",
        model_id="some-model",
        api_key=API_KEY,
        base_url=None,
    )
    assert result.ok is False
    assert result.status == "invalid"


@patch("catalyst_app.provider_validator.ChatOpenAI")
def test_auth_failure_returns_auth_failed(mock_chat):
    mock_instance = MagicMock()
    mock_instance.invoke.side_effect = Exception("401 Unauthorized: invalid api_key")
    mock_chat.return_value = mock_instance

    result = validate_provider(
        provider="openai",
        model_id="gpt-4o",
        api_key=API_KEY,
    )
    assert result.ok is False
    assert result.status == "auth_failed"
    assert API_KEY not in result.message
    assert "Check your API key" in result.message


@patch("catalyst_app.provider_validator.ChatOpenAI")
def test_success_returns_ready(mock_chat):
    mock_instance = MagicMock()
    mock_instance.invoke.return_value = MagicMock()
    mock_chat.return_value = mock_instance

    result = validate_provider(
        provider="openai",
        model_id="gpt-4o",
        api_key=API_KEY,
    )
    assert result.ok is True
    assert result.status == "ready"
    assert result.latency_ms is not None
    assert result.latency_ms >= 0
    assert result.message is None


@patch("catalyst_app.provider_validator.ChatOpenAI")
def test_timeout_returns_timeout(mock_chat):
    mock_instance = MagicMock()
    mock_instance.invoke.side_effect = Exception("Request timed out after 15 seconds")
    mock_chat.return_value = mock_instance

    result = validate_provider(
        provider="openai",
        model_id="gpt-4o",
        api_key=API_KEY,
    )
    assert result.ok is False
    assert result.status == "timeout"
    assert API_KEY not in result.message


@patch("catalyst_app.provider_validator.ChatOpenAI")
def test_connection_refused_returns_request_failed(mock_chat):
    mock_instance = MagicMock()
    mock_instance.invoke.side_effect = Exception("Connection refused")
    mock_chat.return_value = mock_instance

    result = validate_provider(
        provider="openai",
        model_id="gpt-4o",
        api_key=API_KEY,
    )
    assert result.ok is False
    assert result.status == "request_failed"
    assert API_KEY not in result.message


@patch("catalyst_app.provider_validator.ChatOpenAI")
def test_response_never_leaks_key(mock_chat):
    """All status messages must never contain the API key."""
    mock_instance = MagicMock()
    mock_instance.invoke.side_effect = Exception("401 Unauthorized: invalid api_key")
    mock_chat.return_value = mock_instance

    result = validate_provider(
        provider="openai",
        model_id="gpt-4o",
        api_key=API_KEY,
    )
    for attr in ["message", "status"]:
        val = getattr(result, attr, "")
        if val:
            assert API_KEY not in str(val)
    assert "secret" not in (result.message or "").lower()


@patch("catalyst_app.provider_validator.ChatOpenAI")
def test_generic_error_returns_request_failed(mock_chat):
    mock_instance = MagicMock()
    mock_instance.invoke.side_effect = Exception("Something unexpected happened")
    mock_chat.return_value = mock_instance

    result = validate_provider(
        provider="openai",
        model_id="gpt-4o",
        api_key=API_KEY,
    )
    assert result.ok is False
    assert result.status == "request_failed"


def test_deepseek_provider_resolves():
    from catalyst_app.llm_factory import _PROVIDER_DEFAULTS
    assert "deepseek" in _PROVIDER_DEFAULTS


def test_siliconflow_provider_resolves():
    from catalyst_app.llm_factory import _PROVIDER_DEFAULTS
    assert "siliconflow" in _PROVIDER_DEFAULTS
