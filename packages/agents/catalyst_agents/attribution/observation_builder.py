"""Production observation and MoveProfile construction (M4-1)."""
from __future__ import annotations

import math
import statistics
from typing import Any, Mapping

from catalyst_agents.attribution.move_profile import (
    AlignmentBand,
    AvailabilityState,
    DegradedField,
    DirectionRelation,
    MoveProfile,
    PeerReturn,
    PeerSummary,
    SessionAlignment,
    VolumeAbnormality,
    VolumeBand,
)
from catalyst_agents.attribution.provider import ContextProvider
from catalyst_agents.runtime.manifest import ObservationPolicyConfig


def _parse_utc(value: str):
    from datetime import datetime, timezone

    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: Any) -> str:
    from datetime import timezone

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value)) and float(value) > 0


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value))


def _return_pct(current: float | None, previous: float | None) -> float | None:
    if not (_finite_positive(current) and _finite_positive(previous)):
        return None
    return (float(current) / float(previous) - 1.0) * 100.0


# M9 v2 (2026-10-01): a fixed 1pp residual called a -4.5% bank move against a
# -1.5% XLF "divergent". Single names move several times a diversified ETF, so
# the tolerance also scales with the target move.
ALIGNED_RESIDUAL_SHARE = 0.35


def _alignment(
    *,
    reference_kind: str,
    target_return: float | None,
    reference_return: float | None,
    flat_return_pct: float,
    aligned_residual_pct: float,
) -> SessionAlignment:
    """Locked one-session alignment predicate (Agents Foundation TSD §4).

    ``SessionAlignment`` describes one-session directional/magnitude
    alignment; it is never statistical correlation (Final TSD §31 cl. 3).
    """
    target = float(target_return) if _finite(target_return) else None
    reference = float(reference_return) if _finite(reference_return) else None
    if target is None or reference is None:
        return SessionAlignment(
            schema_version="session_alignment_v1",
            reference_kind=reference_kind,
            target_return_pct=target,
            reference_return_pct=reference,
            residual_return_pct=None,
            direction_relation=DirectionRelation.UNKNOWN,
            band=AlignmentBand.UNKNOWN,
            availability=AvailabilityState.UNAVAILABLE,
            reason_codes=("alignment_reference_unavailable",),
        )
    if abs(target) < flat_return_pct:
        direction, band = DirectionRelation.TARGET_FLAT, AlignmentBand.UNKNOWN
    elif abs(reference) < flat_return_pct:
        direction, band = DirectionRelation.REFERENCE_FLAT, AlignmentBand.UNKNOWN
    elif (target > 0) != (reference > 0):
        direction, band = DirectionRelation.OPPOSITE_DIRECTION, AlignmentBand.DIVERGENT
    elif abs(target - reference) <= max(
        aligned_residual_pct, ALIGNED_RESIDUAL_SHARE * abs(target)
    ):
        direction, band = DirectionRelation.SAME_DIRECTION, AlignmentBand.ALIGNED
    else:
        direction, band = DirectionRelation.SAME_DIRECTION, AlignmentBand.DIVERGENT
    return SessionAlignment(
        schema_version="session_alignment_v1",
        reference_kind=reference_kind,
        target_return_pct=target,
        reference_return_pct=reference,
        residual_return_pct=target - reference,
        direction_relation=direction,
        band=band,
        availability=AvailabilityState.AVAILABLE,
    )


