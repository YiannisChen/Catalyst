from __future__ import annotations

import json
import math
import statistics
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from catalyst_agents.attribution.provider import ContextProvider


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
