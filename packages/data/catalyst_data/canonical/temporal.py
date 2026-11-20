"""V1.1 TemporalIdentity contract (M2-1).

Data-core owns the exchange-calendar-backed temporal identity used by every
retrieval arm (Frozen §5.3, §5.4). All temporal datetimes are normalized to
UTC: naive timestamps and mixed/foreign timezone offsets are rejected so PIT
ordering semantics stay unambiguous (data-core TSD §7).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_serializer,
    model_validator,
)

from catalyst_data.canonical._immutable import NoUncheckedCopyUpdates


class TemporalIdentity(NoUncheckedCopyUpdates, BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_date: str
    market_timezone: str
    session_open_at: datetime
    session_close_at: datetime
    information_window_start_at: datetime
    cutoff_at: datetime
    # Optional publication bounds. The session cutoff remains authoritative for
    # event-time checks; these bounds are populated only by event_time_v1.
    evidence_window_start_at: datetime | None = None
    evidence_window_end_at: datetime | None = None

    @model_serializer(mode="wrap")
    def _omit_optional_publication_window(self, handler):
        data = handler(self)
        if self.evidence_window_start_at is None:
            data.pop("evidence_window_start_at", None)
            data.pop("evidence_window_end_at", None)
        return data

    @field_validator(
        "session_open_at",
        "session_close_at",
        "information_window_start_at",
        "cutoff_at",
        "evidence_window_start_at",
        "evidence_window_end_at",
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
        if (self.evidence_window_start_at is None) != (self.evidence_window_end_at is None):
            raise ValueError("evidence publication window bounds must be set together")
        if self.evidence_window_start_at is not None:
            if self.evidence_window_start_at > self.evidence_window_end_at:
                raise ValueError("evidence publication window start must not exceed end")
        return self

    def publication_window(self) -> tuple[datetime, datetime]:
        """Return the separately configured publication bounds or legacy bounds."""
        return (
            self.evidence_window_start_at or self.information_window_start_at,
            self.evidence_window_end_at or self.cutoff_at,
        )

    def contains(self, eligible_at: datetime) -> bool:
        """True when a point-in-time timestamp falls inside the information window."""
        return self.information_window_start_at <= eligible_at <= self.cutoff_at


def build_temporal_identity(session_date: str) -> TemporalIdentity:
    """Derive the run TemporalIdentity from the authoritative data-core US
    trading calendar (Final Migration TSD §14; data-core §7)."""
    from catalyst_data.trading_calendar import (
        latest_closed_trading_day_for_date,
        session_close_utc,
        session_open_utc,
    )

    if not session_date:
        raise ValueError("session_date is required for temporal identity")
    try:
        open_iso = session_open_utc(session_date)
        close_iso = session_close_utc(session_date)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    open_at = datetime.fromisoformat(open_iso.replace("Z", "+00:00"))
    close_at = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
    prior_day = latest_closed_trading_day_for_date(
        datetime.fromisoformat(session_date).date() - timedelta(days=1)
    )
    prior_close_iso = session_close_utc(prior_day.isoformat())
    prior_close_at = datetime.fromisoformat(prior_close_iso.replace("Z", "+00:00"))
    return TemporalIdentity(
        session_date=session_date,
        market_timezone="America/New_York",
        session_open_at=open_at,
        session_close_at=close_at,
        information_window_start_at=prior_close_at,
        cutoff_at=close_at,
    )


def with_event_time_publication_window(
    identity: TemporalIdentity, *, now: datetime | None = None, start_sessions_back: int = 4
) -> TemporalIdentity:
    """Add D-3..D+3 trading-session publication bounds without moving cutoff_at.

    The window opens at the close of the session ``start_sessions_back`` before D (4: D-4's close, i.e. D-3 onward;
    1: the prior close, the v2 rule "window") and ends at D+3's close or now, whichever is earlier."""

    from catalyst_data.trading_calendar import (
        CALENDAR_YEARS,
        calendar_trading_days,
        session_close_utc,
    )

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    days = calendar_trading_days(
        f"{CALENDAR_YEARS[0]}-01-01",
        f"{CALENDAR_YEARS[1]}-12-31",
    )
    index = days.index(identity.session_date)
    first = days[max(0, index - start_sessions_back)]
    last = days[min(len(days) - 1, index + 3)]
    start = datetime.fromisoformat(session_close_utc(first).replace("Z", "+00:00"))
    end = datetime.fromisoformat(session_close_utc(last).replace("Z", "+00:00"))
    end = min(end, now.astimezone(timezone.utc))
    payload = identity.model_dump(mode="python")
    payload.update({
        "evidence_window_start_at": start,
        "evidence_window_end_at": end,
    })
    return TemporalIdentity.model_validate(payload)

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


__all__ = ["TemporalIdentity", "build_temporal_identity", "with_event_time_publication_window", "UTC_ISO_Z_FORMAT", "utc_iso_z"]
