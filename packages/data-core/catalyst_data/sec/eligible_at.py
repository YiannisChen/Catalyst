"""SEC fail-closed eligibility (M3-3; execution-lock §D).

Accepted time is authoritative for ``eligible_at``. When it is unavailable the
row fails closed: filing date is never used as an after-close shortcut. The
declared conservative latest-plausible-instant rule applies only under
operator approval (Q-005) and is surfaced via ``approve_latest_plausible``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


@dataclass(frozen=True)
class EligibleAtResult:
    eligible_at: datetime | None          # None => not eligible
    eligible_at_reason: str
    temporal_precision: str
    accepted_time_recovered: bool
    fail_closed: bool


def latest_plausible_instant(d: str | date) -> datetime:
    """The instant at which date ``d`` ends in America/New_York (Q-005 rule).

    ``(d + 1 day) 00:00:00 America/New_York`` converted to UTC, e.g.
    2026-01-05 (EST) -> 2026-01-06T05:00:00Z; 2026-05-01 (EDT) ->
    2026-05-02T04:00:00Z.
    """
    if isinstance(d, str):
        d = date.fromisoformat(d)
    midnight_et = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=ET)
    return midnight_et.astimezone(UTC).replace(microsecond=0)


def derive_eligible_at(
    filing_row: object,
    accepted_time: datetime | None,
    *,
    approve_latest_plausible: bool = False,
) -> EligibleAtResult:
    """Derive the filing's ``eligible_at`` from the authoritative accepted time.

    Fail-closed default: no accepted time -> ``eligible_at=None``,
    ``fail_closed=True``, ``eligible_at_reason='fail_closed_no_accepted_time'``.
    ``filed_at`` is never used as an after-close shortcut. Under Q-005 approval
    the declared conservative rule (``latest_plausible_instant``) applies to the
    date-only ``filed_at``.
    """
    if accepted_time is not None:
        if accepted_time.tzinfo is None or accepted_time.utcoffset() is None:
            return EligibleAtResult(
                eligible_at=None,
                eligible_at_reason="fail_closed_no_accepted_time",
                temporal_precision="unknown",
                accepted_time_recovered=False,
                fail_closed=True,
            )
        return EligibleAtResult(
            eligible_at=accepted_time.astimezone(UTC).replace(microsecond=0),
            eligible_at_reason="accepted_time_recovered",
            temporal_precision="accepted_time",
            accepted_time_recovered=True,
            fail_closed=False,
        )
    filed_at = (filing_row or {}).get("filed_at") if isinstance(filing_row, dict) else None
    if approve_latest_plausible and filed_at:
        return EligibleAtResult(
            eligible_at=latest_plausible_instant(filed_at),
            eligible_at_reason="latest_plausible_approved",
            temporal_precision="date_only_latest_plausible",
            accepted_time_recovered=False,
            fail_closed=False,
        )
    return EligibleAtResult(
        eligible_at=None,
        eligible_at_reason="fail_closed_no_accepted_time",
        temporal_precision="unknown",
        accepted_time_recovered=False,
        fail_closed=True,
    )


def persist_filing_temporal_repair(
    conn: sqlite3.Connection,
    *,
    filing_id: str,
    result: EligibleAtResult,
    accepted_time: datetime | None,
) -> None:
    """Atomically write the v14 ``filings`` temporal repair columns.

    Never writes canonical rows; M3-5B consumes these columns. An unknown
    ``filing_id`` fails closed.
    """
    exists = conn.execute(
        "SELECT 1 FROM filings WHERE filing_id=?", (filing_id,)
    ).fetchone()
    if not exists:
        raise ValueError(f"unknown filing_id: {filing_id}")

    def _z(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).replace(microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    conn.execute(
        """UPDATE filings SET
               accepted_time_utc = ?,
               eligible_at = ?,
               eligible_at_reason = ?,
               temporal_precision = ?,
               accepted_time_recovered = ?,
               eligibility_fail_closed = ?
           WHERE filing_id = ?""",
        (
            _z(accepted_time),
            _z(result.eligible_at),
            result.eligible_at_reason,
            result.temporal_precision,
            int(result.accepted_time_recovered),
            int(result.fail_closed),
            filing_id,
        ),
    )
    conn.commit()


__all__ = [
    "EligibleAtResult",
    "derive_eligible_at",
    "latest_plausible_instant",
    "persist_filing_temporal_repair",
]
