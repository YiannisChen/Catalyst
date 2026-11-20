"""One place where a time from outside becomes the time we store (2026-10-23).

Every intake path turns a provider's or a page's raw time into UTC here, and only here
(tests/test_m9_timefields.py fails on any other parsing of an external time under ``m9/``). The stored form is
always ``YYYY-MM-DDTHH:MM:SSZ`` (UTC, to the second). Session logic (the window (previous trading day 16:00 ET,
D 16:00 ET], before the open, after the close) converts back to New York with ``ny()``.

Each (source, field) has a declared meaning in FIELDS: what the raw value really is, which may differ from what
it says. Two such cases are known, and both are kept as they are, with a flag for the caller, rather than shifted:

- Finnhub's ``datetime`` for CNBC and Benzinga articles is the New York clock labelled UTC (4 h early in EDT, 5 h
  in EST): flag ``known_skew``; ``times.reconcile`` decides with the page's own time.
- The SEC submissions JSON's ``acceptanceDateTime`` is 4 or 5 h late for about 60% of issuers: flag
  ``unreliable``; the filing index page's "Accepted" (``ny_local``) is the time used.

A raw value without a time zone is never given one: it is missing (precision ``none``), except where the field is
declared as New York local time (the SEC index page) or as a date.

    from catalyst_data.ingestion import timefields as tf
    tv = tf.external(item["datetime"], source="finnhub", field="datetime", publisher=item.get("source"))
    tv.stamp   # "2026-10-22T20:10:51Z" or None
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# How to read a raw value.
#   iso_aware  ISO 8601 with Z or an offset (no offset: missing)
#   epoch_s    Unix seconds;   epoch_ms  Unix milliseconds
#   rfc2822    an RSS/HTTP date with a zone ("-0000", the RFC's "unknown zone", counts as missing)
#   iso_or_rfc ISO 8601 with a zone, else RFC 2822 with a zone (Tavily, web pages)
#   gdelt      YYYYMMDDTHHMMSSZ
#   ny_local   "YYYY-MM-DD HH:MM:SS" on a New York clock (the SEC filing index page)
#   naive_utc  an ISO value without a zone that is declared UTC by a check against an independent source
#   rfc2822_fixed_est  an RFC 2822 value whose writer converted the New York clock at a fixed -05:00
#   date       a calendar date (precision day; ``utc`` is None)
FIELDS: dict[tuple[str, str], str] = {
    ("http", "Retry-After"): "rfc2822",
    ("finnhub", "datetime"): "epoch_s",
    ("polygon", "published_utc"): "iso_aware",
    ("polygon", "bar_t"): "epoch_ms",
    ("alpaca", "created_at"): "iso_aware",
    ("alpaca", "updated_at"): "iso_aware",
    ("alpaca", "bar_t"): "iso_aware",
    ("sec", "acceptanceDateTime"): "iso_aware",
    ("sec", "index_accepted"): "ny_local",
    ("page", "declared"): "iso_aware",          # JSON-LD / meta tags of a fetched page
    ("kitco", "createdAt"): "iso_aware",
    ("benzinga", "datePublished"): "iso_aware",
    ("cnbc", "datePublished"): "iso_aware",
    ("tavily", "published_date"): "iso_or_rfc",
    ("rss", "pubDate"): "rfc2822",
    ("newsapi", "publishedAt"): "iso_aware",
    ("marketaux", "published_at"): "iso_aware",
    ("gdelt", "seendate"): "gdelt",
    ("seekingalpha", "pubDate"): "rfc2822",
    ("fmp", "publishedDate"): "iso_aware",
    ("yfinance", "GradeDate"): "iso_aware",
    ("finra", "date"): "date",
    ("federal_register", "publication_date"): "date",
    ("alphavantage", "time_published"): "gdelt",   # its compact form has no zone: missing, never assumed
    ("bea", "release_date"): "iso_aware",
    ("yfinance", "GradeDate_naive"): "naive_utc",  # Yahoo's GradeDate has no zone and is UTC (study 9b, 2026-10-22)
    ("file", "mtime"): "epoch_s",
    # NextEra's IR feed writes the New York clock converted at a fixed UTC-5 all year: 1 h late in EDT (2 of 2
    # releases against PR Newswire, 2026-10-21 and -22), right in EST.
    ("rss:ir:NEE", "pubDate"): "rfc2822_fixed_est",
}
# (source, publisher) whose times are known to be wrong in a fixed way; see the module docstring.
KNOWN_SKEW = {("finnhub", "CNBC"), ("finnhub", "Benzinga")}
# How far before a window to read Finnhub rows so a skewed time can still land inside it.
FINNHUB_SKEW_LOOKBACK_HOURS = 5
UNRELIABLE = {("sec", "acceptanceDateTime")}


@dataclass(frozen=True)
class TimeValue:
    utc: datetime | None              # aware, UTC; None when missing or date-only
    precision: str                    # second | minute | day | none
    source: str
    field: str
    day: date | None = None           # for precision "day"
    flags: tuple[str, ...] = dc_field(default=())

    @property
    def stamp(self) -> str | None:
        return stamp(self.utc) if self.utc is not None else None


def stamp(t: datetime) -> str:
    """The stored form. ``t`` must be aware (a naive datetime is a bug, not UTC)."""
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("naive datetime: give it a time zone before storing")
    value = t.astimezone(UTC)
    # A fractional second is already after this whole second. Storing it truncated
    # would put the event on the earlier second and let a cutoff miss it.
    if value.microsecond:
        value = value.replace(microsecond=0) + timedelta(seconds=1)
    return value.strftime(FORMAT)


def stored(value: str) -> datetime:
    """A time we wrote (or any ISO 8601 value with a zone) back to an aware UTC datetime; naive raises."""
    t = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError(f"time without a zone: {value!r}")
    return t.astimezone(UTC)


def stored_or_none(value: Any) -> datetime | None:
    try:
        return stored(value) if value else None
    except (TypeError, ValueError):
        return None


def unskew(value: str, *, source: str, publisher: str | None) -> str:
    """A stored stamp for a (source, publisher) in KNOWN_SKEW, read as the New York clock it really is.

    Finnhub labels CNBC and Benzinga New York wall time as UTC; the real instant is that wall time in New York.
    Other values are returned unchanged.
    """
    if (source, publisher) not in KNOWN_SKEW:
        return value
    wall = stored(value).replace(tzinfo=NY)
    return stamp(wall)


def finnhub_read_from(start: str) -> str:
    """Where to start reading Finnhub rows for a window starting at `start`: skewed publishers are stored
    up to FINNHUB_SKEW_LOOKBACK_HOURS early, so read back that far and filter after `unskew`."""
    return stamp(stored(start) - timedelta(hours=FINNHUB_SKEW_LOOKBACK_HOURS))


def ny(t: datetime) -> datetime:
    return t.astimezone(NY)


def ny_wall(day: str, hour: int, minute: int = 0) -> datetime:
    """A New York clock time on ``day`` (e.g. 16:00 for the close), aware."""
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=NY)


def _parse(raw: Any, how: str) -> tuple[datetime | None, date | None, str]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None, "none"
    text = str(raw).strip()
    try:
        if how == "epoch_s":
            return (datetime.fromtimestamp(int(float(text)), UTC), None, "second") if float(text) > 0 else (None, None, "none")
        if how == "epoch_ms":
            return datetime.fromtimestamp(float(text) / 1000, UTC), None, "second"
        if how == "date":
            return None, date.fromisoformat(text[:10]), "day"
        if how == "gdelt":  # YYYYMMDDTHHMMSSZ or YYYYMMDDTHHMMZ; without the Z: missing
            if not text.endswith("Z"):
                return None, None, "none"
            fmt = "%Y%m%dT%H%M%SZ" if len(text) == 16 else "%Y%m%dT%H%MZ"
            return datetime.strptime(text, fmt).replace(tzinfo=UTC), None, "second" if len(text) == 16 else "minute"
        if how == "naive_utc":
            t = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return (t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)), None, "second"
        if how == "ny_local":
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=NY).astimezone(UTC), None, "second"
        if how in ("iso_aware", "iso_or_rfc"):
            try:
                t = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                if how == "iso_aware":
                    raise
                t = parsedate_to_datetime(text)
                if t is not None and "-0000" in text:
                    return None, None, "none"
            if t.tzinfo is None or t.utcoffset() is None:
                return None, None, "none"
            return t.astimezone(UTC), None, "second"
        if how == "rfc2822_fixed_est":
            t = parsedate_to_datetime(text)
            if t is None or t.tzinfo is None or "-0000" in text:
                return None, None, "none"
            wall = (t.astimezone(UTC) - timedelta(hours=5)).replace(tzinfo=None)  # the writer's New York clock
            return wall.replace(tzinfo=NY).astimezone(UTC), None, "second"
        if how == "rfc2822":
            t = parsedate_to_datetime(text)
            if t is None or t.tzinfo is None or "-0000" in text:
                return None, None, "none"
            return t.astimezone(UTC), None, "second"
    except (TypeError, ValueError, OverflowError, OSError):
        return None, None, "none"
    raise KeyError(f"unknown reading {how!r}")


def external(raw: Any, *, source: str, field: str, publisher: str | None = None) -> TimeValue:
    """A raw time from ``source``'s ``field`` as a TimeValue (see FIELDS); unknown (source, field) raises."""
    how = FIELDS[(source, field)]
    utc, day, precision = _parse(raw, how)
    flags = []
    if (source, publisher) in KNOWN_SKEW:
        flags.append("known_skew")
    if (source, field) in UNRELIABLE:
        flags.append("unreliable")
    return TimeValue(utc=utc, precision=precision, source=source, field=field, day=day, flags=tuple(flags))


