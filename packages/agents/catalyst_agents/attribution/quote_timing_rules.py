"""Event-time and publication-window quote rules."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from catalyst_agents.attribution.evidence_roles import (
    _has_same_document_attribution,
    classify_evidence_roles,
)
from catalyst_agents.attribution.quote_common import (
    _DAY_WORDS,
    _ET,
    _PRIMARY_SOURCE_CLASSES,
)
from catalyst_agents.attribution.text import _match_key

_EVENT_RELATIONS = {"BEFORE_OPEN", "INTRADAY", "PRIOR_DAY", "AFTER_CLOSE", "UNKNOWN"}


def _move_deadline(temporal_identity: Any, move_profile: Any | None) -> datetime:
    from catalyst_agents.attribution.policy import (
        GAP_CLOSE_RETURN_SHARE,
        GAP_MIN_ABS_PCT,
    )

    if move_profile is not None:
        gap = getattr(move_profile, "gap_return", None)
        close_return = getattr(move_profile, "target_return", None)
        if (
            gap is not None
            and close_return is not None
            and (gap > 0) == (close_return > 0)
            and abs(gap) >= GAP_MIN_ABS_PCT
            and abs(gap) >= GAP_CLOSE_RETURN_SHARE * abs(close_return)
        ):
            return temporal_identity.session_open_at
    return temporal_identity.cutoff_at


def _is_gap_dominated(temporal: Any, move_profile: Any | None) -> bool:
    return _move_deadline(temporal, move_profile) == temporal.session_open_at


def _explicit_post_open_anchor(
    event_quote: str, event_day: date, temporal: Any
) -> bool:
    text = event_quote.lower()
    if re.search(
        r"\b(?:midday|this afternoon|later in the session|intraday|during trading|"
        r"after (?:the )?open|after the bell|after the close|after the bell|after hours|"
        r"extended trading)\b",
        text,
    ):
        return True
    clock = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b|\b(\d{1,2}):(\d{2})\b",
        event_quote,
        re.I,
    )
    if not clock:
        return False
    if clock[1]:
        hour, minute = int(clock[1]), int(clock[2] or 0)
        suffix = clock[3].lower().replace(".", "")
        if suffix == "pm" and hour != 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
    else:
        hour, minute = int(clock[4]), int(clock[5])
    zone = re.search(r"\b(ET|EST|EDT|UTC|GMT|PT|PST|PDT)\b", event_quote, re.I)
    zone_name = zone[1].upper() if zone else "ET"
    if zone_name in {"ET", "EST", "EDT"}:
        zoneinfo = (
            _ET
            if zone_name == "ET"
            else timezone(timedelta(hours=-5 if zone_name == "EST" else -4))
        )
    elif zone_name in {"PT", "PST", "PDT"}:
        zoneinfo = (
            ZoneInfo("America/Los_Angeles")
            if zone_name == "PT"
            else timezone(timedelta(hours=-8 if zone_name == "PST" else -7))
        )
    else:
        zoneinfo = timezone.utc
    if (
        zone_name in {"EST", "EDT"}
        and datetime.combine(event_day, datetime.min.time(), tzinfo=_ET).tzname()
        != zone_name
    ):
        return True
    if (
        zone_name in {"PST", "PDT"}
        and datetime.combine(
            event_day, datetime.min.time(), tzinfo=ZoneInfo("America/Los_Angeles")
        ).tzname()
        != zone_name
    ):
        return True
    clock_at = datetime.combine(
        event_day,
        datetime.min.time().replace(hour=hour, minute=minute),
        tzinfo=zoneinfo,
    ).astimezone(_ET)
    return clock_at >= temporal.session_open_at.astimezone(_ET)


def _event_time_path(
    item: Any,
    temporal: Any,
    quote_text: str,
    metadata: dict[str, Any],
    move_profile: Any | None,
) -> str | None:
    eligible = item.eligible_at
    deadline = _move_deadline(temporal, move_profile)
    gap_day = _is_gap_dominated(temporal, move_profile)
    owned_time = getattr(item, "source_time_at", None)
    source_class = str(getattr(item.source_class, "value", item.source_class))
    official = {
        "sec",
        "issuer_disclosure",
        "corporate_press_release",
        "official_government",
    }
    excerpt = _match_key(item.excerpt_text or "")
    event_quote = str(metadata.get("event_time_quote") or "")
    if _is_future_preview(quote_text, event_quote, eligible):
        return None
    if source_class in official and owned_time is not None and owned_time <= deadline:
        return "primary_source_time"
    # Early availability proves the publication path only when the analyst
    # has not supplied contradictory event-time claims.
    if eligible <= deadline and not any(
        metadata.get(key)
        for key in ("event_time_quote", "event_relation", "event_date")
    ):
        return "pre_deadline_publication"
    if not event_quote or excerpt.find(_match_key(event_quote)) < 0:
        return _attribution_implied_path(
            item, temporal, quote_text, metadata, move_profile
        )
    parsed = _parse_event_anchor(event_quote, eligible, temporal)
    relation = str(metadata.get("event_relation") or "UNKNOWN").upper()
    if parsed is None or relation not in _EVENT_RELATIONS:
        return _attribution_implied_path(
            item, temporal, quote_text, metadata, move_profile
        )
    try:
        claimed_day = date.fromisoformat(str(metadata.get("event_date")))
    except (TypeError, ValueError):
        return _attribution_implied_path(
            item, temporal, quote_text, metadata, move_profile
        )
    session_day = date.fromisoformat(temporal.session_date)
    event_day, derived_relation = parsed
    if event_day != claimed_day or event_day > session_day:
        return _attribution_implied_path(
            item, temporal, quote_text, metadata, move_profile
        )
    if relation == "UNKNOWN" and not (gap_day and event_day == session_day):
        return _attribution_implied_path(
            item, temporal, quote_text, metadata, move_profile
        )
    if relation == "PRIOR_DAY":
        return (
            _attribution_implied_path(
                item, temporal, quote_text, metadata, move_profile
            )
            if event_day >= session_day
            else "extracted_event_time"
        )
    if event_day < session_day:
        return _attribution_implied_path(
            item, temporal, quote_text, metadata, move_profile
        )
    if gap_day and event_day == session_day:
        if _explicit_post_open_anchor(event_quote, event_day, temporal):
            return None
        if derived_relation == "BEFORE_OPEN":
            return "extracted_event_time" if relation == "BEFORE_OPEN" else None
        has_attribution = "ATTRIBUTION" in classify_evidence_roles(
            quote_text,
            item,
            ticker=str(getattr(move_profile, "ticker", "") or ""),
            session_date=temporal.session_date,
            move_date_is_session=None,
            r4b_path=None,
        ).roles or _has_same_document_attribution(
            item,
            temporal.session_date,
            str(getattr(move_profile, "ticker", "") or ""),
        )
        if relation == "AFTER_CLOSE":
            return "extracted_event_time_date_only" if has_attribution else None
        published_at = eligible.astimezone(_ET)
        close_at = temporal.session_close_at.astimezone(_ET)
        if (
            derived_relation in {"INTRADAY", "UNKNOWN"}
            and relation in {"INTRADAY", "UNKNOWN"}
            and published_at.date() == session_day
            and published_at <= close_at
            and has_attribution
        ):
            return "pre_deadline_publication_gap_implied"
        if (
            derived_relation in {"INTRADAY", "UNKNOWN"}
            and relation in {"INTRADAY", "UNKNOWN"}
            and published_at.date() == session_day
            and published_at > close_at
            and has_attribution
        ):
            return "extracted_event_time_date_only"
        return _attribution_implied_path(
            item, temporal, quote_text, metadata, move_profile
        )
    if derived_relation != relation or relation == "AFTER_CLOSE":
        return _attribution_implied_path(
            item, temporal, quote_text, metadata, move_profile
        )
    if relation == "INTRADAY" and deadline == temporal.session_open_at:
        return _attribution_implied_path(
            item, temporal, quote_text, metadata, move_profile
        )
    if eligible <= deadline:
        return "pre_deadline_publication"
    if source_class in official and owned_time is not None and owned_time <= deadline:
        return "primary_source_time"
    return "extracted_event_time"


def _event_time_unverified(
    item: Any,
    temporal: Any,
    quote_text: str,
    metadata: dict[str, Any],
    move_profile: Any | None,
) -> bool:
    return _event_time_path(item, temporal, quote_text, metadata, move_profile) is None


def _event_time_v2_contradiction(
    item: Any,
    temporal: Any,
    quote_text: str,
    metadata: dict[str, Any],
    move_profile: Any | None,
) -> bool:
    """Reject only explicit, source-anchored evidence that the event was too late."""
    event_quote = str(metadata.get("event_time_quote") or quote_text)
    excerpt = _match_key(item.excerpt_text or "")
    anchored = bool(event_quote) and excerpt.find(_match_key(event_quote)) >= 0
    if _is_future_preview(
        quote_text, event_quote if anchored else "", item.eligible_at
    ):
        return True
    if not anchored:
        return False
    parsed = _parse_event_anchor(event_quote, item.eligible_at, temporal)
    if parsed is None:
        return False
    event_day, relation = parsed
    session_day = date.fromisoformat(temporal.session_date)
    if event_day > session_day or (
        event_day == session_day and relation == "AFTER_CLOSE"
    ):
        return True
    if event_day != session_day:
        return False
    deadline = _move_deadline(temporal, move_profile).astimezone(_ET)
    if _is_gap_dominated(temporal, move_profile) and _explicit_post_open_anchor(
        event_quote, event_day, temporal
    ):
        return True
    clock = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b|\b(\d{1,2}):(\d{2})\b",
        event_quote,
        re.I,
    )
    if not clock:
        return False
    if clock[1]:
        hour, minute = int(clock[1]), int(clock[2] or 0)
        suffix = clock[3].lower().replace(".", "")
        if suffix == "pm" and hour != 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
    else:
        hour, minute = int(clock[4]), int(clock[5])
    zone = re.search(r"\b(ET|EST|EDT|UTC|GMT|PT|PST|PDT)\b", event_quote, re.I)
    zone_name = zone[1].upper() if zone else "ET"
    if zone_name in {"ET", "EST", "EDT"}:
        zoneinfo = (
            _ET
            if zone_name == "ET"
            else timezone(timedelta(hours=-5 if zone_name == "EST" else -4))
        )
    elif zone_name in {"PT", "PST", "PDT"}:
        zoneinfo = (
            ZoneInfo("America/Los_Angeles")
            if zone_name == "PT"
            else timezone(timedelta(hours=-8 if zone_name == "PST" else -7))
        )
    else:
        zoneinfo = timezone.utc
    clock_at = datetime.combine(
        event_day,
        datetime.min.time().replace(hour=hour, minute=minute),
        tzinfo=zoneinfo,
    ).astimezone(_ET)
    return clock_at > deadline


def _event_time_v2_path(
    item: Any,
    temporal: Any,
    quote_text: str,
    metadata: dict[str, Any],
    move_profile: Any | None,
) -> str:
    """Return positive timing evidence when present; otherwise preserve uncertainty."""
    if _event_time_v2_contradiction(item, temporal, quote_text, metadata, move_profile):
        return "contradiction"
    deadline = _move_deadline(temporal, move_profile)
    owned_time = getattr(item, "source_time_at", None)
    source_class = str(
        getattr(
            getattr(item, "source_class", None),
            "value",
            getattr(item, "source_class", ""),
        )
    )
    if (
        source_class in _PRIMARY_SOURCE_CLASSES
        and owned_time is not None
        and owned_time <= deadline
    ):
        return "primary_source_time"
    if item.eligible_at <= deadline and not any(
        metadata.get(key)
        for key in ("event_time_quote", "event_relation", "event_date")
    ):
        return "pre_deadline_publication"
    event_quote = str(metadata.get("event_time_quote") or quote_text)
    if not event_quote or _match_key(event_quote) not in _match_key(
        item.excerpt_text or ""
    ):
        return "unverified"
    parsed = _parse_event_anchor(event_quote, item.eligible_at, temporal)
    if parsed is None:
        return "unverified"
    event_day, relation = parsed
    try:
        claimed_day = date.fromisoformat(str(metadata.get("event_date")))
    except (TypeError, ValueError):
        return "unverified"
    if (
        claimed_day != event_day
        or str(metadata.get("event_relation") or "UNKNOWN").upper() != relation
    ):
        return "unverified"
    if relation not in {"BEFORE_OPEN", "INTRADAY", "PRIOR_DAY"}:
        return "unverified"
    if event_day == date.fromisoformat(temporal.session_date):
        if _is_gap_dominated(temporal, move_profile) and relation != "BEFORE_OPEN":
            return "unverified"  # a gap deadline requires an explicit pre-open time
        # A clock-derived event must precede the ordinary close.
        if re.search(
            r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b|\b\d{1,2}:\d{2}\b",
            event_quote,
            re.I,
        ):
            return "extracted_event_time"
        if relation in {"BEFORE_OPEN", "INTRADAY"}:
            return "extracted_event_time"
    if event_day < date.fromisoformat(temporal.session_date):
        return "extracted_event_time"
    return "unverified"


def _publication_window(temporal: Any) -> tuple[datetime, datetime]:
    method = getattr(temporal, "publication_window", None)
    if callable(method):
        return method()
    return (
        getattr(temporal, "evidence_window_start_at", None)
        or temporal.information_window_start_at,
        getattr(temporal, "evidence_window_end_at", None) or temporal.cutoff_at,
    )


def _quote_timing_window(
    item: Any, temporal: Any, policy: str, release_timing: Mapping[str, Any] | None
) -> tuple[datetime, datetime, datetime, Mapping[str, Any] | None]:
    from catalyst_agents.runtime.earnings_timing import qualifies_as_issuer_earnings

    timing = release_timing or getattr(item, "release_timing", None)
    qualified = timing if qualifies_as_issuer_earnings(timing) else None
    if (
        qualified is not None
        and qualified.get("attributed_session_date") != temporal.session_date
    ):
        qualified = None
    check_time = item.eligible_at
    if qualified is not None:
        try:
            check_time = datetime.fromisoformat(
                str(qualified["corrected_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            qualified = None
            check_time = item.eligible_at
        if check_time.tzinfo is None or check_time.utcoffset() is None:
            qualified = None
            check_time = item.eligible_at
    if policy in ("event_time_v1", "event_time_v2", "event_time_p1"):
        start, end = _publication_window(temporal)
    else:
        start, end = temporal.information_window_start_at, temporal.cutoff_at
    if qualified is not None:
        start, end = temporal.information_window_start_at, temporal.cutoff_at
    return start, end, check_time, qualified


def _is_future_preview(quote: str, event_quote: str, published_at: datetime) -> bool:
    text = f"{quote} {event_quote}"
    return bool(
        re.search(
            r"\b(?:will|plans? to|is expected to|are expected to|is set to|scheduled to)\b",
            text,
            re.I,
        )
        or re.search(r"\b(?:tomorrow|later today)\b", text, re.I)
        or (
            re.search(r"\bafter (?:the )?(?:close|bell)\b", text, re.I)
            and re.search(r"\btoday\b", text, re.I)
        )
    )


def _parse_event_anchor(
    event_quote: str, published_at: datetime, temporal: Any
) -> tuple[date, str] | None:
    """Derive an unambiguous event date and session relation in New York time."""
    from datetime import time

    text = event_quote.lower()
    pub_day = published_at.astimezone(_ET).date()
    dates: list[date] = []
    for match in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text):
        try:
            dates.append(date(int(match[1]), int(match[2]), int(match[3])))
        except ValueError:
            return None
    months = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    for match in re.finditer(
        r"\b(" + "|".join(months) + r")\s+(\d{1,2})(?:,?\s+(20\d{2}))?\b", text
    ):
        try:
            year = int(match[3]) if match[3] else pub_day.year
            month_day = date(year, months.index(match[1]) + 1, int(match[2]))
            if match[3] is None and month_day > pub_day:
                month_day = date(year - 1, month_day.month, month_day.day)
            dates.append(month_day)
        except ValueError:
            return None
    for word, offset in (("yesterday", -1), ("today", 0), ("tomorrow", 1)):
        if re.search(r"\b" + word + r"\b", text):
            dates.append(pub_day + timedelta(days=offset))
    for match in re.finditer(
        r"\b(?:(?:on|last)\s+)?(monday|tuesday|wednesday|thursday|friday)\b", text
    ):
        dates.append(
            pub_day
            - timedelta(days=(pub_day.weekday() - _DAY_WORDS.index(match[1])) % 7)
        )
    has_time = bool(
        re.search(
            r"\b(?:\d{1,2}:\d{2}|\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)|pre.?market|before the bell|before the open|this morning|after the close|after the bell|after hours|extended trading|during trading|intraday)\b",
            text,
        )
    )
    if not dates and not has_time:
        return None
    if not dates:
        dates = [date.fromisoformat(temporal.session_date)]
    if len(set(dates)) != 1:
        return None
    event_day = dates[0]
    session_day = date.fromisoformat(temporal.session_date)
    if event_day < session_day:
        return event_day, "PRIOR_DAY"
    if event_day > session_day:
        return event_day, "AFTER_CLOSE"
    if re.search(
        r"\b(?:after the close|after the bell|after hours|extended trading)\b", text
    ):
        return event_day, "AFTER_CLOSE"
    if re.search(r"\b(?:pre.?market|before the bell|before the open)\b", text):
        return event_day, "BEFORE_OPEN"
    if re.search(r"\bthis morning\b", text):
        return event_day, "INTRADAY"
    if re.search(r"\b(?:during trading|intraday)\b", text):
        return event_day, "INTRADAY"
    if re.search(
        r"\b(?:yesterday|today|tomorrow|monday|tuesday|wednesday|thursday|friday)\b",
        text,
    ):
        return event_day, "INTRADAY"
    clock = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b|\b(\d{1,2}):(\d{2})\b",
        text,
    )
    if clock:
        if clock[1]:
            hour, minute = int(clock[1]), int(clock[2] or 0)
            if clock[3].startswith("p") and hour != 12:
                hour += 12
            if clock[3].startswith("a") and hour == 12:
                hour = 0
        else:
            hour, minute = int(clock[4]), int(clock[5])
        zone = re.search(r"\b(ET|EST|EDT|UTC|GMT|PT|PST|PDT)\b", event_quote, re.I)
        zone_name = zone[1].upper() if zone else "ET"
        if zone_name in {"ET", "EST", "EDT"}:
            zoneinfo = (
                _ET
                if zone_name == "ET"
                else timezone(timedelta(hours=-5 if zone_name == "EST" else -4))
            )
        elif zone_name in {"PT", "PST", "PDT"}:
            zoneinfo = (
                ZoneInfo("America/Los_Angeles")
                if zone_name == "PT"
                else timezone(timedelta(hours=-8 if zone_name == "PST" else -7))
            )
        else:
            zoneinfo = timezone.utc
        if (
            zone_name in {"EST", "EDT"}
            and datetime.combine(event_day, time(12), tzinfo=_ET).tzname() != zone_name
        ):
            return None
        if (
            zone_name in {"PST", "PDT"}
            and datetime.combine(
                event_day, time(12), tzinfo=ZoneInfo("America/Los_Angeles")
            ).tzname()
            != zone_name
        ):
            return None
        clock_at = datetime.combine(
            event_day, time(hour, minute), tzinfo=zoneinfo
        ).astimezone(_ET)
        if clock_at < temporal.session_open_at.astimezone(_ET):
            return event_day, "BEFORE_OPEN"
        if clock_at > temporal.session_close_at.astimezone(_ET):
            return event_day, "AFTER_CLOSE"
        return event_day, "INTRADAY"
    return event_day, "UNKNOWN"


def _relative_event_date(text: str, published_at: datetime) -> date | None:
    value = text.lower()
    published_day = published_at.astimezone(_ET).date()
    if re.search(r"\byesterday\b", value):
        return published_day - timedelta(days=1)
    if re.search(r"\bthis morning\b", value):
        return published_day
    named = re.search(
        r"\b(?:(?:on|last)\s+)?(monday|tuesday|wednesday|thursday|friday)\b", value
    )
    if named:
        wanted = _DAY_WORDS.index(named.group(1))
        delta = (published_day.weekday() - wanted) % 7
        return published_day - timedelta(days=delta)
    return None


def _attribution_implied_path(
    item: Any,
    temporal: Any,
    quote_text: str,
    metadata: dict[str, Any],
    move_profile: Any | None,
) -> str | None:
    """A session-D causal attribution attests the event preceded that move."""
    relation = str(metadata.get("event_relation") or "UNKNOWN").upper()
    if relation == "AFTER_CLOSE":
        return None
    gap_day = _is_gap_dominated(temporal, move_profile)
    if gap_day and relation == "INTRADAY":
        return None
    event_quote = str(metadata.get("event_time_quote") or "")
    if event_quote:
        if re.search(
            r"\b(?:after the close|after the bell|after hours|extended trading)\b",
            event_quote,
            re.I,
        ):
            return None
        parsed_event = _parse_event_anchor(event_quote, item.eligible_at, temporal)
        if parsed_event is not None and parsed_event[0] > date.fromisoformat(
            temporal.session_date
        ):
            return None
        if gap_day and _explicit_post_open_anchor(
            event_quote, date.fromisoformat(temporal.session_date), temporal
        ):
            return None
    event_date = metadata.get("event_date")
    if event_date:
        try:
            if date.fromisoformat(str(event_date)) > date.fromisoformat(
                temporal.session_date
            ):
                return None
        except ValueError:
            return None
    observation = getattr(move_profile, "ticker", None)
    if not observation:
        observation = getattr(getattr(temporal, "observation", None), "ticker", None)
    ticker = str(observation or "")
    if not ticker:
        return None
    roles = classify_evidence_roles(
        quote_text,
        item,
        ticker=ticker,
        session_date=temporal.session_date,
        move_date_is_session=None,
        r4b_path=None,
        quote_metadata=metadata,
    ).roles
    return "attribution_implied" if "ATTRIBUTION" in roles else None
