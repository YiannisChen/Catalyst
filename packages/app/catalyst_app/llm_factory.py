"""LLM client factory — BYOK runtime credential path.

Provides build_llm() which creates a LangChain ChatOpenAI instance.

Supports two paths:
  Legacy/dev:   model_id string + AIHUBMIX_API_KEY env variable
  Production:   provider + model_id + api_key + optional base_url

Never persists the API key. The caller is responsible for key lifecycle.
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

_PROVIDER_DEFAULTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "aihubmix": "https://aihubmix.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "glm": "https://api.z.ai/api/paas/v4",
}


def _get_api_key() -> str:
    """Read AIHUBMIX_API_KEY from environment. Legacy/dev convenience only.

    Raises ValueError if the variable is not set.
    """
    key = os.environ.get("AIHUBMIX_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "AIHUBMIX_API_KEY environment variable is required but not set. "
            "Set it in your .env or shell environment."
        )
    return key


def build_llm(
    model_id: str | None = None,
    *,
    provider: str = "openai",
    api_key: str | None = None,
    base_url: str | None = None,
) -> ChatOpenAI:
    """Create a ChatOpenAI instance from runtime credentials or env fallback.

    Args:
        model_id: Model identifier. When None or "runtime-default", uses
                  DEFAULT_MODEL.  Required unless the legacy env path is active.
        provider: Provider name for base_url resolution. Ignored when base_url
                  is supplied directly.
        api_key:  API key for the provider. When None, falls back to
                  AIHUBMIX_API_KEY env variable (legacy/dev only).
        base_url: Explicit base URL. Takes precedence over provider lookup.

    Returns:
        A configured ChatOpenAI instance.

    Raises:
        ValueError: If no api_key is available and env fallback is not set,
                    or if the provider is unknown and no base_url is given.
    """
    resolved_model = model_id if model_id and model_id != "runtime-default" else DEFAULT_MODEL

    # Base URL resolution
    if base_url:
        resolved_base_url = base_url
    elif provider in _PROVIDER_DEFAULTS:
        resolved_base_url = _PROVIDER_DEFAULTS[provider]
    else:
        raise ValueError(
            f"Unknown provider '{provider}'. Provide a base_url for custom endpoints "
            f"(use provider='custom_openai_compatible' with a base_url)."
        )

    # API key resolution
    resolved_api_key = api_key
    if not resolved_api_key:
        resolved_api_key = _get_api_key()  # legacy/dev fallback
    if not resolved_api_key:
        raise ValueError(
            "No API key available. Provide an api_key or set AIHUBMIX_API_KEY."
        )

    return ChatOpenAI(
        model=resolved_model,
        api_key=resolved_api_key,
        base_url=resolved_base_url,
        temperature=0.0,
        max_retries=2,
        timeout=90,
    )
