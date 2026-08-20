"""V1.1 MoveProfile typed contract (M2-3).

Agents-owned null-safe observation interpretation (Frozen §6.2). M2 defines
schemas only; classification formulas land in M4. Unknown/degraded fields stay
explicit (None + degraded_fields[]) and are never inferred.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

CoverageState = Literal["full", "partial", "unknown"]
VolumeBand = Literal["NORMAL", "ELEVATED", "EXTREME", "UNKNOWN"]
AlignmentBand = Literal["ALIGNED", "PARTIAL", "UNALIGNED", "UNKNOWN"]

CoverageFlag: TypeAlias = str


class PeerSummary(BaseModel):
    """Equal-weight peer median/pct summary with an explicit coverage state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    median_return_pct: float | None = None
    pct_25_return_pct: float | None = None
    pct_75_return_pct: float | None = None
    coverage: CoverageState


class VolumeAbnormality(BaseModel):
    """Typed volume band; unknown is explicit, never normal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: float | None = None
    band: VolumeBand


class SessionAlignment(BaseModel):
    """One-session directional/magnitude alignment, not statistical correlation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: float | None = None
    band: AlignmentBand


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
    "CoverageFlag",
    "CoverageState",
    "DegradedField",
    "MoveProfile",
    "PeerSummary",
    "ScenarioPredicateInputs",
    "ScheduledMacroFlag",
    "SessionAlignment",
    "VolumeAbnormality",
    "VolumeBand",
]
