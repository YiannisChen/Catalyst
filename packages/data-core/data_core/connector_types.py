from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class FetchResult:
    """Uniform result from any connector (FMP, yfinance, GDELT, etc.)."""
    status: int
    data: dict | None = None
    error: str | None = None
    latency_ms: float = 0.0
    source_label: str = ""


class SourceConnector(Protocol):
    """Protocol that any connector must satisfy for the orchestrator."""
    async def fetch(
        self, ticker: str, endpoint: str, date: str
    ) -> FetchResult: ...
