"""Rules 1 and 2: is a cluster's event inside the window after the prior close and up to D's close?"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from catalyst_agents.attribution.extraction import AboutTicker, EventCluster, EventType

ET = ZoneInfo("America/New_York")
EARNINGS_LOOKBACK_DAYS = 30
EARNINGS_LOOKAHEAD_DAYS = 4


def in_window(day: str, time_et: str | None, prior_close: datetime, close: datetime) -> bool:
    """A date-only event on the prior session's day counts as inside; one on D at or after 16:00 ET does not."""
    prior, d = prior_close.astimezone(ET).date(), close.astimezone(ET).date()
    when = date.fromisoformat(day)
    if when == d:
        return not (time_et and time_et >= "16:00")
    if prior < when < d:
        return True
    if when == prior:
        return time_et is None or time_et >= "16:00"
    return False


def text_stale(cluster: EventCluster, dates: dict[str, dict], prior_close: datetime, close: datetime) -> bool:
    """Stale when at least one member has a VERIFIED event date and none of the verified dates is inside."""
    found = []
    for item, _ in cluster.members:
        v = dates.get(str(item.get("evidence_id")))
        if v and v.get("status") == "VERIFIED" and v.get("date"):
            found.append(in_window(v["date"], v.get("time_et"), prior_close, close))
    return bool(found) and not any(found)


def earnings_anchor(cluster: EventCluster, filings_202: Callable[[datetime, datetime], list],
                    prior_close: datetime, close: datetime) -> bool | None:
    """An issuer EARNINGS cluster: False when an Item 2.02 8-K was accepted inside the window; True when one was
    accepted only in the 30 days before it or the 4 days after D's close; None when none is known (text decides)."""
    if cluster.event_type != EventType.EARNINGS or cluster.scope != AboutTicker.ISSUER:
        return None
    if filings_202(prior_close, close):
        return False
    before = filings_202(prior_close - timedelta(days=EARNINGS_LOOKBACK_DAYS), prior_close)
    after = filings_202(close, close + timedelta(days=EARNINGS_LOOKAHEAD_DAYS))
    return True if (before or after) else None


def stale(cluster: EventCluster, *, dates: dict[str, dict], prior_close: datetime, close: datetime,
          filings_202: Callable[[datetime, datetime], list] | None) -> bool:
    if filings_202 is not None:
        anchored = earnings_anchor(cluster, filings_202, prior_close, close)
        if anchored is not None:
            return anchored
    return text_stale(cluster, dates, prior_close, close)


def usable_ids(records: Iterable) -> list[str]:
    from catalyst_agents.attribution.extraction import Session
    return [str(i.get("evidence_id")) for i, e in records
            if e.valid and e.about_ticker != AboutTicker.OTHER and e.session != Session.OTHER_SESSION
            and e.event_type not in (EventType.NONE, None) and not e.is_preview_or_evergreen]
