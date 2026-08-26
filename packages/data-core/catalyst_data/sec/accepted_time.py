"""SEC accepted-time recovery (M3-3; execution-lock §D).

The authoritative accepted time comes from EDGAR submissions JSON
(``raw_assets.source_type='sec_filings'``): ``recent.accessionNumber`` and
``recent.acceptanceDateTime`` are parallel arrays and the accepted time for an
accession is the value at the unique matching index. Every ambiguous or
misaligned case fails closed (returns ``None``).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

_COMPACT = re.compile(r"^\d{14}$")


def parse_edgar_acceptance_datetime(value: object) -> datetime | None:
    """Parse one EDGAR acceptance datetime value to aware UTC (or None).

    Accepted forms:
    - ISO-8601 with ``Z`` suffix;
    - ISO-8601 with an explicit UTC offset (converted to UTC);
    - EDGAR compact ``YYYYMMDDHHMMSS`` interpreted in ``America/New_York``
      then converted to UTC (EST and EDT cases).

    Rejected forms (all fail closed): naive ISO datetimes, malformed strings,
    DST-ambiguous instants, and date-only input (never ``T00:00:00Z``).
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    # Date-only input is never promoted to midnight UTC.
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return None
    if _COMPACT.match(raw):
        return _parse_compact(raw)
    if raw.endswith("Z"):
        text = raw[:-1] + "+00:00"
    elif re.search(r"[+-]\d{2}:?\d{2}$", raw):
        text = raw
    else:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(UTC).replace(microsecond=0)


def _parse_compact(raw: str) -> datetime | None:
    year, month, day = int(raw[0:4]), int(raw[4:6]), int(raw[6:8])
    hour, minute, second = int(raw[8:10]), int(raw[10:12]), int(raw[12:14])
    try:
        wall = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    # DST-ambiguous instant (fall-back): the same wall time maps to two offsets.
    fold0 = wall.replace(tzinfo=ET, fold=0)
    fold1 = wall.replace(tzinfo=ET, fold=1)
    if fold0.utcoffset() != fold1.utcoffset():
        return None
    # Nonexistent wall time (spring-forward gap): the UTC round-trip changes the
    # wall clock, so the instant does not exist in America/New_York.
    back = fold0.astimezone(UTC).astimezone(ET)
    if (back.hour, back.minute, back.second) != (hour, minute, second):
        return None
    return fold0.astimezone(UTC).replace(microsecond=0)


def recover_accepted_time(
    raw_payload: object, *, accession: str
) -> datetime | None:
    """Return the authoritative accepted time for exactly ``accession``.

    ``recent.accessionNumber`` and ``recent.acceptanceDateTime`` are parallel
    arrays; the target accession must match exactly once and the arrays must
    have equal length. Missing target, duplicate target, unequal/malformed
    arrays, or a malformed value at the target index fail closed (``None``).
    """
    if not isinstance(raw_payload, dict):
        return None
    recent = raw_payload.get("recent")
    if not isinstance(recent, dict):
        return None
    accessions = recent.get("accessionNumber")
    times = recent.get("acceptanceDateTime")
    if not isinstance(accessions, list) or not isinstance(times, list):
        return None
    if len(accessions) != len(times):
        return None
    indexes = [i for i, item in enumerate(accessions) if item == accession]
    if len(indexes) != 1:
        return None
    return parse_edgar_acceptance_datetime(times[indexes[0]])


__all__ = ["parse_edgar_acceptance_datetime", "recover_accepted_time"]
