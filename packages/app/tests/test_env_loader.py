"""Lightweight tests for env_loader.py — no network, no LanceDB, no models."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "data-core"))

import pytest
from catalyst_app.env_loader import (
    _ENV_KEY_ALIASES,
    _parse_dotenv,
    get_provider_env_key,
    is_env_key_configured,
)


class TestGlmAliases:
    def test_correct_aliases_no_duplicates(self):
        aliases = _ENV_KEY_ALIASES["GLM_API_KEY"]
        assert "glm_api_key" in aliases
        assert "GLM_API_KEY" in aliases
        assert "zai_api_key" in aliases
        assert "ZAI_API_KEY" in aliases
        assert len(aliases) == len(set(aliases)), "GLM aliases must have no duplicates"


class TestGetProviderEnvKey:
    def test_known_providers(self):
        assert get_provider_env_key("aihubmix") == "AIHUBMIX_API_KEY"
        assert get_provider_env_key("deepseek") == "DEEPSEEK_API_KEY"
        assert get_provider_env_key("glm") == "GLM_API_KEY"

    def test_unknown_providers(self):
        assert get_provider_env_key("openrouter") is None
        assert get_provider_env_key("nonsense") is None


class TestIsEnvKeyConfigured:
    def test_false_for_unknown(self):
        assert is_env_key_configured("openrouter") is False

    def test_true_when_env_set(self):
        with patch.dict(os.environ, {"AIHUBMIX_API_KEY": "sk-test"}):
            assert is_env_key_configured("aihubmix") is True

    def test_false_when_empty(self):
        with patch.dict(os.environ, {"AIHUBMIX_API_KEY": ""}):
            assert is_env_key_configured("aihubmix") is False


class TestParseDotenv:
    def test_basic(self):
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False)
        tmp.write("KEY=value\nOTHER=\"quoted\"\n# comment\n\n")
        tmp.close()
        result = _parse_dotenv(Path(tmp.name))
        Path(tmp.name).unlink()
        assert result == {"KEY": "value", "OTHER": "quoted"}

    def test_missing_file(self):
        assert _parse_dotenv(Path("/nonexistent/.env")) == {}
