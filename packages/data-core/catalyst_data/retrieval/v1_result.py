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

from typing import Literal

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical._immutable import NoUncheckedCopyUpdates
from catalyst_data.canonical.model import AssetType, ContentState, SourceClass, source_role_for
from catalyst_data.canonical.temporal import TemporalIdentity

MaterialCapability = Literal["MATERIAL_CAPABLE", "LEAD_ONLY", "NOT_CAPABLE"]

_MATERIAL_CAPABILITY_BY_STATE: dict[str, MaterialCapability] = {
    "FULL_TEXT": "MATERIAL_CAPABLE",
    "TITLE_ONLY": "LEAD_ONLY",
    "METADATA_ONLY": "NOT_CAPABLE",
}


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
    TemporalIdentity/DataRuntimeIdentity it was served under. M4-0 extends the
    contract with the complete data-owned metadata required to construct
    EvidenceState without querying data-core tables again (section identity,
    ordinal, asset type, content hash, material capability, serving status,
    temporal precision, independence group, canonical URL, evidence role).
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
    # M4-0 data-owned metadata (amendment §1.3)
    section_key: str | None = None
    chunk_ordinal: int | None = None
    asset_type: AssetType
    content_hash: str | None = None
    material_capability: MaterialCapability
    serving_status: str
    temporal_precision: str
    independence_group_id: str | None = None
    canonical_url: str | None = None
    evidence_role: str

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

    @field_validator("chunk_ordinal")
    @classmethod
    def _non_negative_ordinal(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("chunk_ordinal must be non-negative")
        return value

    @model_validator(mode="after")
    def _evidence_identity(self) -> "RetrievalHit":
        if (self.chunk_id is None) == (self.fact_id is None):
            raise ValueError("exactly one of chunk_id or fact_id must be set")
        if self.chunk_id is not None and self.evidence_id != self.chunk_id:
            raise ValueError("text hit evidence_id must equal chunk_id")
        if self.fact_id is not None and self.evidence_id != self.fact_id:
            raise ValueError("structured hit evidence_id must equal fact_id")
        if not self.evidence_role:
            raise ValueError("evidence_role must not be empty")
        if not self.serving_status:
            raise ValueError("serving_status must not be empty")
        if not self.temporal_precision:
            raise ValueError("temporal_precision must not be empty")
        expected_role = source_role_for(self.source_class).value
        if self.evidence_role != expected_role:
            raise ValueError(
                "evidence_role must be the frozen source-role ceiling for "
                f"source_class {self.source_class.value!r}"
            )
        expected_capability = _MATERIAL_CAPABILITY_BY_STATE.get(self.content_state)
        if expected_capability is None:
            raise ValueError(
                "EMPTY/FAILED content must never be served as a retrieval hit"
            )
        if self.material_capability != expected_capability:
            raise ValueError(
                "material_capability must derive exactly from content_state: "
                f"{self.content_state} -> {expected_capability}"
            )
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


__all__ = ["MaterialCapability", "RetrievalHit", "RetrievalResultSet", "StageRank", "StageScore"]
