"""Strict retrieval result contracts shared by lexical and later retrieval arms."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .trace import RetrievalTrace

SEARCHABLE_STATUSES = ("active", "pending_embedding", "embedded", "metadata_only")
SOURCE_CLASSES = frozenset({
    "structured_market_data", "official_government", "issuer_disclosure",
    "corporate_press_release", "reported_news", "analysis_opinion",
    "aggregated_unknown",
})
EVIDENCE_TYPES = frozenset({"news_v2", "filing_v2"})
FALLBACK_REASONS = ("empty_query", "fts5_unavailable", "fts5_missing", "fts5_stale")


class RetrievalContractError(ValueError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class RetrievalFilters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    requested_manifest_id: str
    cutoff: str
    statuses: tuple[str, ...] = SEARCHABLE_STATUSES
    eligibility: Literal["eligible"] = "eligible"
    source_classes: tuple[str, ...] | None = None
    evidence_types: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _validate_contract(self) -> "RetrievalFilters":
        if self.statuses != SEARCHABLE_STATUSES:
            raise ValueError("statuses must use the searchable-status contract order")
        if self.source_classes is not None and self.source_classes != tuple(sorted(set(self.source_classes))):
            raise ValueError("source_classes must be sorted and unique")
        if self.evidence_types is not None and self.evidence_types != tuple(sorted(set(self.evidence_types))):
            raise ValueError("evidence_types must be sorted and unique")
        return self


class RetrievalResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    document_id: str
    available_at: str
    cutoff: str
    filters_applied: RetrievalFilters
    source_class: Literal[
        "structured_market_data", "official_government", "issuer_disclosure",
        "corporate_press_release", "reported_news", "analysis_opinion",
        "aggregated_unknown",
    ]
    lexical_raw_score: float | None
    lexical_rank: int = Field(ge=1)
    dense_score: float | None = None
    dense_rank: int | None = Field(default=None, ge=1)
    fusion_score: float | None = None
    fusion_rank: int | None = Field(default=None, ge=1)
    reranker_score: float | None = None
    reranker_rank: int | None = Field(default=None, ge=1)
    corpus_manifest_id: str
    index_manifest_id: str | None = None
    mode_requested: Literal["lexical", "dense", "hybrid", "reranked"]
    mode_served: Literal["fts5", "sql_like", "dense", "hybrid", "reranked"]
    is_degraded: bool
    fallback_reason: Literal[
        "empty_query", "fts5_unavailable", "fts5_missing", "fts5_stale"
    ] | None = None
    timing_ms: float = Field(ge=0)

    @field_validator("corpus_manifest_id")
    @classmethod
    def _manifest_hex(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("corpus_manifest_id must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _validate_modes(self) -> "RetrievalResult":
        normal_mode = {
            "lexical": "fts5", "dense": "dense",
            "hybrid": "hybrid", "reranked": "reranked",
        }[self.mode_requested]
        if self.is_degraded != (self.mode_served != normal_mode):
            raise ValueError("is_degraded must match mode_served")
        if not self.is_degraded and self.fallback_reason not in (None, "empty_query"):
            raise ValueError("non-degraded result cannot carry a degradation reason")
        return self


class RetrievalResultSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[RetrievalResult, ...] = ()
    results: tuple[RetrievalResult, ...] = ()
    candidate_count: int = Field(ge=0)
    mode_requested: Literal["lexical", "dense", "hybrid", "reranked"]
    mode_served: Literal["fts5", "sql_like", "dense", "hybrid", "reranked"]
    is_degraded: bool
    fallback_reason: Literal[
        "empty_query", "fts5_unavailable", "fts5_missing", "fts5_stale"
    ] | None = None
    trace: RetrievalTrace | None = None

    @model_validator(mode="after")
    def _validate_set(self) -> "RetrievalResultSet":
        if self.candidate_count != len(self.candidates):
            raise ValueError("candidate_count must equal len(candidates)")
        if self.results != self.candidates[:len(self.results)]:
            raise ValueError("results must be a prefix of candidates")
        normal_mode = {
            "lexical": "fts5", "dense": "dense",
            "hybrid": "hybrid", "reranked": "reranked",
        }[self.mode_requested]
        if self.is_degraded != (self.mode_served != normal_mode):
            raise ValueError("is_degraded must match mode_served")
        return self


__all__ = [
    "RetrievalContractError", "RetrievalFilters", "RetrievalResult",
    "RetrievalResultSet", "RetrievalTrace",
]
