from __future__ import annotations

import os
import pytest
from unittest.mock import patch

from catalyst_app.llm_factory import build_llm, SUPPORTED_MODELS, DEFAULT_MODEL


def test_build_llm_returns_chat_openai_instance():
    """build_llm returns a ChatOpenAI configured for aihubmix."""
    with patch.dict(os.environ, {"AIHUBMIX_API_KEY": "sk-test-key-123"}):
        llm = build_llm("gemini-2.5-flash-nothink")
        assert llm.model_name == "gemini-2.5-flash-nothink"
        assert "aihubmix.com" in str(llm.openai_api_base)


def test_build_llm_raises_without_api_key():
    """build_llm raises ValueError when AIHUBMIX_API_KEY is missing."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="AIHUBMIX_API_KEY"):
            build_llm("gemini-2.5-flash-nothink")


def test_supported_models_returns_list():
    """SUPPORTED_MODELS contains the 8 expected model identifiers."""
    assert len(SUPPORTED_MODELS) == 8
    assert "gemini-2.5-flash-nothink" in SUPPORTED_MODELS
    assert "claude-opus-4-6" in SUPPORTED_MODELS


def test_default_model_is_gemini_flash():
    """DEFAULT_MODEL is gemini-2.5-flash-nothink for cost efficiency."""
    assert DEFAULT_MODEL == "gemini-2.5-flash-nothink"


def test_build_llm_rejects_unknown_model():
    """build_llm raises ValueError for model IDs not in SUPPORTED_MODELS."""
    with patch.dict(os.environ, {"AIHUBMIX_API_KEY": "sk-test-key-123"}):
        with pytest.raises(ValueError, match="not supported"):
            build_llm("gpt-4-turbo-not-real")
