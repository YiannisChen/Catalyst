"""SEC fail-closed eligibility (M3-3; execution-lock §D; Batch A A3/A4/A5).

Accepted time is authoritative for ``eligible_at``. When it is unavailable the
row fails closed: filing date is never used as an after-close shortcut. A valid
date-only ``filed_at`` with no accepted time fails closed as
``fail_closed_no_time_of_day`` / ``unknown_time_of_day``; a missing or unusable
``filed_at`` fails closed as ``fail_closed_no_accepted_time`` / ``unknown``.
The declared conservative latest-plausible-instant rule applies only under
operator approval (Q-005) and is surfaced via ``approve_latest_plausible``.
``derive_eligible_at`` is the fail-closed boundary: it never raises for a
malformed ``filed_at``. Persistence is result-authoritative and rejects
contradictory arguments without writing.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

_DATE_ONLY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


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
    2026-05-02T04:00:00Z. Strict: malformed input raises (derive_eligible_at
    is the fail-closed boundary and never calls this with unusable values).
    """
    if isinstance(d, str):
        d = date.fromisoformat(d)
    midnight_et = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=ET)
    return midnight_et.astimezone(UTC).replace(microsecond=0)


def _read_filed_at(filing_row: object) -> object:
    """Read ``filed_at`` from None/dict/mapping-like rows (sqlite3.Row)."""
    if filing_row is None:
        return None
    try:
        return filing_row["filed_at"]
    except (KeyError, IndexError, TypeError):
        return None


def _is_valid_date_only(value: object) -> bool:
    """True only for a ``datetime.date`` (not ``datetime``) or a date-only str.

    Timestamp-shaped strings (``YYYY-MM-DDTHH:MM:SSZ``), naive/aware datetimes,
    integers, and malformed values are unusable (Batch A A3/A4).
    """
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    if isinstance(value, str):
        if not _DATE_ONLY_RE.fullmatch(value):
            return False
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return False


def _fail_closed_no_accepted_time() -> EligibleAtResult:
    return EligibleAtResult(
        eligible_at=None,
        eligible_at_reason="fail_closed_no_accepted_time",
        temporal_precision="unknown",
        accepted_time_recovered=False,
        fail_closed=True,
    )


def derive_eligible_at(
    filing_row: object,
    accepted_time: datetime | None,
    *,
    approve_latest_plausible: bool = False,
) -> EligibleAtResult:
    """Derive the filing's ``eligible_at`` from the authoritative accepted time.

    Priority (execution-lock §D, Batch A A3):
    1. aware ``accepted_time`` -> recovered;
    2. naive ``accepted_time`` -> fail_closed_no_accepted_time / unknown;
    3. valid date-only ``filed_at``, no approval -> fail_closed_no_time_of_day /
       unknown_time_of_day (never an after-close shortcut);
    4. valid date-only ``filed_at``, approved -> latest_plausible_instant;
    5. missing/unusable ``filed_at`` -> fail_closed_no_accepted_time / unknown,
       without raising and without calling ``latest_plausible_instant``.
    """
    if accepted_time is not None:
        if accepted_time.tzinfo is None or accepted_time.utcoffset() is None:
            return _fail_closed_no_accepted_time()
        return EligibleAtResult(
            eligible_at=accepted_time.astimezone(UTC).replace(microsecond=0),
            eligible_at_reason="accepted_time_recovered",
            temporal_precision="accepted_time",
            accepted_time_recovered=True,
            fail_closed=False,
        )

    filed_at = _read_filed_at(filing_row)
    if _is_valid_date_only(filed_at):
        if approve_latest_plausible:
            return EligibleAtResult(
                eligible_at=latest_plausible_instant(filed_at),
                eligible_at_reason="latest_plausible_approved",
                temporal_precision="date_only_latest_plausible",
                accepted_time_recovered=False,
                fail_closed=False,
            )
        return EligibleAtResult(
            eligible_at=None,
            eligible_at_reason="fail_closed_no_time_of_day",
            temporal_precision="unknown_time_of_day",
            accepted_time_recovered=False,
            fail_closed=True,
        )
    return _fail_closed_no_accepted_time()


def _is_aware(value: datetime | None) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def persist_filing_temporal_repair(
    conn: sqlite3.Connection,
    *,
    filing_id: str,
    result: EligibleAtResult,
    accepted_time: datetime | None,
) -> None:
    """Atomically write the v14 ``filings`` temporal repair columns.

    Result-authoritative (Batch A A5): validate, then write, then commit. On
    validation failure raise ``ValueError`` and write nothing.

    - ``accepted_time_recovered=True`` requires ``fail_closed=False``, an aware
      ``result.eligible_at``, an aware ``accepted_time``, and UTC-second
      equality between them; both columns persist the same Z string.
    - ``accepted_time_recovered=False`` requires ``accepted_time is None``;
      ``accepted_time_utc`` persists NULL. ``fail_closed=True`` requires
      ``eligible_at is None``; the only non-fail-closed unrecovered result is an
      approved latest-plausible instant with an aware ``eligible_at``.
    """
    exists = conn.execute(
        "SELECT 1 FROM filings WHERE filing_id=?", (filing_id,)
    ).fetchone()
    if not exists:
        raise ValueError(f"unknown filing_id: {filing_id}")

    if result.accepted_time_recovered:
        if result.fail_closed:
            raise ValueError("accepted_time_recovered result cannot be fail_closed")
        if not _is_aware(result.eligible_at):
            raise ValueError("accepted_time_recovered result requires aware eligible_at")
        if not _is_aware(accepted_time):
            raise ValueError("accepted_time_recovered result requires aware accepted_time")
        if _z(accepted_time) != _z(result.eligible_at):
            raise ValueError(
                "accepted_time and result.eligible_at must agree"
            )
        accepted_time_utc = _z(accepted_time)
        eligible_at = _z(result.eligible_at)
    else:
        if accepted_time is not None:
            raise ValueError(
                "unrecovered result requires accepted_time=None"
            )
        accepted_time_utc = None
        if result.fail_closed:
            if result.eligible_at is not None:
                raise ValueError(
                    "fail_closed result cannot carry a non-null eligible_at"
                )
            eligible_at = None
        else:
            if not _is_aware(result.eligible_at):
                raise ValueError(
                    "non-fail-closed unrecovered result requires aware eligible_at"
                )
            eligible_at = _z(result.eligible_at)

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
            accepted_time_utc,
            eligible_at,
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