def _peer_summary(
    *,
    peer_returns_by_ticker: Mapping[str, float | None],
    target_return: float | None,
    minimum_peer_count: int,
) -> PeerSummary:
    """Equal-weight peer median with explicit coverage (Phase 3 TSD §4)."""
    pairs = tuple(
        sorted(
            (ticker, value if _finite(value) else None)
            for ticker, value in peer_returns_by_ticker.items()
        )
    )
    expected = len(pairs)
    effective_expected = max(expected, minimum_peer_count)
    finite_values = [float(value) for _, value in pairs if _finite(value)]
    available = len(finite_values)
    if expected > 0 and available == effective_expected:
        coverage = AvailabilityState.AVAILABLE
    elif available > 0:
        coverage = AvailabilityState.PARTIAL
    else:
        coverage = AvailabilityState.UNAVAILABLE
    reason_codes: list[str] = []
    if expected == 0:
        reason_codes.append("peer_universe_unavailable")
    elif available < effective_expected:
        reason_codes.append("peer_returns_partial")
    if not finite_values:
        reason_codes.append("peer_returns_unavailable")
    median = float(statistics.median(finite_values)) if finite_values else None
    target_minus = (
        float(target_return) - median
        if target_return is not None and median is not None
        else None
    )
    return PeerSummary(
        schema_version="peer_summary_v1",
        expected_peer_count=effective_expected,
        available_peer_count=available,
        peer_returns=tuple(
            PeerReturn(ticker=ticker, return_pct=value)
            for ticker, value in pairs
            if _finite(value)
        ),
        median_return_pct=median,
        target_minus_median_pct=target_minus,
        coverage_state=coverage,
        reason_codes=tuple(sorted(set(reason_codes))),
    )


def _volume_abnormality(
    *,
    target_volume: float | None,
    expected_prior_sessions: tuple[str, ...],
    prior_volumes_by_session: Mapping[str, float | None],
    elevated_ratio: float,
    extreme_ratio: float,
) -> VolumeAbnormality:
    """Volume band from the prior 20 regular sessions (M4-1 amendment §2).

    Finite positive prior volumes only; at least 10 valid prior sessions;
    insufficient or missing data is UNKNOWN, never NORMAL.
    """
    valid: list[float] = []
    for session in expected_prior_sessions:
        value = prior_volumes_by_session.get(session)
        if _finite_positive(value):
            valid.append(float(value))
    reason_codes: list[str] = []
    if not _finite_positive(target_volume):
        reason_codes.append("invalid_target_volume")
    if len(valid) < 10:
        reason_codes.append("fewer_than_10_valid_prior_sessions")
    if reason_codes:
        return VolumeAbnormality(
            schema_version="volume_abnormality_v1",
            target_volume=float(target_volume) if _finite_positive(target_volume) else None,
            baseline_median_volume=None,
            ratio=None,
            expected_session_count=len(expected_prior_sessions),
            valid_session_count=len(valid),
            band=VolumeBand.UNKNOWN,
            availability=AvailabilityState.UNAVAILABLE,
            reason_codes=tuple(sorted(set(reason_codes))),
        )
    baseline = float(statistics.median(valid))
    ratio = float(target_volume) / baseline
    if ratio >= extreme_ratio:
        band = VolumeBand.EXTREME
    elif ratio >= elevated_ratio:
        band = VolumeBand.ELEVATED
    else:
        band = VolumeBand.NORMAL
    return VolumeAbnormality(
        schema_version="volume_abnormality_v1",
        target_volume=float(target_volume),
        baseline_median_volume=baseline,
        ratio=ratio,
        expected_session_count=len(expected_prior_sessions),
        valid_session_count=len(valid),
        band=band,
        availability=AvailabilityState.AVAILABLE,
    )


