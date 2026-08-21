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

from pydantic import (
    BaseModel,
    ConfigDict,
    field_serializer,
    field_validator,
    model_validator,
)

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical._immutable import NoUncheckedCopyUpdates
from catalyst_data.canonical.model import ContentState, SourceClass
from catalyst_data.canonical.temporal import TemporalIdentity


class StageScore(NoUncheckedCopyUpdates, BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str
    value: float | None

    @model_validator(mode="after")
    def _finite(self) -> "StageScore":
        if not self.stage:
            raise ValueError("score stage must not be empty")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("stage score must be finite")
        return self


class StageRank(NoUncheckedCopyUpdates, BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str
    value: int | None

    @model_validator(mode="after")
    def _positive(self) -> "StageRank":
        if not self.stage:
            raise ValueError("rank stage must not be empty")
        if self.value is not None and self.value < 1:
            raise ValueError("stage rank must be a positive integer")
        return self


class RetrievalHit(NoUncheckedCopyUpdates, BaseModel):
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
    scores: tuple[StageScore, ...]
    ranks: tuple[StageRank, ...]
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

    @field_validator("scores", mode="before")
    @classmethod
    def _scores_as_entries(cls, scores: object) -> object:
        if isinstance(scores, dict):
            scores = tuple(
                {"stage": stage, "value": value}
                for stage, value in scores.items()
            )
        if not isinstance(scores, (list, tuple)):
            return scores
        entries = tuple(
            entry if isinstance(entry, StageScore) else StageScore.model_validate(entry)
            for entry in scores
        )
        stages = tuple(entry.stage for entry in entries)
        if len(stages) != len(set(stages)):
            raise ValueError("score stages must be unique")
        return tuple(sorted(entries, key=lambda entry: entry.stage))

    @field_validator("ranks", mode="before")
    @classmethod
    def _ranks_as_entries(cls, ranks: object) -> object:
        if isinstance(ranks, dict):
            ranks = tuple(
                {"stage": stage, "value": value}
                for stage, value in ranks.items()
            )
        if not isinstance(ranks, (list, tuple)):
            return ranks
        entries = tuple(
            entry if isinstance(entry, StageRank) else StageRank.model_validate(entry)
            for entry in ranks
        )
        stages = tuple(entry.stage for entry in entries)
        if len(stages) != len(set(stages)):
            raise ValueError("rank stages must be unique")
        return tuple(sorted(entries, key=lambda entry: entry.stage))

    @field_serializer("scores")
    def _serialize_scores(
        self, scores: tuple[StageScore, ...]
    ) -> dict[str, float | None]:
        return {entry.stage: entry.value for entry in scores}

    @field_serializer("ranks")
    def _serialize_ranks(self, ranks: tuple[StageRank, ...]) -> dict[str, int | None]:
        return {entry.stage: entry.value for entry in ranks}

    @model_validator(mode="after")
    def _evidence_identity(self) -> "RetrievalHit":
        if (self.chunk_id is None) == (self.fact_id is None):
            raise ValueError("exactly one of chunk_id or fact_id must be set")
        if self.chunk_id is not None and self.evidence_id != self.chunk_id:
            raise ValueError("text hit evidence_id must equal chunk_id")
        if self.fact_id is not None and self.evidence_id != self.fact_id:
            raise ValueError("structured hit evidence_id must equal fact_id")
        return self


class RetrievalResultSet(NoUncheckedCopyUpdates, BaseModel):
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
            for entry in hit.ranks:
                if entry.value is not None:
                    by_stage.setdefault(entry.stage, []).append(entry.value)
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


__all__ = ["RetrievalHit", "RetrievalResultSet", "StageRank", "StageScore"]
