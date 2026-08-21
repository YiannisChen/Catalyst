"""V1.1 EvidenceState contract (M2-4, corrective).

Immutable, run-scoped cumulative union keyed by evidence_id/fact_id
(Phase 3 TSD §9; Final Migration TSD §4/§6.1). Analyst dispositions do not
live inside canonical EvidenceState; EvidenceAssessment is the separate round
artifact that references evidence IDs. Merging a contribution for an existing
evidence id unions contributions/task IDs, keeps the first-seen round, and
fails integrity validation when immutable metadata conflicts.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.model import AssetType, ContentState, SourceClass
from catalyst_data.canonical.temporal import TemporalIdentity

from catalyst_agents.retrieval.task import EvidenceNeed

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

MaterialCapability = Literal["MATERIAL_CAPABLE", "LEAD_ONLY", "NOT_CAPABLE"]
IndependenceStatus = Literal["KNOWN_GROUP", "UNKNOWN"]


class RetrievalContribution(BaseModel):
    """Per-task retrieval provenance on one evidence item (Phase 3 §9).

    Scores remain nullable and are never fabricated; ranks are positive when
    present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    requested_mode: str
    served_mode: str | None = None
    lexical_rank: int | None = None
    lexical_score: float | None = None
    dense_rank: int | None = None
    dense_score: float | None = None
    fusion_rank: int | None = None
    fusion_score: float | None = None
    reranker_rank: int | None = None
    reranker_score: float | None = None
    original_rank: int | None = None
    degradation_flags: tuple[str, ...] = ()

    @field_validator("lexical_score", "dense_score", "fusion_score", "reranker_score")
    @classmethod
    def _finite_scores(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("scores must be finite")
        return value

    @field_validator(
        "lexical_rank", "dense_rank", "fusion_rank", "reranker_rank", "original_rank"
    )
    @classmethod
    def _positive_ranks(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("ranks must be positive integers")
        return value


class ResearchTaskResultStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    DEGRADED = "DEGRADED"
    FAILED_CAPABILITY = "FAILED_CAPABILITY"
    FAILED_INTEGRITY = "FAILED_INTEGRITY"


class ResearchTaskResult(BaseModel):
    """Bounded task execution result (Phase 3 §8).

    Carries per-task evidence/fact payloads and the runtime identity under
    which the task ran; values are typed and never fabricated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    task_fingerprint: str
    priority: int
    status: ResearchTaskResultStatus
    started_at: datetime
    ended_at: datetime
    latency_ms: int
    deadline_exhausted: bool
    evidence_items: tuple[EvidenceStateItem, ...] = ()
    structured_facts: tuple[EvidenceStateItem, ...] = ()
    mode_requested: str
    mode_served: str | None = None
    degradation_reasons: tuple[str, ...] = ()
    error_code: str | None = None
    data_runtime_identity: DataRuntimeIdentity

    @model_validator(mode="after")
    def _task_timing_and_identity(self) -> "ResearchTaskResult":
        if self.priority < 0:
            raise ValueError("priority must be non-negative")
        if self.started_at > self.ended_at:
            raise ValueError("started_at must not exceed ended_at")
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        for item in self.evidence_items:
            if item.chunk_id is None:
                raise ValueError("evidence_items must be text chunk items")
        for item in self.structured_facts:
            if item.chunk_id is not None:
                raise ValueError("structured_facts must be fact items")
        return self


class RetrievalDegradation(BaseModel):
    """Stable retrieval degradation record (Phase 3 §11)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    component: str
    reason_code: str
    requested_mode: str
    served_mode: str | None = None


class CapabilityGap(BaseModel):
    """Backend capability gap (Phase 3 §11); never recoverable in V1.1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_id: str
    evidence_need: EvidenceNeed
    reason_code: Literal[
        "BACKEND_NOT_IMPLEMENTED",
        "SOURCE_NOT_INDEXED",
        "FILTER_UNSUPPORTED",
    ]
    recoverable_in_current_runtime: Literal[False] = False


class EvidenceStateItem(BaseModel):
    """One canonical evidence item in the cumulative state (Phase 3 §9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    canonical_asset_id: str
    canonical_content_version_id: str
    corpus_document_id: str
    chunk_id: str | None = None
    fact_id: str | None = None
    section_key: str | None = None
    chunk_ordinal: int | None = None
    asset_type: AssetType
    provider: str
    publisher: str | None = None
    canonical_url: str | None = None
    source_class: SourceClass
    evidence_role: str
    eligible_at: datetime
    temporal_precision: str
    content_state: ContentState
    material_capability: MaterialCapability
    serving_status: str
    parse_quality: str
    independence_group_id: str | None = None
    independence_status: IndependenceStatus
    content_hash: str | None = None
    text_ref: str
    excerpt_text: str | None = None
    first_seen_round: int
    contributing_task_ids: tuple[str, ...] = ()
    retrieval_contributions: tuple[RetrievalContribution, ...] = ()

    @model_validator(mode="after")
    def _evidence_identity(self) -> "EvidenceStateItem":
        if (self.chunk_id is None) == (self.fact_id is None):
            raise ValueError("exactly one of chunk_id or fact_id must be set")
        if self.chunk_id is not None and self.evidence_id != self.chunk_id:
            raise ValueError("text evidence_id must equal chunk_id")
        if self.fact_id is not None and self.evidence_id != self.fact_id:
            raise ValueError("structured evidence_id must equal fact_id")
        if self.chunk_ordinal is not None and self.chunk_ordinal < 0:
            raise ValueError("chunk_ordinal must be non-negative")
        return self

    def _immutable_metadata(self) -> tuple[object, ...]:
        return (
            self.canonical_asset_id,
            self.canonical_content_version_id,
            self.corpus_document_id,
            self.chunk_id,
            self.fact_id,
            self.section_key,
            self.chunk_ordinal,
            self.asset_type,
            self.provider,
            self.publisher,
            self.canonical_url,
            self.source_class,
            self.evidence_role,
            self.eligible_at,
            self.temporal_precision,
            self.content_state,
            self.material_capability,
            self.serving_status,
            self.parse_quality,
            self.independence_group_id,
            self.independence_status,
            self.content_hash,
            self.text_ref,
        )


class EvidenceState(BaseModel):
    """Cumulative union of evidence items keyed by evidence_id/fact_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    run_id: str
    round: int
    temporal_identity: TemporalIdentity
    data_runtime_identity: DataRuntimeIdentity
    research_policy_version: str
    task_results: tuple[ResearchTaskResult, ...] = ()
    evidence_items: tuple[EvidenceStateItem, ...] = ()
    structured_facts: tuple[EvidenceStateItem, ...] = ()
    degradations: tuple[RetrievalDegradation, ...] = ()
    capability_gaps: tuple[CapabilityGap, ...] = ()
    state_hash: str

    @model_validator(mode="after")
    def _partition_and_unique_items(self) -> "EvidenceState":
        text_ids = tuple(item.evidence_id for item in self.evidence_items)
        fact_ids = tuple(item.fact_id for item in self.structured_facts)
        if any(item.chunk_id is None for item in self.evidence_items):
            raise ValueError("evidence_items must contain only text chunk items")
        if any(item.fact_id is None for item in self.structured_facts):
            raise ValueError("structured_facts must contain only fact items")
        if len(text_ids) != len(set(text_ids)):
            raise ValueError("evidence_items must have unique evidence IDs")
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("structured_facts must have unique fact IDs")
        if set(text_ids) & set(fact_ids):
            raise ValueError("text evidence and structured facts cannot share IDs")
        return self

    @field_validator("state_hash")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("state_hash must be a lowercase SHA-256 hex digest")
        return value

    def upsert(self, item: EvidenceStateItem) -> "EvidenceState":
        """Return a new state with item merged into the cumulative union.

        Same evidence id merges contributions: contributing task ids and
        retrieval contributions are unioned and the first-seen round is
        preserved. Conflicting immutable metadata raises an integrity failure.
        The returned state carries a recomputed canonical state_hash.
        """
        is_fact = item.fact_id is not None
        collection = self.structured_facts if is_fact else self.evidence_items
        other_collection = self.evidence_items if is_fact else self.structured_facts
        if any(existing.evidence_id == item.evidence_id for existing in other_collection):
            raise ValueError(
                "evidence state integrity failure: evidence ID cannot cross text/fact partitions"
            )
        for index, existing in enumerate(collection):
            if existing.evidence_id != item.evidence_id:
                continue
            if existing._immutable_metadata() != item._immutable_metadata():
                raise ValueError(
                    "evidence state integrity failure: conflicting immutable "
                    f"metadata for evidence_id {item.evidence_id!r}"
                )
            dumped = item.model_dump(
                exclude={
                    "first_seen_round",
                    "contributing_task_ids",
                    "retrieval_contributions",
                }
            )
            merged = EvidenceStateItem(
                **dumped,
                first_seen_round=min(existing.first_seen_round, item.first_seen_round),
                contributing_task_ids=tuple(
                    dict.fromkeys(
                        existing.contributing_task_ids + item.contributing_task_ids
                    )
                ),
                retrieval_contributions=tuple(
                    dict.fromkeys(
                        existing.retrieval_contributions
                        + item.retrieval_contributions
                    )
                ),
            )
            items = list(collection)
            items[index] = merged
            target = "structured_facts" if is_fact else "evidence_items"
            candidate = self._validated_rebuild(**{target: tuple(items)})
            return candidate._validated_rebuild(state_hash=compute_state_hash(candidate))
        target = "structured_facts" if is_fact else "evidence_items"
        candidate = self._validated_rebuild(**{target: collection + (item,)})
        return candidate._validated_rebuild(state_hash=compute_state_hash(candidate))

    def _validated_rebuild(self, **updates: object) -> "EvidenceState":
        payload = self.model_dump(mode="python")
        payload.update(updates)
        return type(self).model_validate(payload)

    def model_copy(
        self, *, update: dict[str, object] | None = None, deep: bool = False
    ) -> "EvidenceState":
        """Permit read copies, but forbid unvalidated authoritative updates."""
        if update:
            raise TypeError("EvidenceState does not permit public model_copy updates")
        return super().model_copy(deep=deep)


def compute_state_hash(state: EvidenceState) -> str:
    """Canonical SHA-256 over the state contents excluding state_hash.

    Deterministic canonical JSON: sorted keys, compact separators, UTF-8.
    """
    payload = state.model_dump(mode="json", exclude={"state_hash"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "CapabilityGap",
    "EvidenceState",
    "EvidenceStateItem",
    "IndependenceStatus",
    "MaterialCapability",
    "ResearchTaskResult",
    "ResearchTaskResultStatus",
    "RetrievalContribution",
    "RetrievalDegradation",
    "compute_state_hash",
]
