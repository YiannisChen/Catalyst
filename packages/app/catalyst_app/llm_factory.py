"""LLM client factory for aihubmix OpenAI-compatible proxy.

Provides build_llm() which creates a LangChain ChatOpenAI instance configured
to use the aihubmix relay at https://aihubmix.com/v1.

The API key is read from the AIHUBMIX_API_KEY environment variable.
"""
from __future__ import annotations

import os

from langchain_openai import ChatOpenAI


AIHUBMIX_BASE_URL = "https://aihubmix.com/v1"

SUPPORTED_MODELS: list[str] = [
    "gemini-2.5-flash-nothink",
    "claude-opus-4-6",
    "deepseek-v4-flash",
    "qwen3.6-flash",
    "qwen-turbo",
    "deepseek-v3",
    "coding-minimax-m2.7-free",
    "qwen3.6-plus-preview-free",
]

DEFAULT_MODEL: str = "gemini-2.5-flash-nothink"


def _get_api_key() -> str:
    key = os.environ.get("AIHUBMIX_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "AIHUBMIX_API_KEY environment variable is required but not set. "
            "Set it in your .env or shell environment."
        )
    return key


def build_llm(model_id: str | None = None) -> ChatOpenAI:
    """Create a ChatOpenAI instance targeting the aihubmix proxy.

    Args:
        model_id: One of SUPPORTED_MODELS. Defaults to DEFAULT_MODEL
                  when None or "runtime-default".

    Returns:
        A configured ChatOpenAI with the aihubmix base URL and API key.

    Raises:
        ValueError: If the model_id is not in SUPPORTED_MODELS or
                    AIHUBMIX_API_KEY is not set.
    """
    resolved_model = model_id if model_id and model_id != "runtime-default" else DEFAULT_MODEL

    if resolved_model not in SUPPORTED_MODELS:
        raise ValueError(
            f"Model '{resolved_model}' is not supported. "
            f"Choose from: {', '.join(SUPPORTED_MODELS)}"
        )

    api_key = _get_api_key()

    return ChatOpenAI(
        model=resolved_model,
        api_key=api_key,
        base_url=AIHUBMIX_BASE_URL,
        temperature=0.0,
        max_retries=2,
        timeout=90,
    )
