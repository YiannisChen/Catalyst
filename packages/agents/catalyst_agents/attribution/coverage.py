"""V1.1 CoverageSummary contract (M2-5).

Deterministic pre-Analyst availability summary with observable names only
(Final Migration TSD §6.4). No support/causality/confidence semantics live
here; semantic support begins with the Analyst.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from catalyst_agents.attribution.move_profile import SessionAlignment
from catalyst_data.canonical.model import ContentState

CANONICAL_CONTENT_STATES: frozenset[str] = frozenset(ContentState.__args__)


class CoverageSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible_item_count: int
    eligible_asset_count: int
    eligible_full_text_item_count: int
    material_capable_item_count: int
    material_capable_asset_count: int
    primary_authority_asset_count: int
    direct_primary_asset_count: int
    independent_report_asset_count: int
    commentary_lead_asset_count: int
    unknown_role_asset_count: int
    eligible_reported_news_group_count: int
    unknown_independence_asset_count: int
    duplicate_or_syndicated_asset_count: int
    content_state_counts: dict[str, int]
    parse_degraded_count: int
    retrieval_degradations: tuple[str, ...] = ()
    data_coverage_gaps: tuple[str, ...] = ()
    capability_gaps: tuple[str, ...] = ()
    market_alignment: SessionAlignment | None = None
    sector_alignment: SessionAlignment | None = None
    peer_alignment: SessionAlignment | None = None
    scheduled_macro_present: bool | None = None

    @field_validator("content_state_counts")
    @classmethod
    def _content_state_keys(cls, counts: dict[str, int]) -> dict[str, int]:
        unknown = set(counts) - CANONICAL_CONTENT_STATES
        if unknown:
            raise ValueError(f"unknown content state keys: {sorted(unknown)}")
        return counts


__all__ = ["CANONICAL_CONTENT_STATES", "CoverageSummary"]
