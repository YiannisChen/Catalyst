"""V1.1 corrective research contracts (M2-6, corrective).

Code-owned executable artifacts normalized from model proposals (Phase 4 TSD
§16–19; Frozen §6.4). Production freezes: max_corrective_rounds = 1,
max_actions_per_batch = 1, and a production batch contains exactly one action.
MARKET_STRUCTURE always normalizes to MARKET_STRUCTURE_UNSUPPORTED with
recoverable=false and never creates a retrieval action.
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
    """Code-normalized gap record; recoverability is code-owned (Phase 4 §16)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_id: str
    evidence_need: EvidenceNeed
    time_scope: TimeScope
    expected_information: str
    reason_code: GapReasonCode
    recoverable: bool

    @model_validator(mode="after")
    def _market_structure_normalization(self) -> "MissingEvidence":
        is_market_structure = self.evidence_need is EvidenceNeed.MARKET_STRUCTURE
        is_unsupported = (
            self.reason_code is GapReasonCode.MARKET_STRUCTURE_UNSUPPORTED
        )
        if is_market_structure or is_unsupported:
            if not (is_market_structure and is_unsupported):
                raise ValueError(
                    "MARKET_STRUCTURE must normalize to "
                    "MARKET_STRUCTURE_UNSUPPORTED"
                )
            if self.recoverable:
                raise ValueError(
                    "MARKET_STRUCTURE_UNSUPPORTED must be a non-recoverable gap"
                )
        return self


class CorrectiveResearchAction(BaseModel):
    """One code-owned corrective action (Phase 4 §17)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str
    gap_id: str
    evidence_need: EvidenceNeed
    time_scope: TimeScope
    lookback_sessions: int | None = None
    query_hints: tuple[str, ...] = ()
    research_fingerprint: str

    @model_validator(mode="after")
    def _no_market_structure_and_lookback_consistency(self) -> "CorrectiveResearchAction":
        if self.evidence_need is EvidenceNeed.MARKET_STRUCTURE:
            raise ValueError(
                "MARKET_STRUCTURE must never create a corrective retrieval action"
            )
        if (
            self.lookback_sessions is not None
            and self.time_scope is not TimeScope.LOOKBACK_SESSIONS
        ):
            raise ValueError("lookback_sessions requires LOOKBACK_SESSIONS time scope")
        if self.lookback_sessions is not None and self.lookback_sessions < 1:
            raise ValueError("lookback_sessions must be a positive session count")
        return self


class CorrectiveResearchBatch(BaseModel):
    """Code-owned batch; production freezes at exactly one action (Phase 4 §19)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    run_id: str
    round: int
    actions: tuple[CorrectiveResearchAction, ...]
    shared_deadline: datetime
    internal_deadline: datetime
    total_result_budget: int
    policy_version: str
    cancellation_token_ref: str | None = None

    @model_validator(mode="after")
    def _production_freeze(self) -> "CorrectiveResearchBatch":
        if self.round < 1:
            raise ValueError("round must be positive")
        if len(self.actions) == 0:
            raise ValueError("a corrective research batch cannot be empty")
        if len(self.actions) > MAX_ACTIONS_PER_BATCH:
            raise ValueError(
                "production freeze: at most one corrective action per batch"
            )
        if self.total_result_budget < 1:
            raise ValueError("total_result_budget must be positive")
        if self.shared_deadline > self.internal_deadline:
            raise ValueError("internal_deadline must not precede shared_deadline")
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
