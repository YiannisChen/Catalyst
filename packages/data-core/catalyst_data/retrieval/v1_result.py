"""V1.1 retrieval result set/hit contract (M2-2).

This is a separate typed contract from the live legacy ``retrieval/result.py``
contract. The legacy ``RetrievalResult``/``RetrievalResultSet`` remain
BASELINE_ONLY until their named migration gate (M3-11); the V1.1 contract is
not wired into any live call site in M2.

Phase 2 corrective: the result set itself binds TemporalIdentity and
DataRuntimeIdentity so identity passes unchanged through RetrievalResultSet
even when hits are empty; every hit must match the set-level identity.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import ContentState, SourceClass
from catalyst_data.canonical.temporal import TemporalIdentity


class RetrievalHit(BaseModel):
    """A single V1.1 retrieval hit with complete canonical evidence identity.

    Frozen §5.4: every hit carries per-stage nullable scores/ranks, evidence
    identity and excerpt, provider/publisher/source class, timestamps,
    content/materiality state, dedup/parse metadata, retrieval policy, and the
    TemporalIdentity/DataRuntimeIdentity it was served under.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    canonical_asset_id: str
    content_version_id: str
    corpus_document_id: str
    chunk_id: str | None = None
    fact_id: str | None = None
    excerpt: str
    scores: dict[str, float | None]
    ranks: dict[str, int | None]
    source_class: SourceClass
    content_state: ContentState
    eligible_at: datetime
    ticker_scope: tuple[str, ...]
    provider: str
    publisher: str | None = None
    dedup_cluster_id: str | None = None
    parse_quality: str
    retrieval_policy_version: str
    temporal_identity: TemporalIdentity
    data_runtime_identity: DataRuntimeIdentity

    @field_validator("scores")
    @classmethod
    def _scores_finite(cls, scores: dict[str, float | None]) -> dict[str, float | None]:
        for stage, value in scores.items():
            if value is not None and not math.isfinite(value):
                raise ValueError(f"score for stage {stage!r} must be finite")
        return scores

    @field_validator("ranks")
    @classmethod
    def _ranks_positive(cls, ranks: dict[str, int | None]) -> dict[str, int | None]:
        for stage, value in ranks.items():
            if value is not None and value < 1:
                raise ValueError(f"rank for stage {stage!r} must be a positive integer")
        return ranks

    @model_validator(mode="after")
    def _evidence_identity(self) -> "RetrievalHit":
        if (self.chunk_id is None) == (self.fact_id is None):
            raise ValueError("exactly one of chunk_id or fact_id must be set")
        if self.chunk_id is not None and self.evidence_id != self.chunk_id:
            raise ValueError("text hit evidence_id must equal chunk_id")
        if self.fact_id is not None and self.evidence_id != self.fact_id:
            raise ValueError("structured hit evidence_id must equal fact_id")
        return self


class RetrievalResultSet(BaseModel):
    """Ordered V1.1 retrieval hits for one run under one runtime identity.

    Per-stage ranks across hits are positive and contiguous within each
    participating stage; a missing modality stays None and is never fabricated
    (Frozen §5.4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hits: tuple[RetrievalHit, ...] = ()
    temporal_identity: TemporalIdentity
    data_runtime_identity: DataRuntimeIdentity

    @model_validator(mode="after")
    def _ranks_contiguous_within_stage(self) -> "RetrievalResultSet":
        by_stage: dict[str, list[int]] = {}
        for hit in self.hits:
            for stage, value in hit.ranks.items():
                if value is not None:
                    by_stage.setdefault(stage, []).append(value)
        for stage, ranks in by_stage.items():
            expected = list(range(1, len(ranks) + 1))
            if sorted(ranks) != expected:
                raise ValueError(
                    f"ranks for stage {stage!r} must be contiguous within the "
                    "participating stage"
                )
        for hit in self.hits:
            if hit.temporal_identity != self.temporal_identity:
                raise ValueError(
                    "hit temporal identity must match the set-level TemporalIdentity"
                )
            if hit.data_runtime_identity != self.data_runtime_identity:
                raise ValueError(
                    "hit runtime identity must match the set-level DataRuntimeIdentity"
                )
        return self


__all__ = ["RetrievalHit", "RetrievalResultSet"]
