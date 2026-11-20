"""Quote-only answers (Q1c): the Analyst selects source sentences, code checks them.

Every published cause is a verbatim, complete source sentence from full-text
evidence available inside the session window. The Analyst's own statement and
mechanism prose are never published: a surviving hypothesis's statement is
replaced by its rendered quotes, and a hypothesis with no surviving quote is
dropped before normalization, so ceilings and ABSTAIN follow unchanged.

Rules (docs/plans/2026-09-28-q1c-quote-only-answers.md §3):
R1 the quote's evidence is in the pack and supports the hypothesis;
R2 the quote is one to three complete prose sentences found verbatim
   (after HTML-entity decoding and whitespace collapsing) in the excerpt;
R3 the evidence is FULL_TEXT;
R4 information_window_start_at < eligible_at <= cutoff_at;
R5 at most 4 quotes per hypothesis, 900 characters per quote, 1,800 in all;
   quotes are dropped whole, never truncated;
R6 the quote does not report a price move on another session ("slumped on
   Wednesday" for a Thursday session, "fell yesterday") or over another
   period ("up 17% in January" for a February session, "last month").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Mapping

from catalyst_agents.attribution import text as _text
from catalyst_agents.attribution.evidence_roles import (
    EvidenceRoles,
    classify_evidence_roles,
    publisher_family,
)
from catalyst_agents.attribution.quote_common import (
    _ISSUER_NAME_ALIASES,
    _PRIMARY_SOURCE_CLASSES,
)
from catalyst_agents.attribution.quote_text_rules import (
    _SENTENCE_END,
    _hedged_link_without_strong_link,
    _other_session_relative_to_publication,
)
from catalyst_agents.attribution.quote_timing_rules import (
    _event_time_path,
    _event_time_unverified,
    _event_time_v2_contradiction,
    _event_time_v2_path,
    _quote_timing_window,
)

_ABBREVIATIONS = _text._ABBREVIATIONS
_HTML_BLOCK_TAGS = _text._HTML_BLOCK_TAGS
_HTML_TAG = _text._HTML_TAG
_LIST_MARKER = _text._LIST_MARKER
_NUMERIC = _text._NUMERIC
_QUOTE_MARKS = _text._QUOTE_MARKS
_WORD = _text._WORD
_count_sentences = _text._count_sentences
_is_prose = _text._is_prose
_match_key = _text._match_key
_provenance_fingerprint = _text._provenance_fingerprint
_starts_at_sentence = _text._starts_at_sentence
normalize_text = _text.normalize_text

QUOTE_RULES_VERSION = "q1c_quote_rules_v4"
MAX_QUOTES_PER_HYPOTHESIS = 4
# Official statements run long (c01's key Geneva sentence is 807 characters).
MAX_QUOTE_CHARS = 900
MAX_TOTAL_QUOTE_CHARS = 1_800
# Rendered statement bound (Claim.statement); quotes are dropped whole, never cut.
MAX_STATEMENT_CHARS = 2_000
MAX_SENTENCES_PER_QUOTE = 3




@dataclass(frozen=True)
class QuoteCheck:
    hypothesis_ref: str
    evidence_id: str
    text: str
    kept: bool
    rule: str
    r4a: str | None = None
    r4b: str | None = None
    r4b_path: str | None = None
    r8: str | None = None
    roles: tuple[str, ...] | None = None
    role_reasons: dict[str, tuple[str, ...]] | None = None
    publisher: str | None = None
    publisher_family: str | None = None
    event_time: str | None = None
    source_class: str | None = None
    document_id: str | None = None
    document_key: str | None = None
    provenance_fingerprint: tuple[str, ...] | None = None
    release_session: str | None = None
    release_time_et: str | None = None










def classify_extraction_quote_roles(
    text: str,
    item: Any,
    *,
    ticker: str,
    temporal_identity: Any,
    evidence_window_policy: str = "pre_close_v1",
    move_profile: Any | None = None,
) -> EvidenceRoles:
    """Classify one extracted quote with the same role classifier used for published quotes."""
    path = None
    if item.eligible_at <= temporal_identity.cutoff_at:
        path = "pre_deadline_publication"
    elif evidence_window_policy in ("event_time_v1", "event_time_v2", "event_time_p1"):
        path = (_event_time_path if evidence_window_policy == "event_time_v1" else _event_time_v2_path)(
            item, temporal_identity, text, {}, move_profile,
        )
    return classify_evidence_roles(
        text,
        item,
        ticker=ticker,
        session_date=temporal_identity.session_date,
        move_date_is_session=None,
        r4b_path=path,
        quote_metadata={},
    )




def _filing_unit_rule(quote: str, item: Any, *, filing_rule: bool = False) -> str | None:
    """v2.1 (rule ``filing``): R2 for an SEC exhibit by its own units (catalyst_data.ingestion.collectors.sec_structure), or None when
    the rule is off, the evidence is not an SEC exhibit or its units are not stored (then the news R2 applies).

    A title is quoted whole. A body quote lies inside one bullet or paragraph, starts at the unit's start or at a
    sentence start inside it, and ends at a sentence end (a bullet may also end where the bullet ends); it must start with a capital, a
    digit or a quote mark, have at most MAX_SENTENCES_PER_QUOTE sentences and be prose. A release's lead paragraph
    follows a headline with no full stop and its bullets have none: in the flattened excerpt neither is a complete
    sentence, but each is a complete unit of the document."""
    url = getattr(item, "canonical_url", None) or ""
    if "sec.gov/" not in url or not filing_rule:
        return None
    from catalyst_data.read.sec_units import units_for
    units = units_for(url)
    if not units:
        return None
    for unit in units:
        key = _match_key(unit["text"])
        if unit["kind"] == "title":
            if quote == key:
                return "OK"
            continue
        at = key.find(quote)
        if at < 0:
            continue
        if not (at == 0 or _starts_at_sentence(key, at)):
            continue
        # a bullet may end where the bullet ends (bullets carry no full stop); a paragraph quote ends a sentence
        if not (_SENTENCE_END.search(quote) or (unit["kind"] == "bullet" and at + len(quote) == len(key))):
            continue
        if not (quote[0].isupper() or quote[0].isdigit() or quote[0] in "\"'"):
            continue
        if _count_sentences(quote) > MAX_SENTENCES_PER_QUOTE:
            return "R2_TOO_MANY_SENTENCES"
        if not _is_prose(quote):
            return "R2_NOT_PROSE"
        return "OK"
    return "R2_NOT_COMPLETE_UNIT"


def check_quote(
    quote_text: str,
    evidence_id: str,
    *,
    supporting_ids: set[str],
    items: dict[str, Any],
    temporal_identity: Any,
    evidence_window_policy: str = "pre_close_v1",
    quote_metadata: dict[str, Any] | None = None,
    hypothesis_role: str | None = None,
    move_profile: Any | None = None,
    filing_rule: bool = False,
    release_timing: dict[str, Any] | None = None,
) -> str:
    """Return ``"OK"`` or the first failing rule code."""
    item = items.get(evidence_id)
    if item is None or evidence_id not in supporting_ids:
        return "R1_NOT_SUPPORTING_PACK_EVIDENCE"
    state = getattr(item.content_state, "value", item.content_state)
    if state != "FULL_TEXT":
        return "R3_NOT_FULL_TEXT"
    start, end, eligible_at, qualified_timing = _quote_timing_window(
        item, temporal_identity, evidence_window_policy, release_timing,
    )
    is_event_time = evidence_window_policy in ("event_time_v1", "event_time_v2", "event_time_p1")
    lower_ok = eligible_at >= start if is_event_time else eligible_at > start
    if not (lower_ok and eligible_at <= end):
        return "R4a_OUTSIDE_PUBLICATION_WINDOW" if is_event_time else "R4_OUTSIDE_SESSION_WINDOW"
    quote = _match_key(quote_text)
    if not quote or len(quote) > MAX_QUOTE_CHARS:
        return "R5_QUOTE_LENGTH"
    excerpt = _match_key(item.excerpt_text or "")
    start = excerpt.find(quote)
    if start < 0:
        return "R2_NOT_VERBATIM"
    unit_rule = _filing_unit_rule(quote, item, filing_rule=filing_rule)
    if unit_rule is not None:
        if unit_rule != "OK":
            return unit_rule
    else:
        if not _starts_at_sentence(excerpt, start) or not _SENTENCE_END.search(quote):
            return "R2_NOT_COMPLETE_SENTENCE"
        after_list_marker = bool(_LIST_MARKER.search(excerpt[:start].rstrip()))
        if not (
            quote[0].isupper() or quote[0].isdigit() or quote[0] in "\"'" or after_list_marker
        ):
            return "R2_NOT_COMPLETE_SENTENCE"
        if _count_sentences(quote) > MAX_SENTENCES_PER_QUOTE:
            return "R2_TOO_MANY_SENTENCES"
        if not _is_prose(quote):
            return "R2_NOT_PROSE"
    from catalyst_data.retrieval.web_bodies import other_period_move, other_session_move

    if is_event_time:
        if _other_session_relative_to_publication(quote_text, eligible_at, temporal_identity.session_date) or other_period_move(quote, temporal_identity.session_date):
            return "R6_OTHER_SESSION_MOVE"
        if qualified_timing is not None:
            pass
        elif evidence_window_policy == "event_time_v1":
            if _event_time_unverified(
                item, temporal_identity, quote_text, quote_metadata or {}, move_profile
            ):
                return "R4b_EVENT_TIME_UNVERIFIED"
        elif _event_time_v2_contradiction(
            item, temporal_identity, quote_text, quote_metadata or {}, move_profile
        ):
            return "R4b_EVENT_TIME_CONTRADICTION"
        if (evidence_window_policy in ("event_time_v1", "event_time_v2")
                and hypothesis_role == "PRIMARY" and _hedged_link_without_strong_link(quote_text)):
            return "R8_HEDGED_LINK"
    elif other_session_move(
        quote, temporal_identity.session_open_at.strftime("%A"), temporal_identity.session_date
    ) or other_period_move(quote, temporal_identity.session_date):
        return "R6_OTHER_SESSION_MOVE"
    return "OK"






































_SOURCE_LABELS = {
    "sec": "SEC filing",
    "issuer_disclosure": "company filing",
    "corporate_press_release": "company press release",
    "official_government": "government release",
    "reported_news": "news report",
    "analysis_opinion": "analysis article",
}


def _source_label(item: Any) -> str:
    # The corpus records SEC filings as official_government; name them.
    if "sec.gov/" in (getattr(item, "canonical_url", None) or ""):
        return "SEC filing"
    publisher = (getattr(item, "publisher", None) or "").strip()
    source_class = str(getattr(item.source_class, "value", item.source_class))
    if publisher.lower() in _SOURCE_LABELS:
        return _SOURCE_LABELS[publisher.lower()]
    return publisher or _SOURCE_LABELS.get(source_class, source_class)


def render_quotes(
    kept: list[tuple[str, Any]],
    *, release_timings: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Code-owned statement text: quotes with publisher, time and link."""
    from catalyst_agents.runtime.earnings_timing import public_release_fields

    parts = []
    for text, item in kept:
        timing = (release_timings or {}).get(getattr(item, "evidence_id", ""))
        public_fields = public_release_fields(timing)
        when = (
            f"{public_fields['release_session']} release {public_fields['release_time_et']} ET"
            if public_fields else item.eligible_at.strftime("%Y-%m-%d %H:%M UTC")
        )
        url = getattr(item, "canonical_url", None)
        tail = f", {url}" if url else ""
        parts.append(f"“{normalize_text(text)}” — {_source_label(item)}, {when}{tail}")
    return " ".join(parts)


