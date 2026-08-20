"""V1.1 ResearchTask contract (M2-4).

Agents-owned research task definition and the frozen EvidenceNeed retrieval
strategy mapping (Frozen §6.2). MARKET_STRUCTURE has no V1.1 backend: it maps
to NO_BACKEND and must never create a retrieval action.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator


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


class RetrievalStrategy(str, Enum):
    HYBRID_TEXT = "HYBRID_TEXT"
    DETERMINISTIC_STRUCTURED = "DETERMINISTIC_STRUCTURED"
    NO_BACKEND = "NO_BACKEND"


class ResearchTask(BaseModel):
    """What evidence is needed and when to search (Frozen §6.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    evidence_need: EvidenceNeed
    time_scope: TimeScope
    lookback_sessions: int | None = None
    query_hints: tuple[str, ...] = ()
    retrieval_policy_id: str

    @model_validator(mode="after")
    def _lookback_requires_lookback_scope(self) -> "ResearchTask":
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
    "TimeScope",
    "retrieval_strategy_for",
]
