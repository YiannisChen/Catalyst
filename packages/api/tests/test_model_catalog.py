"""Lightweight tests for model_catalog.py — no network, no LanceDB, no models."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "data"))

from catalyst_app.model_catalog import build_catalog
from fastapi import FastAPI
from fastapi.testclient import TestClient


class TestCatalogProviders:
    def setup_method(self):
        self.catalog = build_catalog()
        self.provider_ids = {p.id for p in self.catalog.providers}

    def test_b7_models_publish_capabilities_and_quality_pointer(self):
        models = {
            (model.provider, model.model_id): model
            for provider in self.catalog.providers
            for model in provider.models
        }
        for provider in ("openai", "anthropic", "xai", "deepseek", "qwen", "gemini"):
            assert any(model_provider == provider for model_provider, _ in models)
        required = {
            "provider", "model_id", "label", "tool_calling", "json_mode",
            "streaming_tool_args", "native_web_search", "supported", "evaluated", "eval_ref",
        }
        for model in models.values():
            assert required <= set(model.model_dump())
            assert model.native_web_search in {"none", "undated"}
            if model.supported:
                assert model.tool_calling and model.json_mode
        evaluated = [model for model in models.values() if model.evaluated]
        assert [(model.provider, model.model_id) for model in evaluated] == [
            ("deepseek", "deepseek-flash")
        ]
        assert evaluated[0].eval_ref == "/api/quality"

    def test_anthropic_is_explicitly_unsupported_without_native_adapter(self):
        anthropic = [
            model for provider in self.catalog.providers if provider.id == "anthropic"
            for model in provider.models
        ]
        assert anthropic
        assert all(not model.supported for model in anthropic)
        assert all(model.support_reason for model in anthropic)

    def test_search_provider_declares_tavily_cutoff_contract(self):
        assert [provider.model_dump() for provider in self.catalog.search_providers] == [
            {"id": "tavily", "date_filter": True, "evaluated_for_attribution": False, "role": "leads"}
        ]

    def test_no_openrouter(self):
        assert "openrouter" not in self.provider_ids

    def test_no_dashscope(self):
        assert "dashscope" not in self.provider_ids

    def test_each_provider_has_label(self):
        for p in self.catalog.providers:
            assert p.label, f"Provider {p.id} missing label"

    def test_custom_has_env_key_false(self):
        custom = next(p for p in self.catalog.providers if p.id == "custom_openai_compatible")
        assert custom.env_key_configured is False
        assert custom.default_model_id is None

    def test_deepseek_has_models(self):
        ds = next(p for p in self.catalog.providers if p.id == "deepseek")
        assert len(ds.models) >= 1
        model_ids = {m.id for m in ds.models}
        assert "deepseek-flash" in model_ids

    def test_catalog_endpoint_serializes_b7_contract(self):
        from catalyst_app.routers.live_runs import router

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            payload = client.get("/api/models/catalog").json()
        assert {"providers", "search_providers"} == set(payload)
        model = next(
            model for provider in payload["providers"] if provider["id"] == "deepseek"
            for model in provider["models"] if model["model_id"] == "deepseek-flash"
        )
        assert model["eval_ref"] == "/api/quality"
        assert model["evaluated"] is True
        assert model["provider"] == "deepseek"
        assert payload["search_providers"] == [
            {"id": "tavily", "date_filter": True, "evaluated_for_attribution": False, "role": "leads"}
        ]


class TestNoSecretLeaks:
    def test_no_provider_contains_key_substring(self):
        """Catalog metadata must never contain any env key value."""
        catalog = build_catalog()
        # Get any real env keys that happen to be set (should never appear in catalog text)
        sensitive = os.environ.get("AIHUBMIX_API_KEY", "")
        if sensitive:
            text = str(catalog.model_dump())
            assert sensitive not in text, "Catalog leaked env key value!"

    def test_catalog_json_no_key_field(self):
        """The JSON representation must not have an 'api_key' or 'key' field."""
        catalog = build_catalog()
        data = catalog.model_dump()
        text = str(data)
        # The word "api_key" might appear in field names (env_key_configured is fine)
        # But no field should hold an actual key value
        for p in data["providers"]:
            assert "api_key" not in p, f"Provider {p['id']} has api_key field"
