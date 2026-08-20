"""V1.1 ResearchTask contract (M2-4, corrective).

Agents-owned research task definition and the frozen EvidenceNeed retrieval
strategy mapping (Frozen §6.2; Phase 3 TSD §7). MARKET_STRUCTURE has no V1.1
backend: it maps to NO_BACKEND and must never create a retrieval action.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_data.canonical.model import SourceClass

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class EvidenceNeed(str, Enum):
    COMPANY_PRIMARY = "COMPANY_PRIMARY"
    COMPANY_NEWS = "COMPANY_NEWS"
    SECTOR_NEWS = "SECTOR_NEWS"
    MACRO_EVENT = "MACRO_EVENT"
    MACRO_SERIES = "MACRO_SERIES"
    FUNDAMENTALS = "FUNDAMENTALS"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"


class TimeScope(str, Enum):
    SESSION_INFORMATION_WINDOW = "SESSION_INFORMATION_WINDOW"
    PRIOR_SESSION = "PRIOR_SESSION"
    LOOKBACK_SESSIONS = "LOOKBACK_SESSIONS"


class ScenarioType(str, Enum):
    SCHEDULED_MACRO = "SCHEDULED_MACRO"
    CONTINUATION = "CONTINUATION"
    BROAD_SECTOR = "BROAD_SECTOR"
    COMPANY_SPECIFIC = "COMPANY_SPECIFIC"
    QUIET_OR_UNCLASSIFIED = "QUIET_OR_UNCLASSIFIED"


class RetrievalStrategy(str, Enum):
    HYBRID_TEXT = "HYBRID_TEXT"
    DETERMINISTIC_STRUCTURED = "DETERMINISTIC_STRUCTURED"
    NO_BACKEND = "NO_BACKEND"


class ResearchTask(BaseModel):
    """What evidence is needed and when to search (Frozen §6.2; Phase 3 §7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    task_id: str
    round: int
    priority: int
    scenario: ScenarioType
    evidence_need: EvidenceNeed
    time_scope: TimeScope
    lookback_sessions: int | None = None
    ticker_scope: tuple[str, ...] = ()
    source_classes: tuple[SourceClass, ...] = ()
    evidence_types: tuple[str, ...] = ()
    query_hints: tuple[str, ...] = ()
    retrieval_policy_id: str
    task_fingerprint: str

    @field_validator("task_fingerprint")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("task_fingerprint must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def _scopes_and_lookback(self) -> "ResearchTask":
        if self.round < 1:
            raise ValueError("round must be positive")
        if self.priority < 0:
            raise ValueError("priority must be non-negative")
        if (
            self.lookback_sessions is not None
            and self.time_scope is not TimeScope.LOOKBACK_SESSIONS
        ):
            raise ValueError("lookback_sessions requires LOOKBACK_SESSIONS time scope")
        if (
            self.time_scope is TimeScope.LOOKBACK_SESSIONS
            and self.lookback_sessions is None
        ):
            raise ValueError("LOOKBACK_SESSIONS time scope requires lookback_sessions")
        if self.lookback_sessions is not None and self.lookback_sessions < 1:
            raise ValueError("lookback_sessions must be a positive session count")
        return self


_RETRIEVAL_STRATEGY_MAPPING: dict[EvidenceNeed, RetrievalStrategy] = {
    EvidenceNeed.COMPANY_PRIMARY: RetrievalStrategy.HYBRID_TEXT,
    EvidenceNeed.COMPANY_NEWS: RetrievalStrategy.HYBRID_TEXT,
    EvidenceNeed.SECTOR_NEWS: RetrievalStrategy.HYBRID_TEXT,
    EvidenceNeed.MACRO_EVENT: RetrievalStrategy.HYBRID_TEXT,
    EvidenceNeed.MACRO_SERIES: RetrievalStrategy.DETERMINISTIC_STRUCTURED,
    EvidenceNeed.FUNDAMENTALS: RetrievalStrategy.DETERMINISTIC_STRUCTURED,
    EvidenceNeed.MARKET_STRUCTURE: RetrievalStrategy.NO_BACKEND,
}


def retrieval_strategy_for(evidence_need: EvidenceNeed) -> RetrievalStrategy:
    """Return the frozen retrieval strategy for an EvidenceNeed (Frozen §6.2)."""
    return _RETRIEVAL_STRATEGY_MAPPING[evidence_need]


__all__ = [
    "EvidenceNeed",
    "ResearchTask",
    "RetrievalStrategy",
    "ScenarioType",
    "TimeScope",
    "retrieval_strategy_for",
]
