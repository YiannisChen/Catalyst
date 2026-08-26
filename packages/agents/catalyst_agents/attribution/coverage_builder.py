"""M4-5: observable CoverageSummary builder (Final TSD §6.4; Phase 3 §11).

Deterministic and pre-semantic: counts eligible items/assets, material-capable
items/assets, distinct assets by source role, known independence groups,
unknown-lineage assets, duplicate/syndicated excess members, exact
content-state counts, parse-degraded items, retrieval degradations,
data-coverage and capability gaps, typed market/sector/peer alignment, and
scheduled-macro availability. It never claims support, contradiction,
explanation, confidence, causality, hypothesis, or semantic relevance.
"""
from __future__ import annotations

from catalyst_agents.attribution.coverage import (
    CANONICAL_CONTENT_STATES,
    CoverageSummary,
    DataCoverageGap,
    IndependenceGroupSummary,
)
from catalyst_agents.attribution.evidence_state import (
    CapabilityGap,
    EvidenceState,
    EvidenceStateItem,
    RetrievalDegradation,
)
from catalyst_agents.attribution.move_profile import MoveProfile

COVERAGE_SCHEMA_VERSION = "coverage_summary_v1"
_SOURCE_ARTIFACT_ID = "move_profile"

_ROLE_FIELD = {
    "PRIMARY_AUTHORITY": "primary_authority_asset_count",
    "DIRECT_PRIMARY": "direct_primary_asset_count",
    "INDEPENDENT_REPORT": "reported_news_asset_count",
    "COMMENTARY_LEAD": "commentary_lead_asset_count",
    "UNKNOWN": "unknown_role_asset_count",
}


def _eligible_items(state: EvidenceState) -> tuple[EvidenceStateItem, ...]:
    return tuple(
        sorted(
            (
                item
                for item in state.evidence_items
                if item.eligible_at <= state.temporal_identity.cutoff_at
            ),
            key=lambda item: item.evidence_id,
        )
    )


def _eligible_facts(state: EvidenceState) -> tuple[EvidenceStateItem, ...]:
    return tuple(
        sorted(
            (
                item
                for item in state.structured_facts
                if item.eligible_at <= state.temporal_identity.cutoff_at
            ),
            key=lambda item: item.evidence_id,
        )
    )


def _role_counts(items: tuple[EvidenceStateItem, ...]) -> dict[str, int]:
    counts: dict[str, int] = {field: 0 for field in _ROLE_FIELD.values()}
    assets: dict[str, set[str]] = {}
    for item in items:
        field = _ROLE_FIELD.get(item.evidence_role)
        if field is None:
            continue
        assets.setdefault(field, set()).add(item.canonical_asset_id)
    for field, asset_set in assets.items():
        counts[field] = len(asset_set)
    return counts


def _independence(items: tuple[EvidenceStateItem, ...]) -> dict[str, object]:
    groups: dict[str, set[str]] = {}
    unknown_assets: set[str] = set()
    for item in items:
        if item.evidence_role != "INDEPENDENT_REPORT":
            continue
        if item.independence_status == "KNOWN_GROUP" and item.independence_group_id:
            groups.setdefault(item.independence_group_id, set()).add(
                item.canonical_asset_id
            )
        else:
            unknown_assets.add(item.canonical_asset_id)
    excess = sum(max(0, len(members) - 1) for members in groups.values())
    summaries = tuple(
        IndependenceGroupSummary(
            independence_group_id=group_id,
            member_asset_ids=tuple(sorted(members)),
            representative_asset_id=sorted(members)[0] if members else None,
        )
        for group_id, members in sorted(groups.items())
    )
    return {
        "groups": summaries,
        "group_count": len(groups),
        "unknown_asset_count": len(unknown_assets),
        "excess_count": excess,
    }


def _content_state_counts(
    items: tuple[EvidenceStateItem, ...],
) -> tuple[tuple[str, int], ...]:
    counts = {state: 0 for state in CANONICAL_CONTENT_STATES}
    for item in items:
        counts[item.content_state] = counts.get(item.content_state, 0) + 1
    return tuple((state, counts[state]) for state in sorted(counts))


def _material_counts(items: tuple[EvidenceStateItem, ...]) -> tuple[int, int]:
    material_items = [
        item for item in items if item.material_capability == "MATERIAL_CAPABLE"
    ]
    assets = {item.canonical_asset_id for item in material_items}
    return len(material_items), len(assets)


