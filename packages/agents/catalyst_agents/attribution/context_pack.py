"""V1.1 EvidenceAnalystContextPack contract (M2-5).

Deterministic model-facing pack (Frozen §6.3.1). Round-one packs keep the
four prior_* semantic fields empty; round two may carry bounded references
from the prior Analyst decision/assessment over the cumulative EvidenceState.
Packing/rendering logic lands in M4.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_agents.attribution.coverage import CoverageSummary
from catalyst_agents.attribution.evidence_state import EvidenceStateItem
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_agents.retrieval.task import ResearchTask

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class EvidenceAnalystContextPack(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    packing_policy_version: str
    round: int
    token_budget: int
    hard_constraints: dict[str, Any]
    observation: MoveProfile
    coverage_summary: CoverageSummary
    research_history: tuple[ResearchTask, ...] = ()
    direct_primary_evidence: tuple[EvidenceStateItem, ...] = ()
    primary_authority_evidence: tuple[EvidenceStateItem, ...] = ()
    independent_reports: tuple[EvidenceStateItem, ...] = ()
    lead_only_evidence: tuple[EvidenceStateItem, ...] = ()
    deterministic_conflict_signals: tuple[str, ...] = ()
    coverage_gaps: tuple[str, ...] = ()
    included_evidence_ids: tuple[str, ...] = ()
    excluded_evidence_ids: tuple[str, ...] = ()
    truncation_metadata: dict[str, Any] = {}
    delta_evidence_ids: tuple[str, ...] = ()
    prior_counter_evidence_ids: tuple[str, ...] = ()
    prior_semantic_conflicts: tuple[str, ...] = ()
    previously_supported_hypothesis_ids: tuple[str, ...] = ()
    unresolved_gap_ids: tuple[str, ...] = ()
    context_pack_sha256: str
    rendered_messages_sha256: str

    @field_validator("context_pack_sha256", "rendered_messages_sha256")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def _round_one_prior_fields_empty(self) -> "EvidenceAnalystContextPack":
        if self.round == 1 and (
            self.prior_counter_evidence_ids
            or self.prior_semantic_conflicts
            or self.previously_supported_hypothesis_ids
            or self.unresolved_gap_ids
        ):
            raise ValueError("round-one pack must keep prior_* semantic fields empty")
        return self


__all__ = ["EvidenceAnalystContextPack"]
