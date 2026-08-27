from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CauseLabel = Literal[
    "market",
    "sector",
    "earnings_guidance",
    "product_demand",
    "legal_regulatory",
    "macro",
    "peer_propagation",
    "supply_chain_propagation",
    "mixed",
    "unexplained",
]
Direction = Literal["positive", "negative", "mixed", "unknown"]


class EvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    document_id: str | None = None
    available_at: str | None = None
    source_class: str | None = None
    ticker_associations: tuple[str, ...] = ()
    dedup_cluster_id: str | None = None
    is_novel: bool = False
    critic_category: str | None = None
    relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    temporal_match: bool = False


class HypothesisDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cause_label: CauseLabel
    direction: Direction
    transmission_mechanism: str = Field(min_length=1)
    supporting_evidence_ids: tuple[str, ...]
    counter_evidence_ids: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    change_condition: str = Field(min_length=1)
    facts: tuple[str, ...]
    calculations: tuple[str, ...]
    inferences: tuple[str, ...]
    unavailable_evidence: tuple[str, ...]


class Hypothesis(HypothesisDraft):
    supporting_evidence: tuple[EvidenceRef, ...] = ()
    counter_evidence: tuple[EvidenceRef, ...] = ()
    prerequisite_gate_passed: bool
    prerequisite_gate_reason: str
    direct_support_exists: bool = False
    independent_supporting_cluster_count: int = Field(default=0, ge=0)
    max_supporting_critic_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    source_support_degradation_count: int = Field(default=0, ge=0)
    max_counter_evidence_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    is_novel: bool = False
    source_support_flags: dict[str, bool] = Field(default_factory=dict)
    validation_violations: tuple[str, ...] = ()
