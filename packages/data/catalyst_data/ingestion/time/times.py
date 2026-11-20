"""Publication times a news API gives against the time the page itself declares (2026-10-22 time-zone review).

Finnhub gives CNBC articles the New York clock time labelled as UTC (4 h early in EDT, 5 h in EST), and Benzinga
pages fetched 2026-09-30..10-08 were stored 4 h early. The leak audit checks stored times, so it cannot see this.
For company bodies, therefore:

- both times are kept (``page_versions.body_times``);
- when the page declares a time-zone-aware publication time, ``eligible_at`` is the later of the two (never earlier
  than either, so an after-close report cannot look like a pre-close one);
- a difference of a whole 4 or 5 hours (within 2 minutes) is flagged ``suspected_tz``;
- when the page declares no usable time and the (provider, publisher) is known to be skewed, the time is
  ``imprecise`` and the body is not stored, rather than trusting the API's time.

Times without a time zone are never given a default one anywhere in intake (they are treated as missing).
"""
from __future__ import annotations

from datetime import datetime

from catalyst_data.ingestion.time import timefields

# (provider, publisher) pairs whose API time is known to be wrong; see data/corrections/README.md on data/collectors.
SKEWED = {("finnhub", "CNBC"), ("finnhub", "Benzinga")}  # Benzinga: 86 of 86 headlines 4 h early against Alpaca (2026-10-22)
TZ_HOURS = (4, 5)          # New York's offsets from UTC
TOLERANCE_S = 120


def utc_or_none(value) -> datetime | None:
    """ISO 8601 with Z or an offset → aware UTC datetime; a value without a time zone → None (never assumed)."""
    return timefields.stored_or_none(value)


stamp = timefields.stamp


def whole_hours(diff_seconds: float, hours=TZ_HOURS) -> int | None:
    """The whole number of hours in ``hours`` that |diff| equals within TOLERANCE_S, else None."""
    for h in hours:
        if abs(abs(diff_seconds) - h * 3600) <= TOLERANCE_S:
            return h
    return None


def reconcile(api_time: str | None, page_time: str | None, *, provider: str | None,
              publisher: str | None) -> tuple[str | None, str]:
    """(eligible_at or None, flag). Flags: agree, page_later, api_later, suspected_tz, api_only, imprecise."""
    api, page = utc_or_none(api_time), utc_or_none(page_time)
    if page is not None and api is not None:
        diff = (api - page).total_seconds()
        chosen = max(api, page)
        if whole_hours(diff) is not None:
            return stamp(chosen), "suspected_tz"
        if abs(diff) <= TOLERANCE_S:
            return stamp(chosen), "agree"
        return stamp(chosen), "page_later" if page > api else "api_later"
    if page is not None:
        return stamp(page), "page_only"
    if api is None or (provider, publisher) in SKEWED:
        return None, "imprecise"
    return stamp(api), "api_only"
