"""Shared UTC timestamp normalization for Pre-B6 corpus identity."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any


class TimestampNormalizationError(ValueError):
    """Malformed or unsupported timestamp for UTC second-resolution Z output."""


def normalize_utc_second_z(value: Any) -> str:
    """Normalize to UTC second-resolution string ending with Z.

    Contract:
    - date-only ``YYYY-MM-DD`` → ``YYYY-MM-DDT00:00:00Z`` (UTC midnight)
    - aware datetimes converted to UTC
    - fractional seconds removed after conversion
    - naive datetime treated as already-UTC (explicit contract)
    - empty/None/malformed → raise TimestampNormalizationError (no 1970 fallback)
    """
    if value is None:
        raise TimestampNormalizationError("timestamp is None")
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            # Contract: naive datetime is treated as UTC wall time
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        dt = dt.replace(microsecond=0)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date) and not isinstance(value, datetime):
        return f"{value.isoformat()}T00:00:00Z"
    if not isinstance(value, str):
        raise TimestampNormalizationError(f"unsupported timestamp type: {type(value)!r}")
    raw = value.strip()
    if not raw:
        raise TimestampNormalizationError("empty timestamp")
    # date-only
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        try:
            date.fromisoformat(raw)
        except ValueError as exc:
            raise TimestampNormalizationError(f"malformed date: {raw}") from exc
        return f"{raw}T00:00:00Z"
    # Normalize Z suffix to +00:00 for fromisoformat
    text = raw
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimestampNormalizationError(f"malformed timestamp: {raw}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    dt = dt.replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
