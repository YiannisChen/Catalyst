"""Classify quote evidence roles and publisher families."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from catalyst_agents.attribution import text as _text
from catalyst_agents.attribution.quote_common import (
    _PRIMARY_SOURCE_CLASSES,
    _issuer_text_matches,
)
from catalyst_agents.attribution.quote_text_rules import (
    _CIRCULAR_PRICE_REPORT,
    _CONCRETE_EVENT,
    _MOVE_PCT,
    _STOCK_MOVE,
    _SUBJECT_TICKER,
    _has_strong_event_link,
    _hedged_link_without_strong_link,
    _move_date_matches_session,
)

normalize_text = _text.normalize_text

@dataclass(frozen=True)
class EvidenceRoles:
    roles: tuple[str, ...]
    reasons: dict[str, tuple[str, ...]]
    event_time: str | None = None


def publisher_family(item: Any) -> str:
    """Return a small, stable family label for syndication-aware counting."""
    source_class = str(getattr(item, "source_class", "") or "")
    source_class = str(
        getattr(getattr(item, "source_class", None), "value", source_class)
    ).lower()
    if source_class in _PRIMARY_SOURCE_CLASSES:
        return "primary:" + source_class
    publisher_value = getattr(item, "publisher", None) or ""
    byline_value = (
        getattr(item, "byline", None)
        or getattr(item, "provider", None)
        or getattr(item, "source_name", None)
    )
    if (
        not publisher_value or "yahoo finance" in str(publisher_value).lower()
    ) and byline_value:
        publisher_value = byline_value
    publisher = " ".join(str(publisher_value).lower().split())
    url = str(getattr(item, "canonical_url", None) or "").lower()
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    domain_families = {
        "fool.com": "motley_fool",
        "247wallst.com": "247_wall_street",
        "reuters.com": "reuters",
        "benzinga.com": "benzinga",
        "apnews.com": "associated_press",
        "bloomberg.com": "bloomberg",
        "cnbc.com": "cnbc",
    }
    if host in domain_families and not (
        host == "fool.com" and publisher in {"yahoo finance", "yahoo"}
    ):
        return domain_families[host]
    if (
        ("yahoo finance" in publisher or host == "finance.yahoo.com")
        and host
        and host != "finance.yahoo.com"
    ):
        return {
            "benzinga.com": "benzinga",
            "fool.com": "motley_fool",
            "reuters.com": "reuters",
            "apnews.com": "associated_press",
        }.get(host, host)
    normalized_publisher = re.sub(r"\bthe\s+", "", publisher)
    families = {
        "yahoo finance": "yahoo_finance",
        "benzinga": "benzinga",
        "motley fool": "motley_fool",
        "the motley fool": "motley_fool",
        "fool.com": "motley_fool",
        "24/7 wall st.": "247_wall_street",
        "24/7 wall st": "247_wall_street",
        "reuters": "reuters",
        "associated press": "associated_press",
        "ap news": "associated_press",
        "bloomberg": "bloomberg",
        "cnbc": "cnbc",
        "proactive investors": "proactive_investors",
    }
    for name, family in families.items():
        if host == "finance.yahoo.com" and family == "yahoo_finance":
            continue
        if (
            name in publisher
            or name in normalized_publisher
            or name.replace(" ", "") in url.replace(" ", "")
        ):
            return family
    if "finance.yahoo.com" in url:
        # Yahoo-hosted copies are grouped with their recorded publisher when
        # known; without an original byline they cannot establish independence.
        return "unknown"
    return host or publisher or "unknown"


def _has_same_document_attribution(
    item: Any,
    session_date: str,
    ticker: str,
) -> bool:
    excerpt = str(getattr(item, "excerpt_text", "") or "")
    if not excerpt:
        return False
    return (
        "ATTRIBUTION"
        in classify_evidence_roles(
            excerpt,
            item,
            ticker=ticker,
            session_date=session_date,
            move_date_is_session=None,
            r4b_path=None,
        ).roles
    )


def classify_evidence_roles(
    text: str,
    item: Any,
    *,
    ticker: str,
    session_date: str,
    move_date_is_session: bool | None,
    r4b_path: str | None,
    quote_metadata: dict[str, Any] | None = None,
) -> EvidenceRoles:
    """Classify a kept event-time quote using only code-visible evidence."""
    reasons: dict[str, tuple[str, ...]] = {}
    sentence = normalize_text(text)
    issuer_mentioned = _issuer_text_matches(sentence, ticker)
    names_this_company = issuer_mentioned
    move_match = _STOCK_MOVE.search(sentence)
    has_stock_move = move_match is not None
    if move_match is not None:
        subject_mentions = [
            m for m in _SUBJECT_TICKER.finditer(sentence[: move_match.end()])
        ]
        moving_ticker = next(
            (m.group(1) or m.group(2) for m in reversed(subject_mentions)), None
        )
        if moving_ticker and moving_ticker.upper() != ticker.upper():
            names_this_company = False
        elif moving_ticker:
            names_this_company = True
    elif not names_this_company:
        # Keep an EVENT attached to the issuer when its sentence names the issuer
        # even when the sentence does not describe a stock move.
        names_this_company = _issuer_text_matches(sentence, ticker)
    has_move_percent = bool(_MOVE_PCT.search(sentence))
    circular = _CIRCULAR_PRICE_REPORT.sub(" ", sentence)
    strong_event_link = _has_strong_event_link(circular, ticker)
    hedged = _hedged_link_without_strong_link(sentence)
    on_session = (
        _move_date_matches_session(sentence, item, session_date)
        if move_date_is_session is None
        else bool(move_date_is_session)
    )
    attribution = (
        names_this_company
        and has_stock_move
        and on_session
        and strong_event_link
        and not hedged
    )
    reasons["ATTRIBUTION"] = tuple(
        (
            "ATTRIBUTION_SUBJECT_MATCH"
            if names_this_company
            else "ATTRIBUTION_SUBJECT_MISSING",
            "ATTRIBUTION_MOVE_LANGUAGE"
            if has_stock_move
            else "ATTRIBUTION_MOVE_LANGUAGE_MISSING",
            "ATTRIBUTION_MOVE_PERCENT"
            if has_move_percent
            else "ATTRIBUTION_MOVE_PERCENT_ABSENT",
            "ATTRIBUTION_SESSION_D_MATCH"
            if on_session
            else "ATTRIBUTION_SESSION_D_MISMATCH",
            "ATTRIBUTION_STRONG_EVENT_LINK"
            if strong_event_link
            else "ATTRIBUTION_STRONG_EVENT_LINK_MISSING",
            "ATTRIBUTION_HEDGED" if hedged else "ATTRIBUTION_NOT_HEDGED",
        )
    )

    concrete_event = bool(_CONCRETE_EVENT.search(circular))
    # An event sentence may omit the issuer when its surrounding source excerpt
    # supplies the subject. Keep this separate from the stock-move subject check
    # above, which can correctly identify a different moving ticker.
    excerpt = str(getattr(item, "excerpt_text", "") or "")
    event_subject_match = issuer_mentioned or (
        bool(excerpt)
        and normalize_text(excerpt) != sentence
        and _has_same_document_attribution(item, session_date, ticker)
    )
    verified_time = r4b_path in {
        "pre_deadline_publication",
        "pre_deadline_publication_gap_implied",
        "primary_source_time",
        "extracted_event_time",
        "issuer_earnings_release",
    }
    reasons["EVENT"] = (
        "EVENT_CONCRETE" if concrete_event else "EVENT_CONCRETE_MISSING",
        "EVENT_SUBJECT_MATCH" if names_this_company else "EVENT_SUBJECT_MISMATCH",
        "EVENT_TIME_VERIFIED:" + str(r4b_path)
        if verified_time
        else "EVENT_TIME_UNVERIFIED",
    )
    roles = []
    if attribution:
        roles.append("ATTRIBUTION")
    if event_subject_match and concrete_event and verified_time:
        roles.append("EVENT")
    if not roles:
        roles.append("LEAD")
        reasons["LEAD"] = ("NO_VERIFIED_EVENT_ROLE",)

    metadata = quote_metadata or {}
    event_time = metadata.get("event_date")
    if not event_time and r4b_path == "primary_source_time":
        source_time = getattr(item, "source_time_at", None)
        event_time = source_time.isoformat() if source_time is not None else None
    return EvidenceRoles(tuple(roles), reasons, str(event_time) if event_time else None)
