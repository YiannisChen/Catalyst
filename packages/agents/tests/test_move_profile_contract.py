"""V1.1 MoveProfile typed contract tests (M2-3).

Schemas only in M2; classification formulas land in M4 (Frozen §6.2,
Final Migration TSD §6.2). Unknown/degraded fields stay explicit and are
never inferred.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.move_profile import (
    CoverageFlag,
    DegradedField,
    MoveProfile,
    PeerSummary,
    ScheduledMacroFlag,
    ScenarioPredicateInputs,
    SessionAlignment,
    VolumeAbnormality,
)


def _peer_summary(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "median_return_pct": 1.25,
        "pct_25_return_pct": -0.5,
        "pct_75_return_pct": 2.0,
        "coverage": "partial",
    }
    base.update(overrides)
    return base


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
        volume_abnormality=VolumeAbnormality(metric=2.1, band="ELEVATED"),
        scheduled_macro_flags=(ScheduledMacroFlag(name="fomc"),),
        market_comove=SessionAlignment(metric=0.6, band="ALIGNED"),
        sector_comove=SessionAlignment(metric=0.3, band="PARTIAL"),
        peer_comove=SessionAlignment(metric=0.1, band="UNKNOWN"),
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
    # Nothing was inferred for unknown values.
    dumped = profile.model_dump()
    assert dumped["peer_summary"] is None
    assert dumped["volume_abnormality"] is None


def test_peer_summary_coverage_is_closed_enum() -> None:
    for value in ("full", "partial", "unknown"):
        assert PeerSummary(**_peer_summary(coverage=value)).coverage == value
    with pytest.raises(ValidationError):
        PeerSummary(**_peer_summary(coverage="none"))


def test_volume_abnormality_metric_and_band() -> None:
    for value in ("NORMAL", "ELEVATED", "EXTREME", "UNKNOWN"):
        assert VolumeAbnormality(metric=1.0, band=value).band == value
    with pytest.raises(ValidationError):
        VolumeAbnormality(metric=1.0, band="HIGH")
    assert VolumeAbnormality(metric=None, band="UNKNOWN").metric is None


def test_session_alignment_metric_and_band() -> None:
    for value in ("ALIGNED", "PARTIAL", "UNALIGNED", "UNKNOWN"):
        assert SessionAlignment(metric=0.5, band=value).band == value
    with pytest.raises(ValidationError):
        SessionAlignment(metric=0.5, band="CORRELATED")
    assert SessionAlignment(metric=None, band="UNKNOWN").metric is None


def test_scheduled_macro_flag_and_coverage_flag_are_typed() -> None:
    flag = ScheduledMacroFlag(name="fomc")
    assert flag.name == "fomc"
    assert isinstance(flag, ScheduledMacroFlag)
    profile = MoveProfile(coverage_flags=("macro_calendar_unavailable",))
    assert profile.coverage_flags == ("macro_calendar_unavailable",)
    assert CoverageFlag is str


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
    # The typed predicate surface contains no news/evidence dependency; a
    # CONTINUATION classification must not consult news or evidence.
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
