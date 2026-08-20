"""V1.1 corrective research contracts (M2-6).

Code-owned executable artifacts normalized from model proposals (Frozen §6.4).
Production freezes: max_corrective_rounds = 1, max_actions_per_batch = 1.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator

from catalyst_agents.retrieval.task import EvidenceNeed, TimeScope


class GapReasonCode(str, Enum):
    """The nine frozen V1.1 gap/capability reason codes (Frozen §6.4)."""

    MISSING_PRIMARY_CONFIRMATION = "MISSING_PRIMARY_CONFIRMATION"
    MISSING_INDEPENDENT_CORROBORATION = "MISSING_INDEPENDENT_CORROBORATION"
    MISSING_PRIOR_SESSION_CONTEXT = "MISSING_PRIOR_SESSION_CONTEXT"
    MISSING_SECTOR_CONTEXT = "MISSING_SECTOR_CONTEXT"
    MISSING_MACRO_CONTEXT = "MISSING_MACRO_CONTEXT"
    MISSING_FUNDAMENTAL_CONTEXT = "MISSING_FUNDAMENTAL_CONTEXT"
    CONFLICT_REQUIRES_RESOLUTION = "CONFLICT_REQUIRES_RESOLUTION"
    MARKET_STRUCTURE_UNSUPPORTED = "MARKET_STRUCTURE_UNSUPPORTED"
    LOCAL_COVERAGE_GAP = "LOCAL_COVERAGE_GAP"


MAX_CORRECTIVE_ROUNDS = 1
MAX_ACTIONS_PER_BATCH = 1


class MissingEvidence(BaseModel):
    """Code-normalized gap record; recoverability is code-owned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_id: str
    evidence_need: EvidenceNeed
    time_scope: TimeScope
    expected_information: str
    reason_code: GapReasonCode
    recoverable: bool

    @model_validator(mode="after")
    def _market_structure_is_non_recoverable(self) -> "MissingEvidence":
        if (
            self.reason_code is GapReasonCode.MARKET_STRUCTURE_UNSUPPORTED
            and self.recoverable
        ):
            raise ValueError(
                "MARKET_STRUCTURE_UNSUPPORTED must be a non-recoverable gap"
            )
        return self


class CorrectiveResearchAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str
    gap_id: str
    evidence_need: EvidenceNeed
    time_scope: TimeScope
    lookback_sessions: int | None = None
    query_hints: tuple[str, ...] = ()
    research_fingerprint: str


class CorrectiveResearchBatch(BaseModel):
    """Code-owned batch; production freezes at one action per batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    actions: tuple[CorrectiveResearchAction, ...] = ()
    shared_deadline: datetime
    total_result_budget: int

    @model_validator(mode="after")
    def _production_freeze(self) -> "CorrectiveResearchBatch":
        if len(self.actions) > MAX_ACTIONS_PER_BATCH:
            raise ValueError(
                "production freeze: at most one corrective action per batch"
            )
        gap_ids = [action.gap_id for action in self.actions]
        if len(gap_ids) != len(set(gap_ids)):
            raise ValueError("one gap ID maps to at most one action")
        return self


__all__ = [
    "GapReasonCode",
    "MAX_ACTIONS_PER_BATCH",
    "MAX_CORRECTIVE_ROUNDS",
    "CorrectiveResearchAction",
    "CorrectiveResearchBatch",
    "MissingEvidence",
]
