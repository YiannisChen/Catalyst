"""V1.1 MoveProfile typed contract tests (M2-3, corrective).

Phase 3 TSD §4 exact shared enums and nested shapes; no invented percentile
fields. Schemas only in M2; classification formulas land in M4. Unknown and
degraded fields stay explicit and are never inferred.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.move_profile import (
    AlignmentBand,
    AvailabilityState,
    CoverageFlag,
    DegradedField,
    DirectionRelation,
    MoveProfile,
    PeerReturn,
    PeerSummary,
    ScheduledMacroFlag,
    ScenarioPredicateInputs,
    SessionAlignment,
    VolumeAbnormality,
    VolumeBand,
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


def _alignment(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "reference_kind": "MARKET",
        "target_return_pct": 3.2,
        "reference_return_pct": 0.8,
        "residual_return_pct": 2.4,
        "direction_relation": "SAME_DIRECTION",
        "band": "ALIGNED",
        "availability": "AVAILABLE",
        "reason_codes": (),
    }
    base.update(overrides)
    return base


def test_shared_enums_are_exact_phase_3_values() -> None:
    assert [a.value for a in AvailabilityState] == ["AVAILABLE", "PARTIAL", "UNAVAILABLE"]
    assert [b.value for b in AlignmentBand] == ["ALIGNED", "DIVERGENT", "UNKNOWN"]
    assert [d.value for d in DirectionRelation] == [
        "SAME_DIRECTION",
        "OPPOSITE_DIRECTION",
        "TARGET_FLAT",
        "REFERENCE_FLAT",
        "UNKNOWN",
    ]
    assert [v.value for v in VolumeBand] == ["NORMAL", "ELEVATED", "EXTREME", "UNKNOWN"]


def test_peer_summary_carries_schema_counts_bounded_returns_and_reason_codes() -> None:
    summary = PeerSummary(**_peer_summary())
    assert summary.schema_version == "1.0"
    assert summary.expected_peer_count == 2
    assert summary.available_peer_count == 2
    assert [p.ticker for p in summary.peer_returns] == ["MSFT", "NVDA"]
    assert summary.median_return_pct == 0.875
    assert summary.target_minus_median_pct == 2.325
    assert summary.coverage_state == "AVAILABLE"
    assert summary.reason_codes == ()


def test_peer_summary_rejects_impossible_counts_and_unsorted_returns() -> None:
    with pytest.raises(ValidationError):
        PeerSummary(**_peer_summary(available_peer_count=3))  # > expected
    with pytest.raises(ValidationError):
        PeerSummary(**_peer_summary(expected_peer_count=-1))
    with pytest.raises(ValidationError):
        PeerSummary(
            **_peer_summary(
                peer_returns=(
                    {"ticker": "NVDA", "return_pct": 1.25},
                    {"ticker": "MSFT", "return_pct": 0.5},
                )
            )
        )  # not sorted by ticker


def test_peer_summary_rejects_invented_percentile_fields() -> None:
    assert "pct_25_return_pct" not in PeerSummary.model_fields
    assert "pct_75_return_pct" not in PeerSummary.model_fields
    with pytest.raises(ValidationError):
        PeerSummary(**_peer_summary(pct_25_return_pct=-0.5))


def test_volume_abnormality_full_contract() -> None:
    volume = VolumeAbnormality(**_volume())
    assert volume.schema_version == "1.0"
    assert volume.target_volume == 9_500_000
    assert volume.baseline_median_volume == 5_000_000
    assert volume.ratio == 1.9
    assert volume.expected_session_count == 20
    assert volume.valid_session_count == 20
    assert volume.band == "ELEVATED"
    assert volume.availability == "AVAILABLE"
    for value in ("NORMAL", "ELEVATED", "EXTREME", "UNKNOWN"):
        assert VolumeAbnormality(**_volume(band=value)).band == value
    with pytest.raises(ValidationError):
        VolumeAbnormality(**_volume(band="HIGH"))


def test_volume_abnormality_never_becomes_normal_when_unavailable() -> None:
    with pytest.raises(ValidationError):
        VolumeAbnormality(**_volume(availability="UNAVAILABLE", band="NORMAL"))
    unavailable = VolumeAbnormality(
        **_volume(availability="UNAVAILABLE", band="UNKNOWN", ratio=None)
    )
    assert unavailable.ratio is None


def test_session_alignment_full_contract() -> None:
    alignment = SessionAlignment(**_alignment())
    assert alignment.schema_version == "1.0"
    assert alignment.reference_kind == "MARKET"
    assert alignment.target_return_pct == 3.2
    assert alignment.reference_return_pct == 0.8
    assert alignment.residual_return_pct == 2.4
    assert alignment.direction_relation == "SAME_DIRECTION"
    assert alignment.band == "ALIGNED"
    assert alignment.availability == "AVAILABLE"
    for kind in ("MARKET", "SECTOR", "PEER_MEDIAN"):
        assert SessionAlignment(**_alignment(reference_kind=kind)).reference_kind == kind
    with pytest.raises(ValidationError):
        SessionAlignment(**_alignment(reference_kind="INDEX"))


def test_session_alignment_unavailable_is_explicitly_unknown() -> None:
    with pytest.raises(ValidationError):
        SessionAlignment(**_alignment(availability="UNAVAILABLE", band="ALIGNED"))
    with pytest.raises(ValidationError):
        SessionAlignment(
            **_alignment(
                availability="UNAVAILABLE", direction_relation="SAME_DIRECTION"
            )
        )


def test_move_profile_serialized_fields_match_frozen_section_6_2() -> None:
    profile = MoveProfile(
        target_return=3.2,
        prior_session_return=1.1,
        gap_return=0.4,
        market_return=0.8,
        sector_return=1.9,
        peer_summary=PeerSummary(**_peer_summary()),
        market_adjusted_return=2.4,
        sector_adjusted_return=1.3,
        volume_abnormality=VolumeAbnormality(**_volume()),
        scheduled_macro_flags=(ScheduledMacroFlag(name="fomc"),),
        market_comove=SessionAlignment(**_alignment()),
        sector_comove=SessionAlignment(**_alignment(reference_kind="SECTOR")),
        peer_comove=SessionAlignment(**_alignment(reference_kind="PEER_MEDIAN")),
        coverage_flags=("sector_proxy_unavailable",),
        degraded_fields=(DegradedField(field="gap_return", reason_code="open_unavailable"),),
    )
    assert set(profile.model_dump()) == {
        "target_return",
        "prior_session_return",
        "gap_return",
        "market_return",
        "sector_return",
        "peer_summary",
        "market_adjusted_return",
        "sector_adjusted_return",
        "volume_abnormality",
        "scheduled_macro_flags",
        "market_comove",
        "sector_comove",
        "peer_comove",
        "coverage_flags",
        "degraded_fields",
    }


def test_move_profile_is_frozen_and_forbids_extra() -> None:
    profile = MoveProfile()
    with pytest.raises(ValidationError):
        profile.target_return = 1.0
    with pytest.raises(ValidationError):
        MoveProfile(news_dependent_field=True)


def test_unknown_fields_stay_none_with_explicit_degraded_fields() -> None:
    profile = MoveProfile(
        target_return=2.0,
        degraded_fields=(DegradedField(field="peer_summary", reason_code="peer_map_unavailable"),),
    )
    assert profile.peer_summary is None
    assert profile.volume_abnormality is None
    assert profile.market_comove is None
    assert profile.degraded_fields[0].field == "peer_summary"
    dumped = profile.model_dump()
    assert dumped["peer_summary"] is None
    assert dumped["volume_abnormality"] is None


def test_scheduled_macro_flag_and_coverage_flag_are_typed() -> None:
    flag = ScheduledMacroFlag(name="fomc")
    assert flag.name == "fomc"
    assert isinstance(flag, ScheduledMacroFlag)
    profile = MoveProfile(coverage_flags=("macro_calendar_unavailable",))
    assert profile.coverage_flags == ("macro_calendar_unavailable",)
    assert CoverageFlag is str


def test_peer_return_is_bounded_ticker_and_return_pair() -> None:
    peer = PeerReturn(ticker="MSFT", return_pct=0.5)
    assert peer.ticker == "MSFT"
    assert peer.return_pct == 0.5
    with pytest.raises(ValidationError):
        PeerReturn(ticker="MSFT", return_pct=float("nan"))


def test_scenario_predicate_inputs_are_typed_and_never_depend_on_news_or_evidence() -> None:
    inputs = ScenarioPredicateInputs(
        target_return_pct=3.2,
        prior_session_return_pct=1.1,
        market_return_pct=0.8,
        sector_return_pct=1.9,
        peer_return_pct=1.25,
        volume_band="ELEVATED",
        macro_flags=(ScheduledMacroFlag(name="fomc"),),
    )
    assert inputs.target_return_pct == 3.2
    assert inputs.prior_session_return_pct == 1.1
    assert inputs.volume_band == "ELEVATED"
    field_names = set(ScenarioPredicateInputs.model_fields)
    assert field_names == {
        "target_return_pct",
        "prior_session_return_pct",
        "market_return_pct",
        "sector_return_pct",
        "peer_return_pct",
        "volume_band",
        "macro_flags",
    }
    assert not any("news" in name or "evidence" in name for name in field_names)
    with pytest.raises(ValidationError):
        ScenarioPredicateInputs(target_return_pct=1.0, evidence_ids=["e1"])
