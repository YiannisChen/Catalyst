from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StageResult:
    """Uniform result envelope for every pipeline stage."""

    stage: str
    ok: bool
    data: dict | list | str | None = None
    error: str | None = None
    latency_ms: float = 0.0
