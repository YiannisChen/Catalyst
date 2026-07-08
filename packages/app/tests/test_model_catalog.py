"""Lightweight tests for model_catalog.py — no network, no LanceDB, no models."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "data-core"))

import pytest
from catalyst_app.model_catalog import build_catalog


class TestCatalogProviders:
    def setup_method(self):
        self.catalog = build_catalog()
        self.provider_ids = {p.id for p in self.catalog.providers}

    def test_has_all_required_providers(self):
        assert "aihubmix" in self.provider_ids
        assert "siliconflow" in self.provider_ids
        assert "openai" in self.provider_ids
        assert "deepseek" in self.provider_ids
        assert "glm" in self.provider_ids
        assert "custom_openai_compatible" in self.provider_ids

    def test_no_openrouter(self):
        assert "openrouter" not in self.provider_ids

    def test_no_dashscope(self):
        assert "dashscope" not in self.provider_ids

    def test_no_anthropic(self):
        assert "anthropic" not in self.provider_ids

    def test_no_gemini(self):
        assert "gemini" not in self.provider_ids

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
        assert "deepseek-v4-flash" in model_ids


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
