"""Deterministic text rules used by quote and evidence attribution checks."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from catalyst_data.retrieval.web_bodies import _MOVE_VERB as _WEB_MOVE_VERB

from catalyst_agents.attribution.quote_common import (
    _DAY_WORDS,
    _ET,
    _issuer_text_matches,
)

_SENTENCE_END = re.compile(r"[.!?;][\"'”’)\]]*$")


_HEDGE_LINKS = (
    r"\bamid\b",
    r"\bas (?:concerns|worries|fears|optimism|sentiment)\b[^.;]{0,50}\b(?:mounted|grew|rose|increased)\b",
    r"(?<![.!?]\s)\b(?:may|might|could)\b",
    r"\blikely\b",
    r"\bpossibly\b",
    r"\bappears to\b",
    r"\bseemingly\b",
    r"\bon (?:easing )?(?:concerns|worries|fears|optimism|sentiment)\b",
)


_STRONG_LINK = re.compile(
    r"\b(?:after|following|because of|because|due to|on news that|on reports that|"
    r"as a result of|in response to|in reaction to|on the back of|driven by|sparked by|triggered by)\b"
    r"([^.;]{2,180})",
    re.IGNORECASE,
)


_ON_EVENT_LINK = re.compile(
    r"\bon\s+(?:the\s+|a\s+)?(?:news|announcement|report|results?|upgrade|downgrade|earnings)\b"
    r"([^.;]{0,160})",
    re.IGNORECASE,
)


_CONCRETE_EVENT = re.compile(
    r"\b(?:cut|raised?|lowered|downgraded|upgraded|guided|reported|announced|filed|"
    r"approved|sued|agreed|acquir(?:e|ed)|beat|missed|won|lost|launch(?:ed)?|recall(?:ed)?|"
    r"outlook|earnings|deal|merger|investigation|lawsuit|contract|approval|decision|"
    r"announcement|upgrade|downgrade|acquisition|results|delivered|quarter)\b",
    re.I,
)


_STOCK_MOVE = re.compile(
    r"\b(?:shares?|stock|it)\b(?:\s+[\w():.-]+){0,12}\s+"
    r"(?:are\s+|were\s+|was\s+|has\s+|have\s+)?"
    r"(?:rose|rises|rising|fell|falls|falling|jumped|jumps|slid|slides|sliding|surged|surges|"
    r"tumbled|tumbles|gained|gains|dropped|drops|rallied|rallies|sank|climbed|"
    r"declined|plunged|soared|advanced|moved(?:\s+\w+){0,2}\s+(?:higher|lower)|"
    r"traded higher|traded lower|up|down|higher|lower)\b",
    re.IGNORECASE,
)


_MOVE_PCT = re.compile(r"\b\d+(?:\.\d+)?\s*%")


_TICKER_MOVE_MARKER = re.compile(r"\b([A-Z]{1,5})\s+[+-]?\d+(?:\.\d+)?\s*%")


_MOVE_VERB = re.compile(
    r"\b(?:rose|rises|rising|fell|falls|falling|jumped|jumps|slid|slides|sliding|surged|surges|tumbled|tumbles|gained|gains|dropped|drops|rallied|rallies|sank|climbed|declined|plunged|soared|advanced|traded higher|traded lower)\b",
    re.I,
)


_SUBJECT_TICKER = re.compile(
    r"\b([A-Z]{1,5})(?:\s+[+-]?\d+(?:\.\d+)?\s*%)?\s*[),]?\s*(?:\([^)]{0,70}\))?(?:'s)?\s+(?:shares?|stock)\b"
    r"|\bshares?\s+of\s+([A-Z]{1,5})\b"
)


_CIRCULAR_PRICE_REPORT = re.compile(
    r"\b(?:reported|reports?|said|says|noted|notes?)\s+(?:that\s+)?(?:the\s+)?"
    r"(?:stock|shares?)\s+(?:had\s+)?(?:fell|fallen|were down|was down|dropped|declined|"
    r"hit\s+(?:a\s+)?(?:\d+[- ](?:year|week)\s+)?low|traded lower)\b[^.;]*",
    re.I,
)


def _move_date_matches_session(text: str, item: Any, session_date: str) -> bool:
    """Resolve a stock-move date against publication time in New York."""
    published_at = getattr(item, "eligible_at", None)
    if published_at is None:
        return False
    published_day = published_at.astimezone(_ET).date()
    session_day = date.fromisoformat(session_date)
    if published_day < session_day:
        return False
    move = _STOCK_MOVE.search(text)
    clause = text[: move.end()] if move else text
    clause = re.split(
        r"\b(?:after|following|because|due to|as a result of|on the news|on the report)\b",
        clause,
        maxsplit=1,
        flags=re.I,
    )[0]
    explicit = re.search(
        r"\b(20\d{2}-\d{2}-\d{2}|(?:on\s+|last\s+)?(?:monday|tuesday|wednesday|thursday|friday)|"
        r"yesterday|today|this morning|last night|(?:january|february|march|april|may|june|july|"
        r"august|september|october|november|december)\s+\d{1,2}(?:,?\s+20\d{2})?)\b",
        clause,
        re.I,
    )
    if explicit:
        token = explicit.group(1).lower().strip()
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", token):
            resolved = date.fromisoformat(token)
        elif token in {"yesterday", "last night"}:
            resolved = published_day - timedelta(days=1)
        elif token in {"today", "this morning"}:
            resolved = published_day
        elif re.search(r"monday|tuesday|wednesday|thursday|friday", token):
            day_name = re.search(r"monday|tuesday|wednesday|thursday|friday", token)[0]
            resolved = published_day - timedelta(
                days=(published_day.weekday() - _DAY_WORDS.index(day_name)) % 7
            )
        else:
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
            month = re.search(
                r"january|february|march|april|may|june|july|august|september|october|november|december",
                token,
            )[0]
            day = int(re.search(r"\d{1,2}", token)[0])
            year_match = re.search(r"20\d{2}", token)
            resolved = date(
                int(year_match[0]) if year_match else published_day.year,
                months.index(month) + 1,
                day,
            )
            if not year_match and resolved > published_day:
                resolved = date(resolved.year - 1, resolved.month, resolved.day)
        return resolved == session_day
    if re.search(r"\b(?:in the )?(?:morning|afternoon) session\b", text, re.I):
        if published_day == session_day:
            return True
        published_local = published_at.astimezone(_ET)
        one_day_later = published_day == session_day + timedelta(days=1)
        before_open = published_local.hour < 9 or (
            published_local.hour == 9 and published_local.minute < 30
        )
        if one_day_later and session_day.weekday() < 5 and before_open:
            return True
        return False
    # With no move-time anchor, only same New York publication date is admissible.
    return published_day == session_day


def _other_session_relative_to_publication(
    text: str, published_at: datetime, session_date: str
) -> bool:

    move = re.compile(r"\b" + _WEB_MOVE_VERB + r"\b([^.;]{0,100})", re.I)
    mention = re.compile(
        r"\b(?:yesterday|today|this morning|last night|(?:on |last )?(?:monday|tuesday|wednesday|thursday|friday)|(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2})\b",
        re.I,
    )
    for move_match in move.finditer(text):
        clause = re.split(
            r"\b(?:after|following|because|due to|as)\b",
            move_match[1],
            maxsplit=1,
            flags=re.I,
        )[0]
        for day_match in mention.finditer(clause):
            resolved = _resolve_move_date(day_match[0], published_at)
            if resolved is not None and resolved != date.fromisoformat(session_date):
                return True
    return False


def _resolve_move_date(value: str, published_at: datetime) -> date | None:
    pub_day = published_at.astimezone(_ET).date()
    value = value.lower().strip()
    if value in {"yesterday", "last night"}:
        return pub_day - timedelta(days=1)
    if value in {"today", "this morning"}:
        return pub_day
    named = re.search(r"monday|tuesday|wednesday|thursday|friday", value)
    if named:
        return pub_day - timedelta(
            days=(pub_day.weekday() - _DAY_WORDS.index(named[0])) % 7
        )
    explicit = re.search(
        r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})",
        value,
    )
    if explicit:
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
        resolved = date(pub_day.year, months.index(explicit[1]) + 1, int(explicit[2]))
        if resolved > pub_day:
            resolved = date(pub_day.year - 1, resolved.month, resolved.day)
        return resolved
    return None


def _hedged_link_without_strong_link(text: str) -> bool:
    hedged = any(
        re.search(pattern, text, re.I if "may|might|could" not in pattern else 0)
        for pattern in _HEDGE_LINKS
    )
    linked_event = _has_strong_event_link(text, "") or any(
        _CONCRETE_EVENT.search(match[1])
        for match in re.finditer(r"\bamid\b([^.;]{2,100})", text, re.I)
    )
    return hedged and not linked_event


def _has_strong_event_link(text: str, ticker: str) -> bool:
    if any(
        _CONCRETE_EVENT.search(match.group(1)) for match in _STRONG_LINK.finditer(text)
    ):
        return True
    if any(
        _CONCRETE_EVENT.search(match.group(1))
        for match in _ON_EVENT_LINK.finditer(text)
    ):
        return True
    if (
        re.search(r"\bon (?:the )?(?:news|announcement|report|results)\b", text, re.I)
        and _CONCRETE_EVENT.search(text)
        and (not ticker or _issuer_text_matches(text, ticker))
    ):
        return True
    if any(
        _CONCRETE_EVENT.search(match.group(1))
        for match in re.finditer(
            r"\bas investors (?:cheered|digested)\b([^.;]{2,160})", text, re.I
        )
    ):
        return True
    causal_sending = re.finditer(
        r"\b([^.;]{0,120}?)\b(?:sent|drove|pushed|lifted|boosted|dragged|weighed on)\b"
        r"([^.;]{2,120})",
        text,
        re.I,
    )
    for match in causal_sending:
        issuer_tail = match.group(2)
        if (
            _CONCRETE_EVENT.search(match.group(1))
            and re.search(r"\b(?:shares?|stock)\b", issuer_tail, re.I)
            and (not ticker or _issuer_text_matches(issuer_tail, ticker))
        ):
            return True
    return False
