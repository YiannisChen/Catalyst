"""Provider canary — explicit authorization guard for live provider testing.

The canary path makes at most request_cap requests to a single provider.
It requires explicit authorized=True and must never be called automatically.
"""
from __future__ import annotations

from typing import Any


class ProviderCanaryNotAuthorized(Exception):
    """Canary called without explicit operator authorization."""


def run_provider_canary(
    provider: str,
    profile: dict[str, Any],
    ticker: str = "AAPL",
    *,
    authorized: bool = False,
    request_cap: int = 1,
    transport: Any = None,
) -> dict[str, Any]:
    """Run a provider canary with fake transport for testing.

    With authorized=False (default): raises ProviderCanaryNotAuthorized.
    With authorized=True: requires a transport callable.  Never makes live
    HTTP calls from test code — the transport is injected.

    Returns a canary report dict.
    """
    if not authorized:
        raise ProviderCanaryNotAuthorized(
            "Provider canary requires explicit operator authorization. "
            "Set authorized=True only under direct human supervision."
        )

    if transport is None:
        raise ValueError("transport is required when authorized=True")

    results: dict[str, Any] = {}
    request_count = 0
    try:
        response = transport(provider, ticker, profile)
        request_count = 1
        items = response.get("results", [])
        results[provider] = {
            "status": "ok",
            "items_count": len(items),
            "requests_made": request_count,
        }
    except Exception as exc:
        results[provider] = {
            "status": "error",
            "error": str(exc),
            "requests_made": request_count,
        }

    return {
        "canary_run": True,
        "ticker": ticker,
        "request_cap": request_cap,
        "results": results,
    }
