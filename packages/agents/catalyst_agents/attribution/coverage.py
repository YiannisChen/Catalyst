"""V1.1 CoverageSummary contract (M2-5, corrective).

Deterministic pre-Analyst availability summary with observable names only
(Phase 3 TSD §11; Final Migration TSD §6.4). Gaps and degradations are typed
records; every count is non-negative. No support/causality/confidence
semantics live here; semantic support begins with the Analyst.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_agents.attribution.evidence_state import (
    CapabilityGap,
    RetrievalDegradation,
)
from catalyst_agents.attribution.move_profile import SessionAlignment
from catalyst_data.canonical.model import ContentState

CANONICAL_CONTENT_STATES: frozenset[str] = frozenset(ContentState.__args__)


class DataCoverageGap(BaseModel):
    """Deterministic data-coverage gap (Phase 3 §11)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_id: str
    field: str
    reason_code: str
    availability: Literal["PARTIAL", "UNAVAILABLE"]
    source_artifact_id: str


class IndependenceGroupSummary(BaseModel):
    """Sorted known independence-group summary (Phase 3 §11)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    independence_group_id: str
    member_asset_ids: tuple[str, ...]
    representative_asset_id: str | None = None

    @model_validator(mode="after")
    def _non_empty_members(self) -> "IndependenceGroupSummary":
        if not self.member_asset_ids:
            raise ValueError("independence group must have at least one member asset")
        if (
            self.representative_asset_id is not None
            and self.representative_asset_id not in self.member_asset_ids
        ):
            raise ValueError("representative asset must be a member asset")
        return self


class ContentStateCount(BaseModel):
    """One immutable canonical content-state count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content_state: ContentState
    count: int

    @model_validator(mode="after")
    def _non_negative(self) -> "ContentStateCount":
        if self.count < 0:
            raise ValueError("content state count must be non-negative")
        return self


class CoverageSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible_item_count: int
    eligible_asset_count: int
    eligible_full_text_item_count: int
    material_capable_item_count: int
    material_capable_asset_count: int
    primary_authority_asset_count: int
    direct_primary_asset_count: int
    reported_news_asset_count: int
    commentary_lead_asset_count: int
    unknown_role_asset_count: int
    eligible_reported_news_group_count: int
    unknown_independence_asset_count: int
    known_duplicate_or_syndicated_asset_count: int
    content_state_counts: tuple[ContentStateCount, ...]
    parse_degraded_item_count: int
    retrieval_degradations: tuple[RetrievalDegradation, ...] = ()
    data_coverage_gaps: tuple[DataCoverageGap, ...] = ()
    capability_gaps: tuple[CapabilityGap, ...] = ()
    independence_groups: tuple[IndependenceGroupSummary, ...] = ()
    market_alignment: SessionAlignment | None = None
    sector_alignment: SessionAlignment | None = None
    peer_alignment: SessionAlignment | None = None
    scheduled_macro_present: bool | None = None

    @field_validator("content_state_counts", mode="before")
    @classmethod
    def _content_state_entries(cls, counts: object) -> object:
        if isinstance(counts, dict):
            return tuple(
                ContentStateCount(content_state=state, count=count)
                for state, count in sorted(counts.items())
            )
        return counts

    @model_validator(mode="after")
    def _counts_non_negative(self) -> "CoverageSummary":
        count_fields = [
            "eligible_item_count",
            "eligible_asset_count",
            "eligible_full_text_item_count",
            "material_capable_item_count",
            "material_capable_asset_count",
            "primary_authority_asset_count",
            "direct_primary_asset_count",
            "reported_news_asset_count",
            "commentary_lead_asset_count",
            "unknown_role_asset_count",
            "eligible_reported_news_group_count",
            "unknown_independence_asset_count",
            "known_duplicate_or_syndicated_asset_count",
            "parse_degraded_item_count",
        ]
        for field in count_fields:
            if getattr(self, field) < 0:
                raise ValueError(f"{field} must be non-negative")
        group_ids = [group.independence_group_id for group in self.independence_groups]
        if group_ids != sorted(group_ids):
            raise ValueError("independence_groups must be sorted by group id")
        content_states = tuple(entry.content_state for entry in self.content_state_counts)
        if len(content_states) != len(set(content_states)):
            raise ValueError("content_state_counts must contain unique states")
        return self


__all__ = [
    "CANONICAL_CONTENT_STATES",
    "CapabilityGap",
    "ContentStateCount",
    "CoverageSummary",
    "DataCoverageGap",
    "IndependenceGroupSummary",
    "RetrievalDegradation",
]