def _data_coverage_gaps(profile: MoveProfile) -> tuple[DataCoverageGap, ...]:
    gaps: list[DataCoverageGap] = []
    for flag in profile.coverage_flags:
        mapping = {
            "target_return_unavailable": ("target_return", "UNAVAILABLE"),
            "benchmark_unavailable": ("market_return", "UNAVAILABLE"),
            "sector_unavailable": ("sector_return", "UNAVAILABLE"),
            "peer_returns_unavailable": ("peer_summary", "UNAVAILABLE"),
            "scheduled_macro_source_unavailable": ("scheduled_macro_flags", "UNAVAILABLE"),
        }
        entry = mapping.get(flag)
        if entry is None:
            continue
        field, availability = entry
        gaps.append(
            DataCoverageGap(
                gap_id=f"data:{field}",
                field=field,
                reason_code=flag,
                availability=availability,
                source_artifact_id=_SOURCE_ARTIFACT_ID,
            )
        )
    if (
        profile.peer_summary is not None
        and profile.peer_summary.coverage_state.value == "PARTIAL"
    ):
        gaps.append(
            DataCoverageGap(
                gap_id="data:peer_summary",
                field="peer_summary",
                reason_code="peer_returns_partial",
                availability="PARTIAL",
                source_artifact_id=_SOURCE_ARTIFACT_ID,
            )
        )
    return tuple(
        sorted(gaps, key=lambda gap: (gap.field, gap.reason_code))
    )


def build_coverage_summary(
    evidence_state: EvidenceState,
    move_profile: MoveProfile,
    retrieval_degradations: tuple[RetrievalDegradation, ...] | tuple[RetrievalDegradation, ...],
    capability_gaps: tuple[CapabilityGap, ...] | tuple[CapabilityGap, ...],
) -> CoverageSummary:
    """Build the deterministic pre-Analyst coverage summary."""
    items = _eligible_items(evidence_state)
    facts = _eligible_facts(evidence_state)
    all_eligible = items + facts
    asset_ids = {item.canonical_asset_id for item in all_eligible}
    full_text_items = tuple(
        item for item in items if item.content_state == "FULL_TEXT"
    )
    material_items, material_assets = _material_counts(items)
    role_counts = _role_counts(items)
    independence = _independence(items)
    state_counts = _content_state_counts(items)
    parse_degraded = sum(
        1 for item in items if item.parse_quality in {"degraded", "failed"}
    )
    data_gaps = _data_coverage_gaps(move_profile)

    macro_source_unavailable = (
        "scheduled_macro_source_unavailable" in move_profile.coverage_flags
    )
    if macro_source_unavailable:
        scheduled_macro_present: bool | None = None
    else:
        scheduled_macro_present = bool(move_profile.scheduled_macro_flags)

    return CoverageSummary(
        eligible_item_count=len(all_eligible),
        eligible_asset_count=len(asset_ids),
        eligible_full_text_item_count=len(full_text_items),
        material_capable_item_count=material_items,
        material_capable_asset_count=material_assets,
        primary_authority_asset_count=role_counts["primary_authority_asset_count"],
        direct_primary_asset_count=role_counts["direct_primary_asset_count"],
        reported_news_asset_count=role_counts["reported_news_asset_count"],
        commentary_lead_asset_count=role_counts["commentary_lead_asset_count"],
        unknown_role_asset_count=role_counts["unknown_role_asset_count"],
        eligible_reported_news_group_count=int(independence["group_count"]),
        unknown_independence_asset_count=int(independence["unknown_asset_count"]),
        known_duplicate_or_syndicated_asset_count=int(independence["excess_count"]),
        content_state_counts={
            state: count for state, count in state_counts
        },
        parse_degraded_item_count=parse_degraded,
        retrieval_degradations=tuple(
            sorted(
                {degradation: None for degradation in retrieval_degradations}.keys(),
                key=lambda d: (d.task_id, d.component, d.reason_code),
            )
        ),
        data_coverage_gaps=data_gaps,
        capability_gaps=tuple(
            sorted(
                {gap: None for gap in capability_gaps}.keys(),
                key=lambda gap: (gap.evidence_need.value, gap.reason_code),
            )
        ),
        independence_groups=tuple(independence["groups"]),
        market_alignment=move_profile.market_comove,
        sector_alignment=move_profile.sector_comove,
        peer_alignment=move_profile.peer_comove,
        scheduled_macro_present=scheduled_macro_present,
    )


__all__ = ["build_coverage_summary"]
