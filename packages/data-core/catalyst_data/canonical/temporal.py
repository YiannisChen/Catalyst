"""V1.1 TemporalIdentity contract (M2-1).

Data-core owns the exchange-calendar-backed temporal identity used by every
retrieval arm (Frozen §5.3, §5.4). All temporal datetimes are normalized to
UTC: naive timestamps and mixed/foreign timezone offsets are rejected so PIT
ordering semantics stay unambiguous (data-core TSD §7).
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from catalyst_data.canonical._immutable import NoUncheckedCopyUpdates


class TemporalIdentity(NoUncheckedCopyUpdates, BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_date: str
    market_timezone: str
    session_open_at: datetime
    session_close_at: datetime
    information_window_start_at: datetime
    cutoff_at: datetime

    @field_validator(
        "session_open_at",
        "session_close_at",
        "information_window_start_at",
        "cutoff_at",
    )
    @classmethod
    def _utc_normalized(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("temporal timestamps must be timezone-aware")
        if value.utcoffset().total_seconds() != 0:
            raise ValueError("temporal timestamps must be normalized to UTC")
        return value

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


UTC_ISO_Z_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_iso_z(value: datetime) -> str:
    """Serialize a datetime as canonical second-resolution UTC ``...Z``.

    This is the only canonical rendering used at the retrieval/context
    boundary. ``datetime.isoformat()`` renders UTC as ``...+00:00``, which the
    retrieval contract rejects as ``invalid_cutoff`` and which mis-orders
    against the canonical ``...Z`` timestamps persisted in SQLite when compared
    as strings.

    The value is not reinterpreted: it is converted to UTC and rendered at
    second resolution. Naive datetimes are rejected so a local wall-clock time
    can never be silently treated as UTC.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("utc_iso_z requires a timezone-aware datetime")
    return value.astimezone(timezone.utc).strftime(UTC_ISO_Z_FORMAT)


__all__ = ["TemporalIdentity", "UTC_ISO_Z_FORMAT", "utc_iso_z"]
