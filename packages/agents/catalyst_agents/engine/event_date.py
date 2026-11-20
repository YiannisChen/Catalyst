"""event_date_v1: when did the event an item describes happen? One extractor-model call per usable record.

The model returns a date and the excerpt's own words that state it (date_phrase). The date is kept only when the phrase
is verbatim in the excerpt, the date is not after publication, and a checkable phrase (a month and day, "today",
"yesterday", a weekday name) agrees with the date. Only VERIFIED dates are used by the recency rule.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PROMPT_VERSION = "event_date_v1"
ROLE = "event_dater"
MAX_OUTPUT_TOKENS = 200
ET = ZoneInfo("America/New_York")
SYSTEM = """You date events in financial news. You get one article excerpt and the event it was found to describe.
Return when the EVENT itself happened or was announced - not when the article was published. A recap published on
Wednesday of an analyst upgrade made on Monday has event_date = Monday.

Rules:
- Use only the excerpt and the publication time given. Do not use outside knowledge.
- date_phrase: copy, verbatim, the shortest words of the excerpt that state when the event happened, e.g. "on
  Tuesday", "Oct. 14", "earlier today", "this morning", "last week", "on Monday after the bell". If the excerpt
  does not say when the event happened, use "" and event_date null.
- Resolve relative words ("today", "yesterday", "Tuesday", "last week") against the publication time, which is in
  US Eastern time.
- precision: DAY when the day is known, WEEK / MONTH / QUARTER when only that is known, NONE when nothing is known.
- time_et: the event's time of day in US Eastern time (HH:MM, 24h) only when the excerpt states it or says before
  the open (use 09:00), after the close / after the bell (use 16:30); else null.

Return JSON only: {"event_date": "YYYY-MM-DD" or null, "precision": "DAY"|"WEEK"|"MONTH"|"QUARTER"|"NONE",
"time_et": "HH:MM" or null, "date_phrase": "..."}"""
REQUEST_OPTIONS = {"max_tokens": MAX_OUTPUT_TOKENS, "response_format": {"type": "json_object"},
                   "extra_body": {"thinking": {"type": "disabled"}}}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def published_et(item: dict) -> datetime:
    value = item["eligible_at"]
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return value.astimezone(ET)


def messages(ticker: str, company: str | None, item: dict, extraction) -> list[dict]:
    user = {
        "ticker": ticker, "company": company or ticker,
        "event_summary": extraction.event_summary,
        "event_quote": extraction.event_quote or extraction.attribution_quote,
        "article_published_et": published_et(item).strftime("%A %Y-%m-%d %H:%M"),
        "excerpt": str(item.get("excerpt_text") or "")[:2500],
    }
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def check(raw: str, item: dict) -> dict:
    """{date, precision, time_et, phrase, model_date, status}; status VERIFIED | UNCHECKED | NONE | REJECTED:<why>."""
    try:
        out = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except ValueError:
        return {"date": None, "status": "REJECTED:unparseable"}
    phrase = str(out.get("date_phrase") or "").strip()
    precision = str(out.get("precision") or "NONE").upper()
    time_et = out.get("time_et")
    base = {"phrase": phrase, "precision": precision, "time_et": time_et, "model_date": out.get("event_date")}
    try:
        d = date.fromisoformat(str(out["event_date"])) if out.get("event_date") else None
    except ValueError:
        return {**base, "date": None, "status": "REJECTED:bad_date"}
    if d is None or not phrase:
        return {**base, "date": None, "status": "NONE"}
    if _norm(phrase) not in _norm(item.get("excerpt_text")):
        return {**base, "date": None, "status": "REJECTED:phrase_not_in_excerpt"}
    pub = published_et(item).date()
    if d > pub:
        return {**base, "date": None, "status": "REJECTED:after_publication"}
    p = _norm(phrase)
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", p)
    if m:
        month_day_matches = (_MONTHS[m.group(1)], int(m.group(2))) == (d.month, d.day)
        # A month/day phrase without a year resolves to its latest occurrence on
        # or before publication. Otherwise a model can return the same month/day
        # from an older year and have it incorrectly certified as stale.
        explicit_year = re.match(r"\s*,?\s*(\d{4})\b", p[m.end():])
        if month_day_matches and explicit_year is None:
            month, day = _MONTHS[m.group(1)], int(m.group(2))
            expected = None
            for year in range(pub.year, 0, -1):
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue
                if candidate <= pub:
                    expected = candidate
                    break
            ok = d == expected
            reason = "REJECTED:month_day_year"
        else:
            ok = month_day_matches
            reason = "REJECTED:month_day"
        return {**base, "date": d.isoformat() if ok else None, "status": "VERIFIED" if ok else reason}
    if re.search(r"\b(today|this morning|this afternoon|earlier in the day|tonight|on the day)\b", p):
        ok = d == pub
        return {**base, "date": d.isoformat() if ok else None, "status": "VERIFIED" if ok else "REJECTED:today"}
    if re.search(r"\b(yesterday|last night)\b", p):
        ok = d == pub - timedelta(days=1)
        return {**base, "date": d.isoformat() if ok else None, "status": "VERIFIED" if ok else "REJECTED:yesterday"}
    named = {i for i, name in enumerate(_WEEKDAYS) if re.search(rf"\b{name}\b", p)}
    if named:
        ok = d.weekday() in named and 0 <= (pub - d).days <= 7
        return {**base, "date": d.isoformat() if ok else None, "status": "VERIFIED" if ok else "REJECTED:weekday"}
    if precision != "DAY":
        return {**base, "date": None, "status": "NONE"}
    return {**base, "date": d.isoformat(), "status": "UNCHECKED"}
