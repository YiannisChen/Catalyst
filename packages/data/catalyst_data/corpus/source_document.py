"""Shared source record passed from corpus adapters into publication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _SourceDocument:
    source_kind: str
    source_key: str
    document_id: str
    source_utf8_bytes: int
    document: dict[str, Any]
    provider: str
    source_type: str
    eligibility: str
    ineligibility_reason: str | None = None
