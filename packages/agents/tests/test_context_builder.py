from __future__ import annotations

import statistics

import pytest

from attribution_fixtures import (
    EXPECTED_20_SESSIONS,
    EXPECTED_VALID_12_SESSION_IDS,
    VALID_12_VOLUMES,
    mock_provider,
    mock_provider_with_ohlcv,
    volume_window_provider,
)


def test_context_builder_additive_decomposition():
    from catalyst_agents.attribution.context_builder import ContextBuilder

    artifact = ContextBuilder(provider=mock_provider_with_ohlcv()).build(
        ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z"
    )
    assert artifact.target_return_pct == pytest.approx(
        artifact.market_component + artifact.sector_excess + artifact.company_specific,
        abs=0.01,
    )


def test_context_builder_return_formula():
    from catalyst_agents.attribution.context_builder import ContextBuilder

    builder = ContextBuilder(provider=mock_provider(close_prices={"2026-01-15": 150.0, "2026-01-14": 155.0}))
    artifact = builder.build(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    assert artifact.target_return_pct == pytest.approx((150.0 / 155.0 - 1) * 100, abs=0.01)


def test_volume_ratio_requires_10_sessions():
    from catalyst_agents.attribution.context_builder import ContextBuilder

    artifact = ContextBuilder(provider=mock_provider(n_sessions=5)).build(
        ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z"
    )
    assert artifact.volume_context_available is False


def test_volume_ratio_with_20_sessions():
    from catalyst_agents.attribution.context_builder import ContextBuilder

    artifact = ContextBuilder(provider=mock_provider(n_sessions=25)).build(
        ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z"
    )
    assert artifact.volume_context_available is True
    assert artifact.volume_ratio is not None


def test_volume_ratio_uses_all_valid_values_inside_fixed_20_session_window():
    from catalyst_agents.attribution.context_builder import ContextBuilder

    provider = volume_window_provider(
        expected_prior_sessions=EXPECTED_20_SESSIONS,
        valid_prior_volumes=VALID_12_VOLUMES,
        older_volume=9_999_999,
        target_volume=1_200,
    )
    artifact = ContextBuilder(provider=provider).build(
        ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z"
    )
    assert artifact.volume_context_available is True
    assert artifact.volume_ratio == pytest.approx(1_200 / statistics.median(VALID_12_VOLUMES))
    assert artifact.volume_valid_sessions == EXPECTED_VALID_12_SESSION_IDS
    assert artifact.volume_denominator == statistics.median(VALID_12_VOLUMES)
    assert 9_999_999 not in artifact.volume_prior_values


@pytest.mark.parametrize("invalid_target_volume", [None, 0, -1, float("nan")])
def test_volume_ratio_rejects_missing_or_non_positive_target_volume(invalid_target_volume):
    from catalyst_agents.attribution.context_builder import ContextBuilder

    artifact = ContextBuilder(provider=volume_window_provider(
        expected_prior_sessions=EXPECTED_20_SESSIONS,
        valid_prior_volumes=VALID_12_VOLUMES,
        target_volume=invalid_target_volume,
    )).build(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    assert artifact.volume_context_available is False
    assert artifact.volume_ratio is None
    assert artifact.volume_unavailable_reason == "invalid_target_volume"


def test_peer_median_missing_is_not_available():
    from catalyst_agents.attribution.context_builder import ContextBuilder

    artifact = ContextBuilder(provider=mock_provider(peer_data={})).build(
        ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z"
    )
    assert artifact.target_vs_peers == "not_available"


def test_peer_median_remains_available_when_target_return_is_unavailable():
    from catalyst_agents.attribution.context_builder import ContextBuilder

    provider = mock_provider(target_close=None, peer_data={"MSFT": -2.5, "GOOGL": -1.5})
    artifact = ContextBuilder(provider=provider).build(
        ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z"
    )

    assert artifact.peer_median == -2.0
    assert artifact.target_vs_peers == "not_available"


def test_context_builder_emits_deterministic_artifact():
    from catalyst_agents.attribution.context_builder import ContextBuilder, canonical_context_bytes

    provider = mock_provider_with_ohlcv()
    b1 = ContextBuilder(provider=provider).build(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    b2 = ContextBuilder(provider=provider).build(ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z")
    assert canonical_context_bytes(b1) == canonical_context_bytes(b2)


def test_non_finite_benchmark_return_is_named_unavailable():
    from catalyst_agents.attribution.context_builder import ContextBuilder

    artifact = ContextBuilder(provider=mock_provider(benchmark_return_pct=float("nan"))).build(
        ticker="AAPL", session_date="2026-01-15", cutoff="2026-01-15T21:00:00Z"
    )

    assert artifact.decomposition_available is False
    assert artifact.decomposition_unavailable_reason == "benchmark_unavailable"
    assert artifact.market_component is None
