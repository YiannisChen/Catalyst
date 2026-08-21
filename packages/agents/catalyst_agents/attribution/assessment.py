"""V1.1 EvidenceAssessment contract (M2-6).

Deterministic code-normalized assessment (Final Migration TSD §8.1). It binds
the raw AnalystDecision, the persisted pack/render hashes, the code-owned
status ceiling, and an optional code-owned corrective batch whose actions
reference distinct recoverable validated gaps.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    AttributionStatus,
)
from catalyst_agents.retrieval.corrective import (
    CorrectiveResearchBatch,
    MissingEvidence,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class EvidenceAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    analyst_decision: AnalystDecision
    validated_missing_evidence: tuple[MissingEvidence, ...] = ()
    status_ceiling: AttributionStatus
    corrective_batch: CorrectiveResearchBatch | None = None
    normalization_violations: tuple[str, ...] = ()
    decision_hash: str
    context_pack_sha256: str
    rendered_messages_sha256: str
    normalization_policy_version: str

    @field_validator("decision_hash", "context_pack_sha256", "rendered_messages_sha256")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def _corrective_batch_matches_validated_gaps(self) -> "EvidenceAssessment":
        if self.corrective_batch is None:
            return self
        gaps = {gap.gap_id: gap for gap in self.validated_missing_evidence}
        for action in self.corrective_batch.actions:
            gap = gaps.get(action.gap_id)
            if gap is None:
                raise ValueError(
                    "corrective action references a gap not in validated_missing_evidence"
                )
            if not gap.recoverable:
                raise ValueError("corrective action requires a recoverable gap")
            if gap.evidence_need != action.evidence_need or gap.time_scope != action.time_scope:
                raise ValueError("corrective action must match its gap need and time scope")
        return self


__all__ = ["EvidenceAssessment"]