def external_stamp(raw: Any, *, source: str, field: str, publisher: str | None = None) -> str | None:
    return external(raw, source=source, field=field, publisher=publisher).stamp


def external_date(raw: Any, *, source: str, field: str) -> date | None:
    """The calendar date of a raw value: the date itself for date fields; else the date of the value as written
    (its own zone), which is what a provider's "published date" means when only the day is compared."""
    how = FIELDS[(source, field)]
    if how == "date":
        return _parse(raw, how)[1]
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return parsedate_to_datetime(text).date()
        except (TypeError, ValueError, OverflowError):
            return None


def now_utc(now: datetime | None = None) -> datetime:
    """``now``, aware and in UTC; the current time when None. A ``now`` passed without a zone (tests and callers that
    build one by hand) is read as UTC: it is our own clock, not a provider's time."""
    value = datetime.now(UTC) if now is None else now
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def stored_legacy(value: str | None) -> datetime | None:
    """A value this package itself wrote without a zone before 2026-10 (the structured-fact cache), read as UTC.
    Never for a provider's or a page's time."""
    if value is None:
        return None
    t = datetime.fromisoformat(value)
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


_DATELINE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%m-%d-%Y", "%m/%d/%Y")


def dateline(text: str) -> date | None:
    """A calendar date written in an article's dateline ("October 22, 2026", "2026-10-22", "10/22/2026")."""
    for fmt in _DATELINE_FORMATS:
        try:
            return datetime.strptime(text.strip().rstrip(","), fmt).date()
        except ValueError:
            continue
    return None


def clock(text: str) -> str | None:
    """A 12-hour clock ("08:30 AM") as "HH:MM"; None if it is not one."""
    try:
        return datetime.strptime(text.strip(), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return None
