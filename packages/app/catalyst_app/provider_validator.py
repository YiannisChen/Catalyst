"""Validate provider connectivity with a lightweight probe request.

Returns structured status without leaking the API key in any response field.

Pattern borrowed from ValueCell's direct-ping approach: send a minimal
completion, classify errors, never echo the key.
"""
from __future__ import annotations

import os
import time
from typing import Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

from catalyst_app.llm_factory import _PROVIDER_DEFAULTS
from catalyst_app.env_loader import get_provider_env_key


# Timeout for probe requests (seconds)
PROBE_TIMEOUT = 15
PROBE_PROMPT = "1+1="


class ProviderValidationResult:
    def __init__(
        self,
        ok: bool,
        status: str,
        message: str | None,
        latency_ms: int | None,
    ):
        self.ok = ok
        self.status = status
        self.message = message
        self.latency_ms = latency_ms


def validate_provider(
    *,
    provider: str,
    model_id: str,
    api_key: str,
    base_url: str | None = None,
    credential_source: str = "browser_key",
) -> ProviderValidationResult:
    """Send a minimal probe to verify provider connectivity and auth.

    Returns a structured result. The API key is NEVER echoed back in
    any response field.
    """
    resolved_base = base_url or _PROVIDER_DEFAULTS.get(provider)
    if resolved_base is None:
        return ProviderValidationResult(
            ok=False,
            status="invalid",
            message=(
                f"Unknown provider '{provider}'. "
                f"For custom endpoints use provider='custom_openai_compatible' with a base_url."
            ),
            latency_ms=None,
        )

    # Resolve API key for server_env mode
    resolved_key = api_key
    if credential_source == "server_env" and not resolved_key:
        env_key_name = get_provider_env_key(provider)
        if not env_key_name:
            return ProviderValidationResult(
                ok=False,
                status="auth_failed",
                message=f"Server env key not configured for provider '{provider}'.",
                latency_ms=None,
            )
        resolved_key = os.environ.get(env_key_name, "")
        if not resolved_key:
            return ProviderValidationResult(
                ok=False,
                status="auth_failed",
                message=f"Server env key not configured for provider '{provider}'.",
                latency_ms=None,
            )
    if not resolved_key:
        return ProviderValidationResult(
            ok=False,
            status="auth_failed",
            message="No API key provided.",
            latency_ms=None,
        )

    llm = ChatOpenAI(
        model=model_id,
        api_key=resolved_key,
        base_url=resolved_base,
        temperature=0.0,
        max_tokens=5,
        timeout=PROBE_TIMEOUT,
        max_retries=0,
    )

    start = time.monotonic()
    try:
        resp = llm.invoke([HumanMessage(content=PROBE_PROMPT)])
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ProviderValidationResult(
            ok=True,
            status="ready",
            message=None,
            latency_ms=elapsed_ms,
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        msg = str(exc).lower()

        # Classify errors — NEVER include the raw API key
        if any(token in msg for token in ("401", "403", "auth", "invalid_api_key")):
            return ProviderValidationResult(
                ok=False,
                status="auth_failed",
                message="Authentication failed. Check your API key.",
                latency_ms=elapsed_ms,
            )
        if any(token in msg for token in ("timeout", "timed out")):
            return ProviderValidationResult(
                ok=False,
                status="timeout",
                message=f"Connection timed out after {PROBE_TIMEOUT}s.",
                latency_ms=elapsed_ms,
            )
        if any(
            token in msg
            for token in ("name or service not known", "connection refused", "getaddrinfo")
        ):
            return ProviderValidationResult(
                ok=False,
                status="request_failed",
                message="Could not reach the provider. Check your base_url and network.",
                latency_ms=elapsed_ms,
            )
        return ProviderValidationResult(
            ok=False,
            status="request_failed",
            message="Provider returned an error.",
            latency_ms=elapsed_ms,
        )
