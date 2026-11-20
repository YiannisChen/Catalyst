"""Lightweight tests for env_loader.py — no network, no LanceDB, no models."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "data"))

from catalyst_app.env_loader import (
    _ENV_KEY_ALIASES,
    _parse_dotenv,
    get_provider_env_key,
    is_env_key_configured,
    load_env_files,
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
        assert get_provider_env_key("xai") == "XAI_API_KEY"
        assert get_provider_env_key("qwen") == "QWEN_API_KEY"
        assert get_provider_env_key("gemini") == "GEMINI_API_KEY"
        assert get_provider_env_key("anthropic") == "ANTHROPIC_API_KEY"
        assert get_provider_env_key("tavily") == "TAVILY_API_KEY"

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


class TestLoadEnvFiles:
    def test_root_file_supplies_all_keys_and_normalizes_aliases(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "catalyst_app.env_loader._parse_dotenv",
            return_value={"POLYGON_API_KEY": "file-polygon", "deepseek_api_key": "file-deepseek"},
        ) as parse:
            load_env_files()
            assert os.environ["POLYGON_API_KEY"] == "file-polygon"
            assert os.environ["DEEPSEEK_API_KEY"] == "file-deepseek"
            assert parse.call_count == 1
            assert parse.call_args.args[0].name == ".env"

    def test_process_environment_takes_priority(self):
        with patch.dict(os.environ, {"POLYGON_API_KEY": "process-polygon", "DEEPSEEK_API_KEY": "process-deepseek"}, clear=True), patch(
            "catalyst_app.env_loader._parse_dotenv",
            return_value={"POLYGON_API_KEY": "file-polygon", "deepseek_api_key": "file-deepseek"},
        ):
            load_env_files()
            assert os.environ["POLYGON_API_KEY"] == "process-polygon"
            assert os.environ["DEEPSEEK_API_KEY"] == "process-deepseek"
            assert os.environ["deepseek_api_key"] == "process-deepseek"
