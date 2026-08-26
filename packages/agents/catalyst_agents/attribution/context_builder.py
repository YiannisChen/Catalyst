from __future__ import annotations

import json
import math
import statistics
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

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


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value)) and float(value) > 0


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value))


def _return_pct(current: float | None, previous: float | None) -> float | None:
    if not (_finite_positive(current) and _finite_positive(previous)):
        return None
    return (float(current) / float(previous) - 1.0) * 100.0


class ContextBuilderArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    formula_revision: Literal["b5-context-v1"] = "b5-context-v1"
    ticker: str
    session_date: str
    cutoff: str
    target_close: float | None
    previous_target_close: float | None
    target_return_pct: float | None
    target_volume: float | None
    expected_prior_sessions: tuple[str, ...]
    volume_valid_sessions: tuple[str, ...]
    volume_prior_values: tuple[float, ...]
    volume_denominator: float | None
    volume_ratio: float | None
    volume_context_available: bool
    volume_unavailable_reason: str | None
    benchmark_ticker: str | None
    benchmark_return_pct: float | None
    sector_ticker: str | None
    sector_return_pct: float | None
    market_component: float | None
    sector_excess: float | None
    company_specific: float | None
    decomposition_available: bool
    decomposition_unavailable_reason: str | None
    peer_returns: tuple[tuple[str, float | None], ...]
    peer_median: float | Literal["not_available"]
    target_vs_peers: float | Literal["not_available"]
    data_quality_flags: tuple[str, ...] = Field(default_factory=tuple)


class ContextBuilder:
    def __init__(self, *, provider: ContextProvider) -> None:
        self.provider = provider

    def build(self, *, ticker: str, session_date: str, cutoff: str) -> ContextBuilderArtifact:
        inputs = self.provider.load_context_inputs(ticker=ticker, session_date=session_date, cutoff=cutoff)
        target_return = _return_pct(inputs.target_close, inputs.previous_target_close)
        flags: set[str] = set()
        if target_return is None:
            flags.add("target_return_unavailable")

        valid_sessions: list[str] = []
        prior_values: list[float] = []
        for session in inputs.expected_prior_sessions:
            value = inputs.prior_volumes_by_session.get(session)
            if _finite_positive(value):
                valid_sessions.append(session)
                prior_values.append(float(value))

        volume_available = False
        volume_denominator: float | None = None
        volume_ratio: float | None = None
        volume_reason: str | None = None
        if not _finite_positive(inputs.target_volume):
            volume_reason = "invalid_target_volume"
            flags.add(volume_reason)
        elif len(prior_values) < 10:
            volume_reason = "fewer_than_10_valid_prior_sessions"
            flags.add(volume_reason)
        else:
            volume_denominator = float(statistics.median(prior_values))
            if _finite_positive(volume_denominator):
                volume_ratio = float(inputs.target_volume) / volume_denominator
                volume_available = True
            else:
                volume_reason = "invalid_volume_denominator"
                flags.add(volume_reason)

        market_component: float | None = None
        sector_excess: float | None = None
        company_specific: float | None = None
        decomposition_available = False
        decomposition_reason: str | None = None
        benchmark_return = float(inputs.benchmark_return_pct) if _finite(inputs.benchmark_return_pct) else None
        sector_return = float(inputs.sector_return_pct) if _finite(inputs.sector_return_pct) else None
        if target_return is None:
            decomposition_reason = "target_return_unavailable"
        elif benchmark_return is None:
            decomposition_reason = "benchmark_unavailable"
        elif sector_return is None:
            market_component = benchmark_return
            decomposition_reason = "sector_unavailable"
        else:
            market_component = benchmark_return
            sector_excess = sector_return - market_component
            company_specific = float(target_return) - sector_return
            decomposition_available = True
        if decomposition_reason:
            flags.add(decomposition_reason)

        peer_pairs = tuple(sorted((ticker, value if _finite(value) else None) for ticker, value in inputs.peer_returns_by_ticker.items()))
        finite_peers = [float(value) for _, value in peer_pairs if _finite(value)]
        if finite_peers:
            peer_median: float | Literal["not_available"] = float(statistics.median(finite_peers))
            target_vs_peers: float | Literal["not_available"] = (
                float(target_return) - peer_median if target_return is not None else "not_available"
            )
        else:
            peer_median = "not_available"
            target_vs_peers = "not_available"
            flags.add("peer_returns_unavailable")

        return ContextBuilderArtifact(
            ticker=inputs.ticker,
            session_date=inputs.session_date.isoformat(),
            cutoff=inputs.cutoff,
            target_close=inputs.target_close,
            previous_target_close=inputs.previous_target_close,
            target_return_pct=target_return,
            target_volume=inputs.target_volume,
            expected_prior_sessions=tuple(inputs.expected_prior_sessions),
            volume_valid_sessions=tuple(valid_sessions),
            volume_prior_values=tuple(prior_values),
            volume_denominator=volume_denominator,
            volume_ratio=volume_ratio,
            volume_context_available=volume_available,
            volume_unavailable_reason=volume_reason,
            benchmark_ticker=inputs.benchmark_ticker,
            benchmark_return_pct=benchmark_return,
            sector_ticker=inputs.sector_ticker,
            sector_return_pct=sector_return,
            market_component=market_component,
            sector_excess=sector_excess,
            company_specific=company_specific,
            decomposition_available=decomposition_available,
            decomposition_unavailable_reason=decomposition_reason,
            peer_returns=peer_pairs,
            peer_median=peer_median,
            target_vs_peers=target_vs_peers,
            data_quality_flags=tuple(sorted(flags)),
        )


def canonical_context_bytes(artifact: ContextBuilderArtifact | dict[str, Any]) -> bytes:
    payload = artifact.model_dump(mode="json") if isinstance(artifact, ContextBuilderArtifact) else artifact
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


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
    elif abs(target - reference) <= aligned_residual_pct:
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
    finite_values = [float(value) for _, value in pairs if _finite(value)]
    available = len(finite_values)
    if expected > 0 and available == expected:
        coverage = AvailabilityState.AVAILABLE
    elif available > 0:
        coverage = AvailabilityState.PARTIAL
    else:
        coverage = AvailabilityState.UNAVAILABLE
    reason_codes: list[str] = []
    if expected == 0:
        reason_codes.append("peer_universe_unavailable")
    elif available < expected:
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
        expected_peer_count=max(expected, minimum_peer_count),
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
    ) -> MoveProfile:
        del temporal_identity  # request identity is carried by the execution path
        inputs = self.provider.load_context_inputs(
            ticker=ticker, session_date=session_date, cutoff=cutoff
        )
        target_return = _return_pct(inputs.target_close, inputs.previous_target_close)
        prior_return = _return_pct(
            inputs.previous_target_close, inputs.previous_2_target_close
        )
        gap_return = _return_pct(inputs.target_open, inputs.previous_target_close)
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

        macro_flags = tuple(
            sorted(inputs.scheduled_macro_flags, key=lambda flag: (flag.name, str(flag.scheduled_at)))
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
        )


__all__ = [
    "ContextBuilder",
    "ContextBuilderArtifact",
    "ObservationBuilder",
    "canonical_context_bytes",
]
