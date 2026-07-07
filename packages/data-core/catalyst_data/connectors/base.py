"""Base types for data connectors.

Connector convention: each provider module exposes a ``create_<name>_fetcher``
factory function that returns an ``async def fetch(ticker, endpoint, date) ->
FetchResult`` closure.  The closure captures API keys, HTTP clients, and rate
limiters via closure scope.  This pattern composes well with ``retry.with_retry``
and avoids the overhead of Protocol / ABC boilerplate for a small connector set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FetchResult:
    """Uniform result from any connector (FMP, yfinance, GDELT, etc.)."""
    status: int
    data: dict | None = None
    error: str | None = None
    latency_ms: float = 0.0
    source_label: str = ""
    retry_after_seconds: float | None = None
    error_class: str | None = None        # H2: ErrorClass value from error_taxonomy
    items_count: int | None = None        # H2: number of items in data
