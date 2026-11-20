from __future__ import annotations

import pytest
from catalyst_agents.attribution.context_builder import _alignment
from catalyst_agents.attribution.move_profile import (
    AvailabilityState,
    MoveProfile,
    PeerReturn,
    PeerSummary,
)
from catalyst_agents.attribution.tiers import decide_tier
from catalyst_agents.runtime.manifest import ObservationPolicyConfig

POLICY = ObservationPolicyConfig(
    material_target_return_pct=2.0,
    material_prior_return_pct=1.5,
    quiet_target_return_pct=0.5,
    flat_reference_return_pct=0.25,
    aligned_residual_pct=1.0,
    volume_elevated_ratio=1.5,
    volume_extreme_ratio=3.0,
    minimum_peer_count=3,
    require_sector_and_peer_for_broad_sector=False,
    scenario_policy_version="sp:v2",
)


def _profile(
    target: float | None = 0.5,
    *,
    prior: float | None = 0.0,
    market: float | None = 0.4,
    sector: float | None = 0.5,
    peers: tuple[float, ...] = (0.4, 0.5, 0.6),
    commodity: float | None = None,
    gap: float | None = 0.0,
    volume_ratio: float | None = 1.0,
    volume_band: str = "NORMAL",
) -> MoveProfile:
    peer_returns = tuple(
        PeerReturn(ticker=f"P{i}", return_pct=value)
        for i, value in enumerate(peers)
    )
    median = sorted(peers)[len(peers) // 2] if peers else None
    summary = PeerSummary(
        schema_version="peer_summary_v1",
        expected_peer_count=len(peers),
        available_peer_count=len(peers),
        peer_returns=peer_returns,
        median_return_pct=median,
        target_minus_median_pct=target - median if target is not None and median is not None else None,
        coverage_state=AvailabilityState.AVAILABLE if peers else AvailabilityState.UNAVAILABLE,
    )
    market_alignment = _alignment(
        reference_kind="MARKET", target_return=target, reference_return=market,
        flat_return_pct=POLICY.flat_reference_return_pct,
        aligned_residual_pct=POLICY.aligned_residual_pct,
    )
    sector_alignment = _alignment(
        reference_kind="SECTOR", target_return=target, reference_return=sector,
        flat_return_pct=POLICY.flat_reference_return_pct,
        aligned_residual_pct=POLICY.aligned_residual_pct,
    )
    peer_alignment = _alignment(
        reference_kind="PEER_MEDIAN", target_return=target, reference_return=median,
        flat_return_pct=POLICY.flat_reference_return_pct,
        aligned_residual_pct=POLICY.aligned_residual_pct,
    )
    from catalyst_agents.attribution.move_profile import VolumeAbnormality, VolumeBand

    volume = VolumeAbnormality(
        schema_version="volume_abnormality_v1",
        target_volume=100.0,
        baseline_median_volume=100.0,
        ratio=volume_ratio,
        expected_session_count=20,
        valid_session_count=20,
        band=VolumeBand(volume_band),
        availability=AvailabilityState.AVAILABLE,
    )
    return MoveProfile(
        ticker="ABC", target_return=target, prior_session_return=prior,
        gap_return=gap, market_return=market, sector_return=sector,
        sector_ticker="XLK", peer_summary=summary,
        market_comove=market_alignment, sector_comove=sector_alignment,
        peer_comove=peer_alignment, volume_abnormality=volume,
        commodity_ticker="USO" if commodity is not None else None,
        commodity_return=commodity,
    )


def _tier(profile: MoveProfile, filings: int = 0):
    return decide_tier(profile, filings_in_window=filings, policy=POLICY)


def test_ordinary_close_is_t1_with_aligned_threshold_and_available_reference():
    decision = _tier(_profile())
    assert decision.tier == "T1"
    assert decision.governing_ref["kind"] == "peers"
    assert decision.threshold_used == 1.0
    assert decision.overrides == []


def test_market_wide_shock_is_t2_governed_by_spy():
    decision = _tier(_profile(-4.0, market=-4.0, sector=None, peers=()))
    assert (decision.tier, decision.governing_ref["kind"], decision.governing_ref["ticker"]) == (
        "T2", "market", "SPY"
    )


def test_oil_aligned_move_is_t2_governed_by_commodity():
    decision = _tier(_profile(-8.0, market=-1.0, sector=-4.0, peers=(-3.0, -3.5, -2.0), commodity=-8.0))
    assert decision.tier == "T2"
    assert decision.governing_ref["kind"] == "commodity"
    assert decision.governing_ref["ticker"] == "USO"


def test_flat_stock_against_moving_sector_is_t3():
    decision = _tier(_profile(0.0, market=-1.0, sector=-3.0, peers=(-2.0, -3.0, -4.0)))
    assert decision.tier == "T3"
    assert decision.governing_ref is None


def test_large_gap_override_blocks_t1():
    decision = _tier(_profile(0.2, gap=2.0))
    assert decision.tier == "T3"
    assert "GAP_GE_2" in decision.overrides


def test_in_window_issuer_filing_override_blocks_t1():
    decision = _tier(_profile(), filings=1)
    assert decision.tier == "T3"
    assert "ISSUER_FILING" in decision.overrides


def test_missing_sector_does_not_claim_in_line():
    decision = _tier(_profile(0.4, market=0.4, sector=None, peers=()))
    assert decision.tier == "T1"
    assert decision.governing_ref is None
    assert decision.reason_code != "IN_LINE_WITH_SECTOR"


def test_opposite_direction_is_t3():
    decision = _tier(_profile(3.0, market=-3.0, sector=-2.5, peers=(-3.0, -2.0, -2.5)))
    assert decision.tier == "T3"
    assert decision.governing_ref is None


def test_aligned_market_but_divergent_sector_has_no_governing_reference():
    decision = _tier(_profile(-3.0, market=-3.0, sector=1.0, peers=(-2.8, -3.0, -3.2)))
    assert decision.tier == "T3"
    assert decision.governing_ref is None


def test_commodity_move_blocks_t1_even_when_not_aligned():
    decision = _tier(_profile(0.4, commodity=-2.0))
    assert decision.tier == "T3"


def test_scaled_alignment_threshold_uses_locked_share():
    decision = _tier(_profile(-8.0, market=-6.0, sector=-6.0, peers=(-6.0, -6.0, -6.0)))
    assert decision.threshold_used == pytest.approx(2.8)


@pytest.mark.parametrize(
    ("profile", "filings", "tier", "reason"),
    [
        (_profile(None), 0, "T0", "target_return_unavailable"),
        (_profile(0.5), 0, "T1", ""),
        (_profile(2.0, market=2.0, sector=2.0, peers=(2.0, 2.0, 2.0)), 0, "T2", ""),
        (_profile(0.0, market=0.2, sector=0.3, peers=(0.2, 0.3, 0.4)), 0, "T1", ""),
        (_profile(0.2, market=2.0, sector=-3.0, peers=()), 0, "T3", ""),
        (_profile(0.2, market=None, sector=None, peers=(), volume_ratio=3.0, volume_band="EXTREME"), 0, "T3", ""),
        (_profile(2.0, prior=2.0, market=2.0, sector=2.0, peers=(2.0, 2.0, 2.0)), 0, "T3", ""),
    ],
)
def test_tier_edges(profile, filings, tier, reason):
    decision = _tier(profile, filings)
    assert decision.tier == tier
    if reason:
        assert decision.reason_code == reason
    if profile.target_return == 0.0 and profile.sector_return == 0.3:
        assert "CONTINUATION" not in decision.overrides
    if profile.volume_abnormality.band.value == "EXTREME":
        assert "VOLUME_EXTREME" in decision.overrides
    if profile.prior_session_return == 2.0:
        assert "CONTINUATION" in decision.overrides


def test_non_trading_session_is_t0_even_with_a_return():
    profile = _profile(1.0).model_copy(update={
        "coverage_flags": ("not_a_trading_session",)
    })
    decision = _tier(profile)
    assert decision.tier == "T0"
    assert decision.reason_code == "not_a_trading_session"


def test_flat_stock_with_a_two_name_peer_set_is_still_ordinary():
    # AAPL 2026-03-24: +0.06%, SPY -0.34%, XLK -0.58%, peers DELL +7.5%, HPQ +1.9% (2 available).
    profile = _profile(0.06, market=-0.34, sector=-0.58, peers=(7.49, 1.94))
    assert _tier(profile).tier == "T1"
    # with three peers that rose, the flat stock did hold up against its group
    assert _tier(_profile(0.06, market=-0.34, sector=-0.58, peers=(7.49, 1.94, 2.5))).tier == "T3"
