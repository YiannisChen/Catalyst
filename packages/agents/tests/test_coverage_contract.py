"""V1.1 CoverageSummary contract tests (M2-5, corrective).

Phase 3 TSD §11 / Final Migration TSD §6.4: observable availability counts
only; typed gap/degradation records; no support/causality/confidence
semantics may appear in the pre-Analyst summary.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.coverage import (
    CapabilityGap,
    CoverageSummary,
    DataCoverageGap,
    IndependenceGroupSummary,
)
from catalyst_agents.attribution.evidence_state import RetrievalDegradation
from catalyst_agents.attribution.move_profile import (
    PeerSummary,
    SessionAlignment,
    VolumeAbnormality,
)


def _peer_summary(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "expected_peer_count": 2,
        "available_peer_count": 2,
        "peer_returns": (
            {"ticker": "MSFT", "return_pct": 0.5},
            {"ticker": "NVDA", "return_pct": 1.25},
        ),
        "median_return_pct": 0.875,
        "target_minus_median_pct": 2.325,
        "coverage_state": "AVAILABLE",
        "reason_codes": (),
    }
    base.update(overrides)
    return base


def _volume(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "target_volume": 9_500_000,
        "baseline_median_volume": 5_000_000,
        "ratio": 1.9,
        "expected_session_count": 20,
        "valid_session_count": 20,
        "band": "ELEVATED",
        "availability": "AVAILABLE",
        "reason_codes": (),
    }
    base.update(overrides)
    return base


def _coverage(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "eligible_item_count": 40,
        "eligible_asset_count": 25,
        "eligible_full_text_item_count": 12,
        "material_capable_item_count": 10,
        "material_capable_asset_count": 8,
        "primary_authority_asset_count": 1,
        "direct_primary_asset_count": 2,
        "reported_news_asset_count": 3,
        "commentary_lead_asset_count": 1,
        "unknown_role_asset_count": 1,
        "eligible_reported_news_group_count": 2,
        "unknown_independence_asset_count": 1,
        "known_duplicate_or_syndicated_asset_count": 4,
        "content_state_counts": {
            "FULL_TEXT": 12,
            "TITLE_ONLY": 3,
            "METADATA_ONLY": 20,
            "EMPTY": 0,
            "FAILED": 5,
        },
        "parse_degraded_item_count": 3,
        "retrieval_degradations": (),
        "data_coverage_gaps": (),
        "capability_gaps": (),
        "independence_groups": (),
        "market_alignment": SessionAlignment(
            schema_version="1.0",
            reference_kind="MARKET",
            target_return_pct=3.2,
            reference_return_pct=0.8,
            residual_return_pct=2.4,
            direction_relation="SAME_DIRECTION",
            band="ALIGNED",
            availability="AVAILABLE",
            reason_codes=(),
        ),
        "sector_alignment": None,
        "peer_alignment": None,
        "scheduled_macro_present": None,
    }
    base.update(overrides)
    return base


def test_coverage_summary_uses_exactly_final_tsd_6_4_observable_names() -> None:
    summary = CoverageSummary(**_coverage())
    assert set(CoverageSummary.model_fields) == {
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
        "content_state_counts",
        "parse_degraded_item_count",
        "retrieval_degradations",
        "data_coverage_gaps",
        "capability_gaps",
        "independence_groups",
        "market_alignment",
        "sector_alignment",
        "peer_alignment",
        "scheduled_macro_present",
    }


def test_coverage_summary_is_strict_and_frozen() -> None:
    summary = CoverageSummary(**_coverage())
    with pytest.raises(ValidationError):
        summary.eligible_item_count = 41  # frozen
    with pytest.raises(ValidationError):
        CoverageSummary(**_coverage(), unknown_field=True)  # extra forbidden


def test_schema_negative_no_support_causality_or_confidence_fields() -> None:
    field_names = set(CoverageSummary.model_fields)
    assert "support" not in field_names
    assert "causality" not in field_names
    assert "primary_cause" not in field_names
    assert "confidence" not in field_names


def test_content_state_counts_accept_only_canonical_states() -> None:
    with pytest.raises(ValidationError):
        CoverageSummary(
            **_coverage(content_state_counts={"FULL": 1})
        )
    summary = CoverageSummary(**_coverage())
    counts = {entry.content_state: entry.count for entry in summary.content_state_counts}
    assert counts["FULL_TEXT"] == 12
    assert counts["METADATA_ONLY"] == 20
    with pytest.raises((TypeError, AttributeError, ValidationError)):
        summary.content_state_counts[0].count = -1
    dumped = summary.model_dump()
    assert dumped["content_state_counts"] == {
        "EMPTY": 0,
        "FAILED": 5,
        "FULL_TEXT": 12,
        "METADATA_ONLY": 20,
        "TITLE_ONLY": 3,
    }
    assert (
        json.loads(summary.model_dump_json())["content_state_counts"]
        == dumped["content_state_counts"]
    )
    assert CoverageSummary.model_validate(dumped).model_dump() == dumped


def test_content_state_counts_normalize_entries_and_reject_duplicates() -> None:
    summary = CoverageSummary(**_coverage(content_state_counts=[
        {"content_state": "TITLE_ONLY", "count": 3},
        {"content_state": "FULL_TEXT", "count": 12},
    ]))
    assert tuple(entry.content_state for entry in summary.content_state_counts) == (
        "FULL_TEXT", "TITLE_ONLY",
    )
    with pytest.raises(ValidationError):
        CoverageSummary(**_coverage(content_state_counts=[
            {"content_state": "FULL_TEXT", "count": 1},
            {"content_state": "FULL_TEXT", "count": 2},
        ]))
    with pytest.raises(TypeError):
        summary.model_copy(update={"content_state_counts": {"FULL_TEXT": -1}})
    with pytest.raises(TypeError):
        summary.content_state_counts[0].model_copy(update={"count": -1})
    assert summary.model_copy() == summary


def test_all_counts_are_non_negative() -> None:
    with pytest.raises(ValidationError):
        CoverageSummary(**_coverage(eligible_item_count=-1))
    with pytest.raises(ValidationError):
        CoverageSummary(**_coverage(parse_degraded_item_count=-3))
    with pytest.raises(ValidationError):
        CoverageSummary(**_coverage(eligible_reported_news_group_count=-2))


def test_typed_gap_and_degradation_records() -> None:
    data_gap = DataCoverageGap(
        gap_id="gap:1",
        field="peer_proxy",
        reason_code="PEER_PROXY_UNAVAILABLE",
        availability="UNAVAILABLE",
        source_artifact_id="observation:1",
    )
    capability_gap = CapabilityGap(
        gap_id="gap:2",
        evidence_need="MARKET_STRUCTURE",
        reason_code="BACKEND_NOT_IMPLEMENTED",
    )
    degradation = RetrievalDegradation(
        task_id="task-1",
        component="dense",
        reason_code="dense_unavailable",
        requested_mode="hybrid",
        served_mode=None,
    )
    summary = CoverageSummary(
        **_coverage(
            retrieval_degradations=(degradation,),
            data_coverage_gaps=(data_gap,),
            capability_gaps=(capability_gap,),
        )
    )
    assert summary.data_coverage_gaps[0].availability == "UNAVAILABLE"
    assert summary.capability_gaps[0].recoverable_in_current_runtime is False
    assert summary.retrieval_degradations[0].component == "dense"


def test_capability_gap_is_never_recoverable() -> None:
    with pytest.raises(ValidationError):
        CapabilityGap(
            gap_id="gap:2",
            evidence_need="MARKET_STRUCTURE",
            reason_code="BACKEND_NOT_IMPLEMENTED",
            recoverable_in_current_runtime=True,
        )


def test_independence_group_summary_is_typed() -> None:
    group = IndependenceGroupSummary(
        independence_group_id="syndication:g1",
        member_asset_ids=("asset:1", "asset:2"),
        representative_asset_id="asset:1",
    )
    assert group.representative_asset_id == "asset:1"
    with pytest.raises(ValidationError):
        IndependenceGroupSummary(
            independence_group_id="syndication:g1",
            member_asset_ids=(),
            representative_asset_id="asset:1",
        )


def test_independence_groups_must_be_sorted() -> None:
    from catalyst_agents.attribution.coverage import IndependenceGroupSummary

    with pytest.raises(ValidationError):
        CoverageSummary(
            **_coverage(
                independence_groups=(
                    IndependenceGroupSummary(
                        independence_group_id="syndication:g2",
                        member_asset_ids=("asset:3",),
                    ),
                    IndependenceGroupSummary(
                        independence_group_id="syndication:g1",
                        member_asset_ids=("asset:1",),
                    ),
                )
            )
        )


def test_peer_and_volume_values_reject_nan_inf_and_negative_ratio() -> None:
    with pytest.raises(ValidationError):
        PeerSummary(**_peer_summary(median_return_pct=float("nan")))
    with pytest.raises(ValidationError):
        VolumeAbnormality(**_volume(ratio=float("inf")))
    with pytest.raises(ValidationError):
        VolumeAbnormality(**_volume(ratio=-1.0))


def test_alignments_are_typed_session_alignment_and_scheduled_macro_is_nullable() -> None:
    summary = CoverageSummary(**_coverage())
    assert summary.market_alignment.band == "ALIGNED"
    assert summary.sector_alignment is None
    assert summary.scheduled_macro_present is None
    summary = CoverageSummary(**_coverage(scheduled_macro_present=True))
    assert summary.scheduled_macro_present is True
