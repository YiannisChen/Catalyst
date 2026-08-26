"""M4-5: observable coverage summary builder (Final TSD §6.4; Phase 3 §11)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalyst_agents.attribution.coverage import (
    ContentStateCount,
    CoverageSummary,
    DataCoverageGap,
    IndependenceGroupSummary,
)
from catalyst_agents.attribution.coverage_builder import build_coverage_summary
from catalyst_agents.attribution.evidence_state import (
    CapabilityGap,
    EvidenceState,
    EvidenceStateItem,
    RetrievalDegradation,
)
from catalyst_agents.attribution.move_profile import (
    AlignmentBand,
    AvailabilityState,
    DirectionRelation,
    MoveProfile,
    PeerReturn,
    PeerSummary,
    SessionAlignment,
    VolumeAbnormality,
    VolumeBand,
)
from catalyst_agents.retrieval.task import EvidenceNeed
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-15",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-15T14:30:00Z"),
        session_close_at=_utc("2026-01-15T21:00:00Z"),
        information_window_start_at=_utc("2026-01-14T21:00:00Z"),
        cutoff_at=_utc("2026-01-15T21:00:00Z"),
    )


def _runtime() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id="m" * 64,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _item(
    evidence_id: str,
    *,
    asset_id: str,
    source_class: str,
    evidence_role: str,
    content_state: str = "FULL_TEXT",
    material_capability: str = "MATERIAL_CAPABLE",
    parse_quality: str = "full",
    independence_group_id: str | None = None,
    independence_status: str = "UNKNOWN",
    eligible_at: str = "2026-01-15T10:00:00Z",
    chunk_id: str | None = None,
) -> EvidenceStateItem:
    is_fact = evidence_id.startswith("fact:")
    return EvidenceStateItem(
        evidence_id=evidence_id,
        canonical_asset_id=asset_id,
        canonical_content_version_id=f"version:{evidence_id}",
        corpus_document_id=f"doc:{evidence_id}",
        chunk_id=chunk_id if chunk_id is not None else (None if is_fact else evidence_id),
        fact_id=evidence_id if is_fact else None,
        section_key="body" if not is_fact else None,
        chunk_ordinal=1 if not is_fact else None,
        asset_type="STRUCTURED_CONTEXT" if is_fact else "NEWS",
        provider="polygon",
        publisher=None,
        canonical_url=None,
        source_class=source_class,
        evidence_role=evidence_role,
        eligible_at=_utc(eligible_at),
        temporal_precision="publication_time",
        content_state=content_state,
        material_capability=material_capability,
        serving_status="body_candidate",
        parse_quality=parse_quality,
        independence_group_id=independence_group_id,
        independence_status=independence_status,
        content_hash="c" * 64,
        text_ref=evidence_id,
        excerpt_text="excerpt",
        first_seen_round=1,
        contributing_task_ids=("t:1",),
        retrieval_contributions=(),
    )


def _state(items: tuple[EvidenceStateItem, ...], *, facts: tuple[EvidenceStateItem, ...] = ()) -> EvidenceState:
    return EvidenceState(
        schema_version="evidence_state_v1",
        run_id="run:1",
        round=1,
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        research_policy_version="sp:v1",
        task_results=(),
        evidence_items=items,
        structured_facts=facts,
        degradations=(),
        capability_gaps=(),
        state_hash="0" * 64,
    )


def _move_profile(**overrides) -> MoveProfile:
    base = dict(
        target_return=3.0,
        prior_session_return=2.0,
        gap_return=0.5,
        market_return=2.9,
        sector_return=2.8,
        peer_summary=PeerSummary(
            schema_version="peer_summary_v1",
            expected_peer_count=2,
            available_peer_count=2,
            peer_returns=(
                PeerReturn(ticker="MSFT", return_pct=2.8),
                PeerReturn(ticker="NVDA", return_pct=3.0),
            ),
            median_return_pct=2.9,
            target_minus_median_pct=0.1,
            coverage_state=AvailabilityState.AVAILABLE,
        ),
        market_adjusted_return=0.1,
        sector_adjusted_return=0.2,
        volume_abnormality=VolumeAbnormality(
            schema_version="volume_abnormality_v1",
            target_volume=1000.0,
            baseline_median_volume=1000.0,
            ratio=1.0,
            expected_session_count=20,
            valid_session_count=12,
            band=VolumeBand.NORMAL,
            availability=AvailabilityState.AVAILABLE,
        ),
        scheduled_macro_flags=(),
        market_comove=SessionAlignment(
            schema_version="session_alignment_v1",
            reference_kind="MARKET",
            target_return_pct=3.0,
            reference_return_pct=2.9,
            residual_return_pct=0.1,
            direction_relation=DirectionRelation.SAME_DIRECTION,
            band=AlignmentBand.ALIGNED,
            availability=AvailabilityState.AVAILABLE,
        ),
        sector_comove=SessionAlignment(
            schema_version="session_alignment_v1",
            reference_kind="SECTOR",
            target_return_pct=3.0,
            reference_return_pct=2.8,
            residual_return_pct=0.2,
            direction_relation=DirectionRelation.SAME_DIRECTION,
            band=AlignmentBand.ALIGNED,
            availability=AvailabilityState.AVAILABLE,
        ),
        peer_comove=SessionAlignment(
            schema_version="session_alignment_v1",
            reference_kind="PEER_MEDIAN",
            target_return_pct=3.0,
            reference_return_pct=2.9,
            residual_return_pct=0.1,
            direction_relation=DirectionRelation.SAME_DIRECTION,
            band=AlignmentBand.ALIGNED,
            availability=AvailabilityState.AVAILABLE,
        ),
        coverage_flags=(),
        degraded_fields=(),
    )
    base.update(overrides)
    return MoveProfile(**base)


def test_coverage_counts_distinct_assets_by_role():
    items = (
        _item("chunk:a1", asset_id="asset:disclosure", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY"),
        _item("chunk:a2", asset_id="asset:disclosure", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY"),
        _item("chunk:b1", asset_id="asset:gov", source_class="official_government", evidence_role="PRIMARY_AUTHORITY"),
        _item("chunk:n1", asset_id="asset:news1", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              independence_group_id="g:1", independence_status="KNOWN_GROUP"),
        _item("chunk:n2", asset_id="asset:news2", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              independence_group_id="g:1", independence_status="KNOWN_GROUP"),
        _item("chunk:c1", asset_id="asset:commentary", source_class="analysis_opinion", evidence_role="COMMENTARY_LEAD"),
    )
    summary = build_coverage_summary(
        _state(items), _move_profile(), (), ()
    )
    assert summary.eligible_item_count == 6
    assert summary.eligible_asset_count == 5
    assert summary.material_capable_item_count == 6
    assert summary.material_capable_asset_count == 5
    assert summary.direct_primary_asset_count == 1
    assert summary.primary_authority_asset_count == 1
    assert summary.reported_news_asset_count == 2
    assert summary.commentary_lead_asset_count == 1
    assert summary.unknown_role_asset_count == 0
    assert summary.eligible_reported_news_group_count == 1
    assert summary.unknown_independence_asset_count == 0
    assert summary.known_duplicate_or_syndicated_asset_count == 1


def test_coverage_unknown_lineage_excluded_from_known_groups():
    items = (
        _item("chunk:n1", asset_id="asset:news1", source_class="reported_news", evidence_role="INDEPENDENT_REPORT"),
        _item("chunk:n2", asset_id="asset:news2", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              independence_group_id="g:9", independence_status="KNOWN_GROUP"),
    )
    summary = build_coverage_summary(_state(items), _move_profile(), (), ())
    assert summary.eligible_reported_news_group_count == 1
    assert summary.unknown_independence_asset_count == 1
    assert summary.reported_news_asset_count == 2


def test_coverage_content_state_counts_and_parse_degraded():
    items = (
        _item("chunk:a1", asset_id="asset:a", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY"),
        _item("chunk:b1", asset_id="asset:b", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              content_state="TITLE_ONLY", material_capability="LEAD_ONLY", parse_quality="degraded"),
        _item("chunk:c1", asset_id="asset:c", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              content_state="METADATA_ONLY", material_capability="NOT_CAPABLE"),
    )
    summary = build_coverage_summary(_state(items), _move_profile(), (), ())
    counts = {entry.content_state: entry.count for entry in summary.content_state_counts}
    assert counts["FULL_TEXT"] == 1
    assert counts["TITLE_ONLY"] == 1
    assert counts["METADATA_ONLY"] == 1
    assert summary.parse_degraded_item_count == 1
    # Parse degradation does not rewrite materiality.
    assert summary.material_capable_item_count == 1


def test_coverage_structured_facts_and_eligible_filter():
    text = _item("chunk:a1", asset_id="asset:a", source_class="issuer_disclosure", evidence_role="DIRECT_PRIMARY")
    late = _item("chunk:late", asset_id="asset:late", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
                 eligible_at="2026-01-16T10:00:00Z")
    fact = _item("fact:1", asset_id="asset:fact", source_class="structured_market_data", evidence_role="STRUCTURED_CONTEXT")
    summary = build_coverage_summary(
        _state((text, late), facts=(fact,)), _move_profile(), (), ()
    )
    # Post-cutoff item excluded from eligibility counts.
    assert summary.eligible_item_count == 2  # text + fact
    assert summary.eligible_asset_count == 2


def test_coverage_retrieval_and_capability_gaps_flow_through():
    degradation = RetrievalDegradation(
        task_id="t:1", component="retrieval", reason_code="retrieval_backend_unavailable",
        requested_mode="reranked", served_mode=None,
    )
    gap = CapabilityGap(
        gap_id="cap:macro_event", evidence_need=EvidenceNeed.MACRO_EVENT,
        reason_code="BACKEND_NOT_IMPLEMENTED",
    )
    summary = build_coverage_summary(
        _state(()), _move_profile(), (degradation,), (gap,)
    )
    assert summary.retrieval_degradations == (degradation,)
    assert summary.capability_gaps == (gap,)


def test_coverage_data_gaps_from_move_profile():
    profile = _move_profile(
        market_return=None,
        coverage_flags=("benchmark_unavailable", "sector_unavailable", "scheduled_macro_source_unavailable"),
        market_comove=SessionAlignment(
            schema_version="session_alignment_v1", reference_kind="MARKET",
            target_return_pct=None, reference_return_pct=None, residual_return_pct=None,
            direction_relation=DirectionRelation.UNKNOWN, band=AlignmentBand.UNKNOWN,
            availability=AvailabilityState.UNAVAILABLE,
            reason_codes=("alignment_reference_unavailable",),
        ),
        sector_comove=None,
    )
    summary = build_coverage_summary(_state(()), profile, (), ())
    fields = {gap.field for gap in summary.data_coverage_gaps}
    assert "market_return" in fields
    assert "sector_return" in fields
    assert "scheduled_macro_flags" in fields
    assert summary.market_alignment is not None
    assert summary.market_alignment.band == AlignmentBand.UNKNOWN
    assert summary.sector_alignment is None
    assert summary.scheduled_macro_present is None


def test_coverage_scheduled_macro_present_semantics():
    from catalyst_agents.attribution.move_profile import ScheduledMacroFlag

    profile_yes = _move_profile(scheduled_macro_flags=(ScheduledMacroFlag(name="FOMC", scheduled_at=None),))
    assert build_coverage_summary(_state(()), profile_yes, (), ()).scheduled_macro_present is True

    profile_no = _move_profile(scheduled_macro_flags=())
    assert build_coverage_summary(_state(()), profile_no, (), ()).scheduled_macro_present is False

    profile_unavailable = _move_profile(coverage_flags=("scheduled_macro_source_unavailable",))
    assert build_coverage_summary(_state(()), profile_unavailable, (), ()).scheduled_macro_present is None


def test_coverage_independence_groups_are_sorted_with_representative():
    items = (
        _item("chunk:n2", asset_id="asset:news2", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              independence_group_id="g:2", independence_status="KNOWN_GROUP"),
        _item("chunk:n1", asset_id="asset:news1", source_class="reported_news", evidence_role="INDEPENDENT_REPORT",
              independence_group_id="g:1", independence_status="KNOWN_GROUP"),
    )
    summary = build_coverage_summary(_state(items), _move_profile(), (), ())
    groups = summary.independence_groups
    assert [group.independence_group_id for group in groups] == ["g:1", "g:2"]
    assert all(isinstance(group, IndependenceGroupSummary) for group in groups)
    assert groups[0].representative_asset_id == "asset:news1"


def test_coverage_has_no_semantic_support_fields():
    forbidden = {
        "support", "contradiction", "explanation", "confidence", "causality",
        "hypothesis", "semantic_relevance", "corroborat", "magnitude_fit",
    }
    fields = set(CoverageSummary.model_fields)
    for name in forbidden:
        assert not any(name in field for field in fields), name
