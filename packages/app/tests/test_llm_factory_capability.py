"""M5-0: app llm_factory V1.1 capability adapter.

Final Migration TSD §15: build_v1_llm returns a client with max_retries=0
(provider-internal retries neutralized) and temperature=0.0, and exposes
capability metadata declaring structured output + true streaming. The legacy
build_llm (max_retries=2) must not be the V1.1 construction path.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from catalyst_agents.runtime.provider_capability import (
    require_capabilities,
    ProviderCapability,
)
from catalyst_app.llm_factory import build_llm, build_v1_llm

REQUIRED = ProviderCapability(
    supports_structured_output=True,
    supports_true_streaming=True,
    declares_token_accounting=True,
    normalizes_timeout_errors=True,
    capability_revision="v1.1-test",
)


def test_build_v1_llm_neutralizes_provider_internal_retries_and_temperature() -> None:
    with patch("catalyst_app.llm_factory.ChatOpenAI") as chat_openai:
        build_v1_llm(
            "provider-specific-model",
            provider="custom_openai_compatible",
            api_key="test-key",
            base_url="https://example.invalid/v1",
        )
    kwargs = chat_openai.call_args.kwargs
    assert kwargs["max_retries"] == 0
    assert kwargs["temperature"] == 0.0


def test_build_v1_llm_client_passes_capability_admission() -> None:
    client = build_v1_llm(
        "provider-specific-model",
        provider="custom_openai_compatible",
        api_key="sk-test-key-123",
        base_url="https://example.invalid/v1",
    )
    capability = require_capabilities(client, REQUIRED)
    assert capability.supports_structured_output is True
    assert capability.supports_true_streaming is True
    assert capability.declares_token_accounting is True
    assert capability.normalizes_timeout_errors is True
    assert capability.capability_revision


def test_legacy_build_llm_still_uses_provider_internal_retries() -> None:
    with patch("catalyst_app.llm_factory.ChatOpenAI") as chat_openai:
        build_llm(
            "provider-specific-model",
            provider="custom_openai_compatible",
            api_key="test-key",
            base_url="https://example.invalid/v1",
        )
    assert chat_openai.call_args.kwargs["max_retries"] == 2


def test_v1_path_does_not_reuse_legacy_build_llm_construction() -> None:
    """The V1.1 path constructs its own client with max_retries=0; the legacy
    build_llm (max_retries=2) is not used for the V1.1 path."""
    with patch("catalyst_app.llm_factory.ChatOpenAI") as chat_openai:
        build_v1_llm(
            "provider-specific-model",
            provider="custom_openai_compatible",
            api_key="test-key",
            base_url="https://example.invalid/v1",
        )
        build_llm(
            "provider-specific-model",
            provider="custom_openai_compatible",
            api_key="test-key",
            base_url="https://example.invalid/v1",
        )
    retries_by_call = [
        call.kwargs["max_retries"] for call in chat_openai.call_args_list
    ]
    assert retries_by_call == [0, 2]


@pytest.mark.parametrize(
    "model, expected",
    [
        (
            {
                "model_id": "provider-specific-model",
                "provider": "custom_openai_compatible",
                "base_url": "https://example.invalid/v1",
            },
            {
                "model_id": "provider-specific-model",
                "provider": "custom_openai_compatible",
                "base_url": "https://example.invalid/v1",
            },
        ),
        ("provider-specific-model", {"model_id": "provider-specific-model"}),
    ],
)
def test_production_graph_factory_uses_admitted_v1_client(
    monkeypatch, model, expected
) -> None:
    import catalyst_app.dependencies as app_dependencies

    admitted_client = object()
    captured: dict[str, object] = {}

    class _Loader:
        def get_dependencies(self):
            return SimpleNamespace(
                retriever="retriever",
                requested_manifest_id="manifest-id",
            )

    def _build_v1_llm(model_id=None, **kwargs):
        captured["factory"] = {"model_id": model_id, **kwargs}
        return admitted_client

    def _adapter(**kwargs):
        captured["adapter"] = kwargs
        return kwargs

    assert "build_llm" not in app_dependencies.__dict__
    monkeypatch.setattr(
        app_dependencies, "get_runtime_dependency_loader", lambda: _Loader()
    )
    monkeypatch.setattr(
        app_dependencies, "build_v1_llm", _build_v1_llm, raising=False
    )
    monkeypatch.setattr(app_dependencies, "build_v1_graph_adapter", _adapter)

    graph = app_dependencies._graph_factory(model, api_key="runtime-key")

    assert captured["factory"] == {**expected, "api_key": "runtime-key"}
    assert graph["model"] is admitted_client
    assert graph["retriever"] == "retriever"
    assert graph["requested_manifest_id"] == "manifest-id"
