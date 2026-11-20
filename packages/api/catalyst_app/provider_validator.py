"""Perform small BYOK validation probes without exposing credentials."""
from __future__ import annotations

import os
import time

import requests
from catalyst_agents.llm import ChatClient

from catalyst_app.env_loader import get_provider_env_key
from catalyst_app.llm_factory import _PROVIDER_DEFAULTS

PROBE_TIMEOUT = 15
PROBE_PROMPT = "1+1="


class ProviderValidationResult:
    def __init__(self, ok: bool, status: str, message: str | None, latency_ms: int | None):
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
    """Send one minimal completion/search request and return a safe status."""
    if provider == "anthropic":
        return ProviderValidationResult(
            False, "unsupported_provider", "Native Anthropic Messages adapter is not implemented.", None
        )
    if provider == "tavily":
        return _validate_tavily(api_key)

    resolved_base = base_url or _PROVIDER_DEFAULTS.get(provider)
    if resolved_base is None:
        return ProviderValidationResult(
            False,
            "invalid",
            f"Unknown provider '{provider}'. For custom endpoints use provider='custom_openai_compatible' with a base_url.",
            None,
        )

    resolved_key = api_key
    if credential_source == "server_env" and not resolved_key:
        env_key_name = get_provider_env_key(provider)
        resolved_key = os.environ.get(env_key_name, "") if env_key_name else ""
        if not resolved_key:
            return ProviderValidationResult(
                False, "auth_failed", f"Server env key not configured for provider '{provider}'.", None
            )
    if not resolved_key:
        return ProviderValidationResult(False, "auth_failed", "No API key provided.", None)

    start = time.monotonic()
    try:
        llm = ChatClient(
            model=model_id,
            api_key=resolved_key,
            base_url=resolved_base,
            temperature=0.0,
            max_tokens=1,
            timeout=PROBE_TIMEOUT,
        )
        llm.invoke([{"role": "user", "content": PROBE_PROMPT}])
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ProviderValidationResult(True, "ready", None, elapsed_ms)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        msg = str(exc).lower()
        # xAI and Gemini answer a wrong key with 400 ("Incorrect API key", "API key not valid").
        if any(
            token in msg
            for token in ("401", "403", "auth", "invalid_api_key", "api key", "api_key_invalid", "permission_denied")
        ):
            return ProviderValidationResult(False, "auth_failed", "Authentication failed. Check your API key.", elapsed_ms)
        if any(token in msg for token in ("timeout", "timed out")):
            return ProviderValidationResult(
                False, "timeout", f"Connection timed out after {PROBE_TIMEOUT}s.", elapsed_ms
            )
        if any(token in msg for token in ("name or service not known", "connection refused", "getaddrinfo")):
            return ProviderValidationResult(False, "request_failed", "Could not reach the provider.", elapsed_ms)
        return ProviderValidationResult(False, "request_failed", "Provider returned an error.", elapsed_ms)


def _validate_tavily(api_key: str) -> ProviderValidationResult:
    """Make one bounded, one-result Tavily request without retaining its body."""
    if not api_key.strip():
        return ProviderValidationResult(False, "auth_failed", "No API key provided.", None)
    start = time.monotonic()
    try:
        response = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": "provider key validation", "max_results": 1},
            timeout=PROBE_TIMEOUT,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if response.status_code in (401, 403):
            return ProviderValidationResult(False, "auth_failed", "Authentication failed.", elapsed_ms)
        if response.status_code >= 400:
            return ProviderValidationResult(False, "request_failed", "Provider returned an error.", elapsed_ms)
        return ProviderValidationResult(True, "ready", None, elapsed_ms)
    except requests.Timeout:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ProviderValidationResult(
            False, "timeout", f"Connection timed out after {PROBE_TIMEOUT}s.", elapsed_ms
        )
    except requests.RequestException:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ProviderValidationResult(False, "request_failed", "Could not reach the provider.", elapsed_ms)
    except Exception:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ProviderValidationResult(False, "request_failed", "Provider returned an error.", elapsed_ms)
