"""M4-1: ObservationBuilder and MoveProfile production formulas (amendment §2)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

import pytest
from pydantic import ValidationError

from catalyst_agents.attribution.context_builder import ObservationBuilder
from catalyst_agents.attribution.move_profile import (
    AlignmentBand,
    AvailabilityState,
    DirectionRelation,
    MoveProfile,
    PeerSummary,
    SessionAlignment,
    VolumeBand,
)
from catalyst_agents.attribution.provider import ContextInputs
from catalyst_agents.runtime.manifest import ObservationPolicyConfig


def _policy(**overrides) -> ObservationPolicyConfig:
    base = dict(
        material_target_return_pct=2.0,
        material_prior_return_pct=1.5,
        quiet_target_return_pct=0.5,
        flat_reference_return_pct=0.25,
        aligned_residual_pct=1.0,
        volume_elevated_ratio=1.5,
        volume_extreme_ratio=3.0,
        minimum_peer_count=3,
        require_sector_and_peer_for_broad_sector=True,
        scenario_policy_version="sp:v1",
    )
    base.update(overrides)
    return ObservationPolicyConfig(**base)


def _inputs(**overrides) -> ContextInputs:
    base = dict(
        ticker="AAPL",
        session_date=date(2026, 1, 15),
        cutoff="2026-01-15T21:00:00Z",
        target_close=110.0,
        previous_target_close=100.0,
        target_open=102.0,
        previous_2_target_close=90.0,
        target_volume=2200.0,
        expected_prior_sessions=tuple(f"2026-01-{day:02d}" for day in range(1, 21)),
        prior_volumes_by_session={
            f"2026-01-{day:02d}": 1000.0 + day for day in range(1, 21)
        },
        benchmark_ticker="SPY",
        benchmark_return_pct=1.5,
        sector_ticker="XLK",
        sector_return_pct=2.0,
        peer_returns_by_ticker={"MSFT": 4.0, "NVDA": 6.0},
    )
    base.update(overrides)
    return ContextInputs(**base)


class RecordingProvider:
    def __init__(self, inputs: ContextInputs):
        self._inputs = inputs
        self.calls: list[tuple[str, str, str]] = []
        self.document_lookups = 0

    def load_context_inputs(self, *, ticker: str, session_date: str, cutoff: str) -> ContextInputs:
        self.calls.append((ticker, session_date, cutoff))
        return self._inputs


def _build(provider, policy=None):
    builder = ObservationBuilder(
        provider=provider, policy=policy or _policy()
    )
    return builder.build(
        ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z"
    )


def test_observation_builder_computes_locked_formulas():
    """M4-1 amendment §2 formulas: target/prior/gap, adjusted returns, peer
    median, volume band."""
    provider = RecordingProvider(_inputs())
    profile = _build(provider)

    assert profile.target_return == pytest.approx(10.0)
    assert profile.prior_session_return == pytest.approx((100.0 / 90.0 - 1.0) * 100.0)
    assert profile.gap_return == pytest.approx(2.0)
    assert profile.market_return == pytest.approx(1.5)
    assert profile.sector_return == pytest.approx(2.0)
    assert profile.market_adjusted_return == pytest.approx(8.5)
    assert profile.sector_adjusted_return == pytest.approx(8.0)
    assert profile.peer_summary is not None
    assert profile.peer_summary.median_return_pct == pytest.approx(5.0)
    assert profile.peer_summary.target_minus_median_pct == pytest.approx(5.0)
    assert profile.peer_summary.coverage_state == AvailabilityState.AVAILABLE
    assert profile.volume_abnormality is not None
    assert profile.volume_abnormality.band == VolumeBand.ELEVATED
    assert profile.volume_abnormality.ratio == pytest.approx(2200.0 / 1010.5)


def test_observation_builder_market_and_sector_alignment():
    provider = RecordingProvider(
        _inputs(
            benchmark_return_pct=9.5,
            sector_return_pct=9.2,
            peer_returns_by_ticker={"MSFT": 9.8, "NVDA": 9.6},
        )
    )
    profile = _build(provider)
    assert profile.market_comove is not None
    assert profile.market_comove.band == AlignmentBand.ALIGNED
    assert profile.market_comove.direction_relation == DirectionRelation.SAME_DIRECTION
    assert profile.sector_comove is not None
    assert profile.sector_comove.band == AlignmentBand.ALIGNED
    assert profile.peer_comove is not None
    assert profile.peer_comove.band == AlignmentBand.ALIGNED


def test_observation_builder_missing_benchmark_keeps_explicit_unknown():
    """Missing benchmark -> null market fields and UNKNOWN alignment; missing
    sector does not erase a valid market residual (Q-008)."""
    provider = RecordingProvider(
        _inputs(benchmark_ticker=None, benchmark_return_pct=None)
    )
    profile = _build(provider)
    assert profile.market_return is None
    assert profile.market_adjusted_return is None
    assert profile.market_comove is not None
    assert profile.market_comove.band == AlignmentBand.UNKNOWN
    assert profile.market_comove.availability == AvailabilityState.UNAVAILABLE
    assert profile.sector_adjusted_return == pytest.approx(8.0)


def test_observation_builder_missing_sector_keeps_market_residual():
    provider = RecordingProvider(_inputs(sector_ticker=None, sector_return_pct=None))
    profile = _build(provider)
    assert profile.sector_return is None
    assert profile.sector_adjusted_return is None
    assert profile.sector_comove is not None
    assert profile.sector_comove.band == AlignmentBand.UNKNOWN
    assert profile.market_adjusted_return == pytest.approx(8.5)


def test_observation_builder_volume_never_normal_when_missing():
    provider = RecordingProvider(
        _inputs(target_volume=None, prior_volumes_by_session={})
    )
    profile = _build(provider)
    assert profile.volume_abnormality is not None
    assert profile.volume_abnormality.band == VolumeBand.UNKNOWN
    assert profile.volume_abnormality.availability == AvailabilityState.UNAVAILABLE

    few = RecordingProvider(
        _inputs(prior_volumes_by_session={"2026-01-01": 100.0})
    )
    profile = _build(few)
    assert profile.volume_abnormality.band == VolumeBand.UNKNOWN


def test_observation_builder_alignment_predicate_locked():
    """Exact SessionAlignment predicate (Agents Foundation TSD §4)."""
    provider = RecordingProvider(
        _inputs(
            benchmark_return_pct=10.0,
            sector_return_pct=-5.0,
        )
    )
    profile = _build(provider)
    assert profile.market_comove.direction_relation == DirectionRelation.SAME_DIRECTION
    assert profile.market_comove.band == AlignmentBand.ALIGNED
    assert profile.sector_comove.direction_relation == DirectionRelation.OPPOSITE_DIRECTION
    assert profile.sector_comove.band == AlignmentBand.DIVERGENT

    flat_target = _build(
        RecordingProvider(
            _inputs(target_close=100.2, previous_target_close=100.0)
        )
    )
    assert flat_target.market_comove.direction_relation == DirectionRelation.TARGET_FLAT

    flat_ref = _build(
        RecordingProvider(_inputs(benchmark_return_pct=0.1))
    )
    assert flat_ref.market_comove.direction_relation == DirectionRelation.REFERENCE_FLAT

    divergent = _build(
        RecordingProvider(_inputs(benchmark_return_pct=12.0))
    )
    assert divergent.market_comove.band == AlignmentBand.DIVERGENT
    assert divergent.market_comove.direction_relation == DirectionRelation.SAME_DIRECTION


def test_observation_builder_scheduled_macro_flags_and_source_availability():
    from catalyst_agents.attribution.move_profile import ScheduledMacroFlag

    flags = (ScheduledMacroFlag(name="FOMC", scheduled_at=None),)
    provider = RecordingProvider(
        _inputs(scheduled_macro_flags=flags, macro_source_available=True)
    )
    profile = _build(provider)
    assert profile.scheduled_macro_flags == flags
    assert bool(profile.scheduled_macro_flags) is True

    empty_with_source = _build(
        RecordingProvider(_inputs(scheduled_macro_flags=(), macro_source_available=True))
    )
    assert empty_with_source.scheduled_macro_flags == ()
    assert bool(empty_with_source.scheduled_macro_flags) is False

    missing_source = _build(
        RecordingProvider(_inputs(scheduled_macro_flags=(), macro_source_available=False))
    )
    assert any("macro" in flag for flag in missing_source.coverage_flags)


def test_observation_builder_no_peer_adjusted_return_and_no_importance():
    """Schema-negative: no peer_adjusted_return; ScheduledMacroFlag has no
    importance field; PeerSummary exposes target_minus_median_pct."""
    assert "peer_adjusted_return" not in MoveProfile.model_fields
    assert "target_minus_median_pct" in PeerSummary.model_fields
    from catalyst_agents.attribution.move_profile import ScheduledMacroFlag

    assert "importance" not in ScheduledMacroFlag.model_fields


def test_observation_builder_performs_no_documentary_lookup():
    provider = RecordingProvider(_inputs())
    _build(provider)
    assert len(provider.calls) == 1
    assert provider.calls[0] == ("AAPL", "2026-01-15", "2026-01-15T21:00:00Z")
    assert provider.document_lookups == 0


def test_observation_builder_session_validity_distinct_from_price_availability():
    """Missing target close is a target_return_unavailable degradation, never an
    invalid session; the builder has no session-validity field."""
    provider = RecordingProvider(
        _inputs(target_close=None, previous_target_close=None)
    )
    profile = _build(provider)
    assert profile.target_return is None
    assert profile.gap_return is None
    assert profile.prior_session_return is None
    assert any(flag == "target_return_unavailable" for flag in profile.coverage_flags)
    assert any(
        field.field == "target_return" and field.reason_code == "target_return_unavailable"
        for field in profile.degraded_fields
    )
    assert "session_valid" not in MoveProfile.model_fields


def test_observation_builder_requires_policy():
    with pytest.raises(TypeError):
        ObservationBuilder(provider=RecordingProvider(_inputs()))