def _quote_check_record(
    *, hypothesis_ref: str, evidence_id: str, text: str, quote: Mapping[str, Any],
    item: Any | None, rule: str, context_pack: Any, temporal_identity: Any,
    evidence_window_policy: str, role: str,
    release_timing: Mapping[str, Any] | None,
) -> QuoteCheck:
    """Build the per-quote audit record independently of payload rewriting."""
    from catalyst_agents.runtime.earnings_timing import public_release_fields

    observation = getattr(context_pack, "observation", None)
    path = None
    r4a_status = r4b_status = r8_status = None
    qualified_timing = None
    if item is not None:
        publication_start, publication_end, check_time, qualified_timing = _quote_timing_window(
            item, temporal_identity, evidence_window_policy, release_timing,
        )
    if evidence_window_policy in ("event_time_v1", "event_time_v2", "event_time_p1") and item is not None:
        in_window = publication_start <= check_time <= publication_end
        r4a_status = "PASS" if in_window else "FAIL"
        if not in_window:
            r4b_status = "NOT_RUN"
        elif qualified_timing is not None:
            r4b_status = "PASS"
            path = "issuer_earnings_release"
        elif evidence_window_policy == "event_time_v1":
            r4b_status = "FAIL" if _event_time_unverified(
                item, temporal_identity, text, quote, observation,
            ) else "PASS"
        else:
            r4b_status = "FAIL" if _event_time_v2_contradiction(
                item, temporal_identity, text, quote, observation,
            ) else "PASS"
        r8_status = (
            "FAIL" if role == "PRIMARY" and _hedged_link_without_strong_link(text)
            else "PASS" if role == "PRIMARY" else "NOT_RUN"
        )
        if in_window and r4b_status == "PASS":
            if qualified_timing is not None:
                path = "issuer_earnings_release"
            elif evidence_window_policy == "event_time_v1":
                path = _event_time_path(item, temporal_identity, text, quote, observation)
            else:
                path = _event_time_v2_path(item, temporal_identity, text, quote, observation)

    roles = role_reasons = publisher = family = event_time = None
    source_class = document_id = document_key = provenance_fingerprint = None
    if evidence_window_policy in ("event_time_v1", "event_time_v2", "event_time_p1") and item is not None:
        if rule == "OK":
            classified = classify_evidence_roles(
                text, item,
                ticker=str(getattr(observation, "ticker", "") or ""),
                session_date=temporal_identity.session_date,
                move_date_is_session=None,
                r4b_path=path,
                quote_metadata=quote,
            )
            roles, role_reasons, event_time = (
                classified.roles, classified.reasons, classified.event_time,
            )
        publisher = getattr(item, "publisher", None) or _source_label(item)
        family = publisher_family(item)
        source_class = str(getattr(
            getattr(item, "source_class", None), "value", getattr(item, "source_class", "")
        ))
        document_id = str(
            getattr(item, "document_id", None) or re.sub(r":web_body:\d+$", "", evidence_id)
        )
        document_key = str(getattr(item, "canonical_url", None) or document_id)
        provenance_fingerprint = _provenance_fingerprint(
            str(getattr(item, "excerpt_text", "") or "")
        )
    public_fields = public_release_fields(qualified_timing)
    return QuoteCheck(
        hypothesis_ref, evidence_id, text, rule == "OK", rule,
        r4a=r4a_status, r4b=r4b_status, r4b_path=path, r8=r8_status,
        roles=roles, role_reasons=role_reasons, publisher=publisher,
        publisher_family=family, event_time=event_time, source_class=source_class,
        document_id=document_id, document_key=document_key,
        provenance_fingerprint=provenance_fingerprint,
        release_session=public_fields.get("release_session") if public_fields else None,
        release_time_et=public_fields.get("release_time_et") if public_fields else None,
    )


