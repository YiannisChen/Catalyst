"""V1.1 EvidenceState contract (M2-4).

Immutable, run-scoped cumulative union keyed by evidence_id/fact_id
(Final Migration TSD §4/§6.1/§6.4). Merging a contribution for an existing
evidence id unions research task ids, keeps the first-seen round, and fails
integrity validation when immutable metadata conflicts.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from catalyst_data.canonical.model import ContentState, SourceClass

EvidenceDisposition = Literal["accepted", "lead", "rejected"]


class EvidenceStateItem(BaseModel):
    """One canonical evidence item in the cumulative state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    first_seen_round: int
    research_task_ids: tuple[str, ...]
    state: EvidenceDisposition
    canonical_asset_id: str
    content_version_id: str
    chunk_id: str | None = None
    fact_id: str | None = None
    content_state: ContentState
    source_class: SourceClass
    independence_group: str | None = None
    dedup_cluster_id: str | None = None

    @model_validator(mode="after")
    def _evidence_identity(self) -> "EvidenceStateItem":
        if (self.chunk_id is None) == (self.fact_id is None):
            raise ValueError("exactly one of chunk_id or fact_id must be set")
        if self.chunk_id is not None and self.evidence_id != self.chunk_id:
            raise ValueError("text evidence_id must equal chunk_id")
        if self.fact_id is not None and self.evidence_id != self.fact_id:
            raise ValueError("structured evidence_id must equal fact_id")
        return self

    def _immutable_metadata(self) -> tuple[object, ...]:
        return (
            self.canonical_asset_id,
            self.content_version_id,
            self.chunk_id,
            self.fact_id,
            self.content_state,
            self.source_class,
            self.independence_group,
            self.dedup_cluster_id,
        )


class EvidenceState(BaseModel):
    """Cumulative union of evidence items keyed by evidence_id/fact_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[EvidenceStateItem, ...] = ()

    def upsert(self, item: EvidenceStateItem) -> "EvidenceState":
        """Return a new state with item merged into the cumulative union.

        Same evidence id merges contributions: research task ids are unioned,
        the first-seen round is preserved, and the existing disposition is
        kept. Conflicting immutable metadata raises an integrity failure.
        """
        for index, existing in enumerate(self.items):
            if existing.evidence_id != item.evidence_id:
                continue
            if existing._immutable_metadata() != item._immutable_metadata():
                raise ValueError(
                    "evidence state integrity failure: conflicting immutable "
                    f"metadata for evidence_id {item.evidence_id!r}"
                )
            dumped = item.model_dump(exclude={"first_seen_round", "research_task_ids", "state"})
            merged = EvidenceStateItem(
                **dumped,
                first_seen_round=min(existing.first_seen_round, item.first_seen_round),
                research_task_ids=tuple(
                    dict.fromkeys(existing.research_task_ids + item.research_task_ids)
                ),
                state=existing.state,
            )
            items = list(self.items)
            items[index] = merged
            return EvidenceState(items=tuple(items))
        return EvidenceState(items=self.items + (item,))


__all__ = ["EvidenceDisposition", "EvidenceState", "EvidenceStateItem"]
