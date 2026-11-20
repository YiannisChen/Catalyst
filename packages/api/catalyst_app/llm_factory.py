"""LLM client factory — BYOK runtime credential path.

Provides build_v1_llm() / build_llm(), which create the thin OpenAI-compatible chat client
(catalyst_agents.llm.ChatClient; LangChain is no longer used on the run path).

Supports two paths:
  Legacy/dev:   model_id string + AIHUBMIX_API_KEY env variable
  Production:   provider + model_id + api_key + optional base_url

Never persists the API key. The caller is responsible for key lifecycle.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from catalyst_agents.llm import ChatClient

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
V1_MAX_OUTPUT_TOKENS: int = 2_000

# Endpoints that reject ``response_format: json_schema`` cannot receive the
# OpenAI structured-output shape. They are steered to the LangChain
# ``function_calling`` method instead: the AnalystDecision schema travels in
# ``tools[]`` with a forced ``tool_choice``, so field names and enum values are
# carried by the request itself. (The 2026-09-15 live c01 run used
# ``json_mode``/``json_object``, which transmits no schema at all, and the model
# answered valid JSON with invented field names twice in a row.)
_PROVIDER_STRUCTURED_OUTPUT_METHODS: dict[str, str] = {
    "deepseek": "function_calling",
}

PROVIDER_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "xai": "https://api.x.ai/v1",
    "aihubmix": "https://aihubmix.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    # Mainland DashScope, as in valuecell: a key from the China console only works here, not on dashscope-intl.
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "glm": "https://api.z.ai/api/paas/v4",
}
_PROVIDER_DEFAULTS = PROVIDER_BASE_URLS


@dataclass(frozen=True)
class PublicModelConfig:
    """The one server-key model used by anonymous and invite runs."""

    provider: str
    model_id: str
    base_url: str


PUBLIC_RUN_MODEL = PublicModelConfig(
    provider="deepseek",
    model_id="deepseek-v4-flash",
    base_url=_PROVIDER_DEFAULTS["deepseek"],
)


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
) -> ChatClient:
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

    return ChatClient(
        model=resolved_model,
        api_key=resolved_api_key,
        base_url=resolved_base_url,
        temperature=0.0,
        timeout=90,
    )


def build_v1_llm(
    model_id: str | None = None,
    *,
    provider: str = "openai",
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
) -> ChatClient:
    """Create the V1.1 ChatOpenAI client (Final Migration TSD §15; M5-0).

    Provider-internal retries are neutralized (``max_retries=0``) so the V1.1
    policy layer owns the single technical retry per logical role
    (``runtime.provider_capability``). Temperature is pinned to 0.0 for
    deterministic structured output, and true streaming declares usage/token
    accounting. Capability metadata is attached to the returned client and
    consumed by ``provider_capability_for`` at admission.

    Credential identity is strict: the V1.1 path never falls back to the
    legacy generic ``AIHUBMIX_API_KEY``; an explicit V1 credential must be
    supplied. ``timeout_seconds`` is the remaining persisted absolute run
    deadline (never a fixed 90-second provider timeout); when omitted the
    client is created without a hardcoded timeout.
    """
    resolved_model = (
        model_id if model_id and model_id != "runtime-default" else DEFAULT_MODEL
    )

    if base_url:
        resolved_base_url = base_url
    elif provider in _PROVIDER_DEFAULTS:
        resolved_base_url = _PROVIDER_DEFAULTS[provider]
    else:
        raise ValueError(
            f"Unknown provider '{provider}'. Provide a base_url for custom endpoints "
            f"(use provider='custom_openai_compatible' with a base_url)."
        )

    if not api_key:
        raise ValueError(
            "No API key available for the V1.1 runtime; an explicit "
            "credential must be resolved before a worker may start."
        )

    # Thinking off for every DeepSeek model: the extractor's small output envelope leaves no room for reasoning
    # tokens. (The CATALYST_DEEPSEEK_THINKING experiment, which needed LangChain tool binding, is retired.)
    extra_body = {"thinking": {"type": "disabled"}} if provider == "deepseek" else None
    client = ChatClient(
        model=resolved_model,
        api_key=api_key,
        base_url=resolved_base_url,
        temperature=0.0,
        max_tokens=V1_MAX_OUTPUT_TOKENS,
        timeout=timeout_seconds,
        extra_body=extra_body,
        capability_metadata={
            "supports_structured_output": True,
            "supports_true_streaming": True,
            "declares_token_accounting": True,
            "normalizes_timeout_errors": True,
            "capability_revision": "v1.1-capability-1",
            # DeepSeek's OpenAI-compatible surface documents response_format json_object|text only, so the analyst
            # admission must not send the OpenAI json_schema shape.
            "structured_output_method": _PROVIDER_STRUCTURED_OUTPUT_METHODS.get(provider),
        },
    )
    # Migration step 0: record / replay the run's model calls when asked to; otherwise the client itself.
    from catalyst_agents.llm import wrap

    return wrap(client)
