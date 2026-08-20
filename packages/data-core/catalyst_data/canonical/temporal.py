"""V1.1 TemporalIdentity contract (M2-1).

Data-core owns the exchange-calendar-backed temporal identity used by every
retrieval arm (Frozen §5.3, §5.4).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator


class TemporalIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_date: str
    market_timezone: str
    session_open_at: datetime
    session_close_at: datetime
    information_window_start_at: datetime
    cutoff_at: datetime

    @model_validator(mode="after")
    def _temporal_order(self) -> "TemporalIdentity":
        if self.session_open_at > self.session_close_at:
            raise ValueError("session_open_at must not exceed session_close_at")
        if self.information_window_start_at > self.cutoff_at:
            raise ValueError("information_window_start_at must not exceed cutoff_at")
        return self

    def contains(self, eligible_at: datetime) -> bool:
        """True when a point-in-time timestamp falls inside the information window."""
        return self.information_window_start_at <= eligible_at <= self.cutoff_at


__all__ = ["TemporalIdentity"]