def _rewrite_quote_payload(
    payload: dict, kept_hypotheses: list[dict],
) -> dict:
    """Replace model prose with checked quotes and prune dangling references."""
    kept_refs = {h["hypothesis_ref"] for h in kept_hypotheses}
    decisions = [
        {
            key: value for key, value in decision.items()
            if key not in ("supports_hypothesis_refs", "contradicts_hypothesis_refs")
        }
        for decision in payload.get("evidence_decisions") or ()
    ]
    proposals = [
        {
            **proposal,
            "related_hypothesis_refs": [
                ref for ref in proposal.get("related_hypothesis_refs") or () if ref in kept_refs
            ],
        }
        for proposal in payload.get("proposed_missing_evidence") or ()
    ]
    rewritten = {
        **payload,
        "candidate_hypotheses": kept_hypotheses,
        "evidence_decisions": decisions,
        "proposed_missing_evidence": proposals,
    }
    if not kept_hypotheses and rewritten.get("recommended_status") != "ABSTAIN":
        rewritten["recommended_status"] = "ABSTAIN"
    return rewritten


def apply_quote_rules(
    payload: dict,
    context_pack: Any,
    *, evidence_window_policy: str = "pre_close_v1", filing_rule: bool = False,
    release_timings: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict, tuple[QuoteCheck, ...]]:
    """Filter a raw AnalystDecision payload to quote-backed hypotheses.

    Returns the rewritten payload (hypotheses keep only quoted support, their
    statement becomes the rendered quotes, mechanism is cleared; hypotheses
    without a surviving quote are removed together with every reference to
    them) and one check record per proposed quote.
    """
    from catalyst_agents.runtime.earnings_timing import public_release_fields

    items = {
        item.evidence_id: item
        for item in context_pack.evidence_inventory
        if item.evidence_id in set(context_pack.included_evidence_ids)
    }
    temporal_identity = context_pack.temporal_identity
    if release_timings is None:
        release_timings = {}
    release_timings = {
        evidence_id: timing
        for evidence_id, timing in release_timings.items()
        if timing.get("attributed_session_date") == temporal_identity.session_date
    }
    checks: list[QuoteCheck] = []
    kept_hypotheses: list[dict] = []
    total_chars = 0
    used_quotes: set[tuple[str, str]] = set()
    for hypothesis in payload.get("candidate_hypotheses") or ():
        ref = hypothesis.get("hypothesis_ref", "")
        supporting = set(hypothesis.get("supporting_evidence_ids") or ())
        role = str(hypothesis.get("proposed_role") or "")
        kept: list[tuple[str, Any]] = []
        kept_quote_payloads: list[dict[str, Any]] = []
        kept_check_indices: list[int] = []
        kept_quote_keys: list[tuple[str, str]] = []
        kept_quote_chars: list[int] = []
        for quote in (hypothesis.get("quotes") or ())[:MAX_QUOTES_PER_HYPOTHESIS]:
            text = str(quote.get("text", ""))
            evidence_id = str(quote.get("evidence_id", ""))
            release_timing = release_timings.get(evidence_id)
            if release_timing and release_timing.get("attributed_session_date") != temporal_identity.session_date:
                release_timing = None
            rule = check_quote(
                text, evidence_id, supporting_ids=supporting, items=items,
                temporal_identity=temporal_identity,
                evidence_window_policy=evidence_window_policy,
                quote_metadata=quote,
                hypothesis_role=role,
                move_profile=getattr(context_pack, "observation", None),
                filing_rule=filing_rule,
                release_timing=release_timing,
            )
            if rule == "OK" and total_chars + len(normalize_text(text)) > MAX_TOTAL_QUOTE_CHARS:
                rule = "R5_ANSWER_LENGTH"
            key = (evidence_id, _match_key(text))
            if rule == "OK" and key in used_quotes:
                rule = "R5_DUPLICATE_QUOTE"  # already published by an earlier hypothesis
            if rule == "OK":
                used_quotes.add(key)
            if rule == "OK":
                total_chars += len(normalize_text(text))
                kept.append((text, items[evidence_id]))
                kept_quote_payloads.append({
                    "evidence_id": evidence_id, "text": text,
                    **(public_release_fields(release_timing) or {}),
                    **({key: quote[key] for key in ("event_time_quote", "event_relation", "event_date") if quote.get(key) is not None}
                       if evidence_window_policy in ("event_time_v1", "event_time_v2") else {}),
                })
            item = items.get(evidence_id)
            check = _quote_check_record(
                hypothesis_ref=ref, evidence_id=evidence_id, text=text, quote=quote,
                item=item, rule=rule, context_pack=context_pack,
                temporal_identity=temporal_identity,
                evidence_window_policy=evidence_window_policy, role=role,
                release_timing=release_timing,
            )
            checks.append(check)
            if rule == "OK":
                kept_check_indices.append(len(checks) - 1)
                kept_quote_keys.append(key)
                kept_quote_chars.append(len(normalize_text(text)))
        if not kept:
            continue
        while kept and len(render_quotes(kept, release_timings=release_timings)) > MAX_STATEMENT_CHARS:
            kept.pop()  # drop whole quotes; a quote is never truncated
            kept_quote_payloads.pop()
            total_chars -= kept_quote_chars.pop()
            used_quotes.discard(kept_quote_keys.pop())
            check_index = kept_check_indices.pop()
            checks[check_index] = replace(
                checks[check_index], kept=False, rule="R5_STATEMENT_LENGTH",
            )
        if not kept:
            continue
        quoted_ids = []
        for _, item in kept:
            if item.evidence_id not in quoted_ids:
                quoted_ids.append(item.evidence_id)
        kept_hypotheses.append(
            {
                **hypothesis,
                "statement": render_quotes(kept, release_timings=release_timings),
                "mechanism": None,
                "supporting_evidence_ids": quoted_ids,
                "quotes": kept_quote_payloads,
            }
        )
    return _rewrite_quote_payload(payload, kept_hypotheses), tuple(checks)


__all__ = [
    "QUOTE_RULES_VERSION",
    "QuoteCheck",
    "apply_quote_rules",
    "check_quote",
    "classify_extraction_quote_roles",
    "normalize_text",
    "render_quotes",
]