class ObservationBuilder:
    """Agents-owned Observation/MoveProfile builder (M4-1).

    Reuses the ContextBuilder formulas (returns, volume, decomposition) and
    assembles the typed MoveProfile. Data-core supplies PIT facts only; this
    builder performs no documentary retrieval, LLM call, causal inference,
    implicit external lookup, or database mutation.
    """

    def __init__(
        self, *, provider: ContextProvider, policy: ObservationPolicyConfig
    ) -> None:
        self.provider = provider
        self.policy = policy

    def build(
        self,
        *,
        ticker: str,
        session_date: str,
        cutoff: str,
        temporal_identity: Any = None,
        observation_policy_version: str | None = None,
    ) -> MoveProfile:
        """Build the MoveProfile under the validated TemporalIdentity.

        When a TemporalIdentity is supplied it is authoritative: the session
        date comes from ``temporal_identity.session_date`` and the cutoff from
        ``temporal_identity.cutoff_at``. Explicit string arguments must agree
        and fail closed on disagreement; they are never silently discarded.
        """
        if temporal_identity is not None:
            if session_date is not None and session_date != temporal_identity.session_date:
                raise ValueError(
                    "session_date disagrees with TemporalIdentity.session_date"
                )
            if cutoff is not None and _parse_utc(cutoff) != temporal_identity.cutoff_at:
                raise ValueError(
                    "cutoff disagrees with TemporalIdentity.cutoff_at"
                )
            session_date = temporal_identity.session_date
            cutoff = _utc_iso(temporal_identity.cutoff_at)
            information_window_start_at = _utc_iso(
                temporal_identity.information_window_start_at
            )
        else:
            information_window_start_at = None
        if not session_date or not cutoff:
            raise ValueError(
                "session_date and cutoff are required when TemporalIdentity is absent"
            )
        inputs = self.provider.load_context_inputs(
            ticker=ticker,
            session_date=session_date,
            cutoff=cutoff,
            information_window_start_at=information_window_start_at,
        )
        # Step 0: on an ex-dividend session the cash goes back into the close and the open,
        # so the move card shows the holder's return rather than the mechanical drop.
        dividend_cash = getattr(inputs, "target_ex_dividend_cash", None)
        dividend_cash = float(dividend_cash) if _finite(dividend_cash) and dividend_cash > 0 else 0.0
        target_return = _return_pct(
            inputs.target_close + dividend_cash if inputs.target_close is not None else None,
            inputs.previous_target_close,
        )
        prior_return = _return_pct(
            inputs.previous_target_close, inputs.previous_2_target_close
        )
        gap_return = _return_pct(
            inputs.target_open + dividend_cash if inputs.target_open is not None else None,
            inputs.previous_target_close,
        )
        market_return = (
            float(inputs.benchmark_return_pct)
            if _finite(inputs.benchmark_return_pct)
            else None
        )
        sector_return = (
            float(inputs.sector_return_pct)
            if _finite(inputs.sector_return_pct)
            else None
        )
        market_adjusted = (
            float(target_return) - market_return
            if target_return is not None and market_return is not None
            else None
        )
        sector_adjusted = (
            float(target_return) - sector_return
            if target_return is not None and sector_return is not None
            else None
        )

        peer_summary = _peer_summary(
            peer_returns_by_ticker=inputs.peer_returns_by_ticker,
            target_return=target_return,
            minimum_peer_count=self.policy.minimum_peer_count,
        )
        volume = _volume_abnormality(
            target_volume=inputs.target_volume,
            expected_prior_sessions=inputs.expected_prior_sessions,
            prior_volumes_by_session=inputs.prior_volumes_by_session,
            elevated_ratio=self.policy.volume_elevated_ratio,
            extreme_ratio=self.policy.volume_extreme_ratio,
        )
        market_comove = _alignment(
            reference_kind="MARKET",
            target_return=target_return,
            reference_return=market_return,
            flat_return_pct=self.policy.flat_reference_return_pct,
            aligned_residual_pct=self.policy.aligned_residual_pct,
        )
        sector_comove = _alignment(
            reference_kind="SECTOR",
            target_return=target_return,
            reference_return=sector_return,
            flat_return_pct=self.policy.flat_reference_return_pct,
            aligned_residual_pct=self.policy.aligned_residual_pct,
        )
        peer_comove = _alignment(
            reference_kind="PEER_MEDIAN",
            target_return=target_return,
            reference_return=peer_summary.median_return_pct,
            flat_return_pct=self.policy.flat_reference_return_pct,
            aligned_residual_pct=self.policy.aligned_residual_pct,
        )

        coverage_flags: set[str] = set()
        if dividend_cash:
            coverage_flags.add("ex_dividend_adjusted")
        if _finite(getattr(inputs, "target_split_ratio", None)):
            coverage_flags.add("split_on_session")
        degraded_fields: list[DegradedField] = []
        if target_return is None:
            coverage_flags.add("target_return_unavailable")
            degraded_fields.append(
                DegradedField(field="target_return", reason_code="target_return_unavailable")
            )
        if prior_return is None:
            degraded_fields.append(
                DegradedField(
                    field="prior_session_return", reason_code="prior_session_return_unavailable"
                )
            )
        if gap_return is None:
            degraded_fields.append(
                DegradedField(field="gap_return", reason_code="gap_return_unavailable")
            )
        if market_return is None:
            coverage_flags.add("benchmark_unavailable")
            degraded_fields.append(
                DegradedField(field="market_return", reason_code="benchmark_unavailable")
            )
        if sector_return is None:
            coverage_flags.add("sector_unavailable")
            degraded_fields.append(
                DegradedField(field="sector_return", reason_code="sector_unavailable")
            )
        if peer_summary.median_return_pct is None:
            coverage_flags.add("peer_returns_unavailable")
        if volume.availability is AvailabilityState.UNAVAILABLE:
            coverage_flags.update(volume.reason_codes)
        if not inputs.macro_source_available:
            coverage_flags.add("scheduled_macro_source_unavailable")
            degraded_fields.append(
                DegradedField(
                    field="scheduled_macro_flags",
                    reason_code="scheduled_macro_source_unavailable",
                )
            )

        macro_flags = self._window_filtered_macro_flags(
            inputs.scheduled_macro_flags, temporal_identity
        )
        # A5 behavioral seam: ``context_builder_v1_limited`` returns a limited
        # observation (returns/coverage only, no peer/volume/comove context);
        # ``move_profile_v1`` (and None) is the MoveProfile-aware production
        # observation.
        if observation_policy_version == "context_builder_v1_limited":
            return MoveProfile(
                target_return=target_return,
                prior_session_return=prior_return,
                gap_return=gap_return,
                market_return=market_return,
                sector_return=sector_return,
                market_adjusted_return=market_adjusted,
                sector_adjusted_return=sector_adjusted,
                coverage_flags=tuple(sorted(coverage_flags)),
                degraded_fields=tuple(degraded_fields),
            )
        return MoveProfile(
            target_return=target_return,
            prior_session_return=prior_return,
            gap_return=gap_return,
            market_return=market_return,
            sector_return=sector_return,
            peer_summary=peer_summary,
            market_adjusted_return=market_adjusted,
            sector_adjusted_return=sector_adjusted,
            volume_abnormality=volume,
            scheduled_macro_flags=macro_flags,
            market_comove=market_comove,
            sector_comove=sector_comove,
            peer_comove=peer_comove,
            coverage_flags=tuple(sorted(coverage_flags)),
            degraded_fields=tuple(degraded_fields),
            commodity_ticker=getattr(inputs, "commodity_ticker", None),
            sector_ticker=getattr(inputs, "sector_ticker", None),
            ticker=getattr(inputs, "ticker", None),
            commodity_return=(
                float(inputs.commodity_return_pct)
                if _finite(getattr(inputs, "commodity_return_pct", None))
                else None
            ),
        )

    def _window_filtered_macro_flags(
        self,
        flags: tuple[Any, ...],
        temporal_identity: Any,
    ) -> tuple[Any, ...]:
        """Deterministic dedup/sort of configured releases bounded by the
        information window; releases outside the window never produce flags."""
        if temporal_identity is not None:
            filtered = []
            for flag in flags:
                scheduled_at = getattr(flag, "scheduled_at", None)
                if scheduled_at is None:
                    continue
                if not (
                    temporal_identity.information_window_start_at
                    <= scheduled_at
                    <= temporal_identity.cutoff_at
                ):
                    continue
                filtered.append(flag)
            flags = tuple(filtered)
        seen: set[str] = set()
        deduped = []
        for flag in sorted(flags, key=lambda f: (f.name, str(f.scheduled_at))):
            key = f"{flag.name}:{flag.scheduled_at}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(flag)
        return tuple(deduped)


__all__ = ["ObservationBuilder", "ALIGNED_RESIDUAL_SHARE", "_alignment"]
