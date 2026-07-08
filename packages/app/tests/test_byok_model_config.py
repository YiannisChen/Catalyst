"""Tests for BYOK ModelConfig schemas including credential_source."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "data-core"))

import pytest
from pydantic import ValidationError

from catalyst_app.schemas import (
    CreateRunRequest,
    CredentialSource,
    ModelConfig,
    ModelValidateRequest,
    ModelValidateResponse,
)


class TestModelConfig:
    def test_rejects_browser_key_without_api_key(self):
        """browser_key credential_source requires non-empty api_key."""
        with pytest.raises(ValidationError):
            ModelConfig(
                provider="openai", model_id="gpt-4o", api_key="",
                credential_source=CredentialSource.BROWSER_KEY,
            )

    def test_accepts_server_env_with_empty_api_key(self):
        """server_env allows empty api_key."""
        mc = ModelConfig(
            provider="aihubmix", model_id="gemini-2.5-flash-nothink", api_key="",
            credential_source=CredentialSource.SERVER_ENV,
        )
        assert mc.provider == "aihubmix"
        assert mc.api_key == ""
        assert mc.credential_source == CredentialSource.SERVER_ENV

    def test_accepts_minimal_with_api_key(self):
        mc = ModelConfig(provider="openai", model_id="gpt-4o", api_key="sk-test")
        assert mc.provider == "openai"
        assert mc.model_id == "gpt-4o"
        assert mc.credential_source == CredentialSource.BROWSER_KEY  # default

    def test_with_base_url(self):
        mc = ModelConfig(
            provider="custom_openai_compatible",
            model_id="local-model",
            api_key="sk-test",
            base_url="https://my-proxy.local/v1",
        )
        assert mc.base_url == "https://my-proxy.local/v1"

    def test_default_credential_source(self):
        mc = ModelConfig(provider="deepseek", model_id="deepseek-v4-flash", api_key="sk-test")
        assert mc.credential_source == CredentialSource.BROWSER_KEY


class TestModelValidateRequest:
    def test_server_env_allows_empty_api_key(self):
        req = ModelValidateRequest(
            provider="aihubmix", model_id="gemini-2.5-flash-nothink", api_key="",
            credential_source=CredentialSource.SERVER_ENV,
        )
        assert req.api_key == ""
        assert req.credential_source == CredentialSource.SERVER_ENV

    def test_browser_key_with_api_key(self):
        req = ModelValidateRequest(
            provider="openai", model_id="gpt-4o", api_key="sk-test",
            credential_source=CredentialSource.BROWSER_KEY,
        )
        assert req.api_key == "sk-test"

    def test_credential_source_is_enum(self):
        req = ModelValidateRequest(
            provider="openai", model_id="gpt-4o", api_key="sk-test",
            credential_source=CredentialSource.BROWSER_KEY,
        )
        assert isinstance(req.credential_source, CredentialSource)


class TestCreateRunRequest:
    def test_requires_model_or_model_id(self):
        with pytest.raises(ValidationError):
            CreateRunRequest(ticker="AAPL", trade_date="2025-09-08")

    def test_accepts_model_config_with_server_env(self):
        req = CreateRunRequest(
            ticker="AAPL",
            trade_date="2025-09-08",
            model=ModelConfig(
                provider="deepseek", model_id="deepseek-v4-flash", api_key="",
                credential_source=CredentialSource.SERVER_ENV,
            ),
        )
        assert req.model.provider == "deepseek"
        assert req.model.credential_source == CredentialSource.SERVER_ENV

    def test_accepts_model_config_with_browser_key(self):
        req = CreateRunRequest(
            ticker="AAPL",
            trade_date="2025-09-08",
            model=ModelConfig(
                provider="openai", model_id="gpt-4o-mini", api_key="sk-test",
            ),
        )
        assert req.model.provider == "openai"

    def test_accepts_legacy_model_id(self):
        req = CreateRunRequest(
            ticker="AAPL",
            trade_date="2025-09-08",
            model_id="gemini-2.5-flash-nothink",
        )
        assert req.model_id == "gemini-2.5-flash-nothink"
        assert req.model is None


class TestModelValidateResponse:
    def test_roundtrip(self):
        resp = ModelValidateResponse(
            ok=True, provider="openai", model_id="gpt-4o",
            status="ready", latency_ms=450,
        )
        assert resp.ok is True
        assert resp.status == "ready"
        assert resp.latency_ms == 450
