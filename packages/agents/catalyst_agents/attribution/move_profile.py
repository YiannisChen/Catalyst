"""V1.1 MoveProfile typed contract (M2-3, corrective).

Agents-owned null-safe observation interpretation (Frozen §6.2). The nested
shapes and shared enums are the exact Phase 3 TSD §4 contract: AvailabilityState,
AlignmentBand, DirectionRelation, VolumeBand. No invented percentile fields are
retained. M2 defines schemas only; classification formulas land in M4. Unknown
and degraded fields stay explicit and are never inferred.
"""
from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class AvailabilityState(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class AlignmentBand(str, Enum):
    ALIGNED = "ALIGNED"
    DIVERGENT = "DIVERGENT"
    UNKNOWN = "UNKNOWN"


class DirectionRelation(str, Enum):
    SAME_DIRECTION = "SAME_DIRECTION"
    OPPOSITE_DIRECTION = "OPPOSITE_DIRECTION"
    TARGET_FLAT = "TARGET_FLAT"
    REFERENCE_FLAT = "REFERENCE_FLAT"
    UNKNOWN = "UNKNOWN"


class VolumeBand(str, Enum):
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    EXTREME = "EXTREME"
    UNKNOWN = "UNKNOWN"


CoverageFlag: TypeAlias = str
ReferenceKind = Literal["MARKET", "SECTOR", "PEER_MEDIAN"]


class PeerReturn(BaseModel):
    """One bounded point-in-time peer return (Phase 3 TSD §4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    return_pct: float

    @field_validator("return_pct")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("peer return must be finite")
        return value


class PeerSummary(BaseModel):
    """Equal-weight peer median summary with explicit coverage (Phase 3 §4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    expected_peer_count: int
    available_peer_count: int
    peer_returns: tuple[PeerReturn, ...] = ()
    median_return_pct: float | None = None
    target_minus_median_pct: float | None = None
    coverage_state: AvailabilityState
    reason_codes: tuple[str, ...] = ()

    @field_validator("expected_peer_count")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("expected_peer_count must be non-negative")
        return value

    @model_validator(mode="after")
    def _counts_and_order(self) -> "PeerSummary":
        if self.available_peer_count < 0 or self.available_peer_count > self.expected_peer_count:
            raise ValueError(
                "available_peer_count must be between 0 and expected_peer_count"
            )
        if len(self.peer_returns) != self.available_peer_count:
            raise ValueError("peer_returns length must equal available_peer_count")
        tickers = [peer.ticker for peer in self.peer_returns]
        if tickers != sorted(tickers):
            raise ValueError("peer_returns must be sorted by ticker")
        return self


class VolumeAbnormality(BaseModel):
    """Typed volume band with explicit availability (Phase 3 §4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    target_volume: float | None = None
    baseline_median_volume: float | None = None
    ratio: float | None = None
    expected_session_count: int
    valid_session_count: int
    band: VolumeBand
    availability: AvailabilityState
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _counts_and_unknown_semantics(self) -> "VolumeAbnormality":
        if self.expected_session_count < 0:
            raise ValueError("expected_session_count must be non-negative")
        if self.valid_session_count < 0 or self.valid_session_count > self.expected_session_count:
            raise ValueError(
                "valid_session_count must be between 0 and expected_session_count"
            )
        if self.availability is AvailabilityState.UNAVAILABLE:
            if self.band is not VolumeBand.UNKNOWN:
                raise ValueError("unavailable volume must be band UNKNOWN, never NORMAL")
            if self.ratio is not None:
                raise ValueError("unavailable volume must not fabricate a ratio")
        return self


class SessionAlignment(BaseModel):
    """One-session directional/magnitude alignment, not correlation (Phase 3 §4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    reference_kind: ReferenceKind
    target_return_pct: float | None = None
    reference_return_pct: float | None = None
    residual_return_pct: float | None = None
    direction_relation: DirectionRelation
    band: AlignmentBand
    availability: AvailabilityState
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _unavailable_is_unknown(self) -> "SessionAlignment":
        if self.availability is AvailabilityState.UNAVAILABLE:
            if self.band is not AlignmentBand.UNKNOWN:
                raise ValueError("unavailable alignment must be band UNKNOWN")
            if self.direction_relation is not DirectionRelation.UNKNOWN:
                raise ValueError("unavailable alignment must have UNKNOWN direction")
        return self


class ScheduledMacroFlag(BaseModel):
    """A scheduled macro release observed inside the information window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    scheduled_at: datetime | None = None


class DegradedField(BaseModel):
    """An explicit field-level degradation with its reason code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    reason_code: str


class MoveProfile(BaseModel):
    """Null-safe observation interpretation (Frozen §6.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_return: float | None = None
    prior_session_return: float | None = None
    gap_return: float | None = None
    market_return: float | None = None
    sector_return: float | None = None
    peer_summary: PeerSummary | None = None
    market_adjusted_return: float | None = None
    sector_adjusted_return: float | None = None
    volume_abnormality: VolumeAbnormality | None = None
    scheduled_macro_flags: tuple[ScheduledMacroFlag, ...] = ()
    market_comove: SessionAlignment | None = None
    sector_comove: SessionAlignment | None = None
    peer_comove: SessionAlignment | None = None
    coverage_flags: tuple[CoverageFlag, ...] = ()
    degraded_fields: tuple[DegradedField, ...] = ()


class ScenarioPredicateInputs(BaseModel):
    """Typed inputs to the deterministic scenario classification predicates.

    Deliberately contains no news/evidence fields: CONTINUATION and the other
    InitialResearchPolicy predicates must not depend on evidence that has not
    yet been researched (Frozen §6.2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_return_pct: float | None = None
    prior_session_return_pct: float | None = None
    market_return_pct: float | None = None
    sector_return_pct: float | None = None
    peer_return_pct: float | None = None
    volume_band: VolumeBand | None = None
    macro_flags: tuple[ScheduledMacroFlag, ...] = ()


__all__ = [
    "AlignmentBand",
    "AvailabilityState",
    "CoverageFlag",
    "DegradedField",
    "DirectionRelation",
    "MoveProfile",
    "PeerReturn",
    "PeerSummary",
    "ReferenceKind",
    "ScenarioPredicateInputs",
    "ScheduledMacroFlag",
    "SessionAlignment",
    "VolumeAbnormality",
    "VolumeBand",
]
