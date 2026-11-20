"""Render-time, JSON-safe answer projection built beside the quote renderer.

Headline template uses "as" for an ALIGNED governing reference, "while" for an
opposite-direction reference, and relative-performance wording for a same-
direction DIVERGENT reference. The reference follows the tier priority:
commodity, sector, peers, then market. With no target return the
headline states that fact; with no reference it reports only the target move.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from catalyst_agents.attribution.quotes import normalize_text
from catalyst_agents.observability.decision_trace import ABSTENTION_REASON_CODES

ANSWER_BASIS_NOTE = "The cited source text is shown below. The system checked each quote against its source and the applicable evidence window."
ABSTAIN_LIMITATION = (
    "No cause was published for this move: no source sentence within the evidence window was accepted as its cause."
)
# The earlier wording stays recognised so snapshots written before M1-4c are still filtered.
ABSTAIN_LIMITATION_PREFIXES = ("No cause was published for this move:", "No cause was accepted:")

_PRIMARY_SOURCE_CLASSES = {
    "sec", "issuer_disclosure", "corporate_press_release", "official_government",
}


class AnswerGrade(StrEnum):
    ORDINARY = "ORDINARY"
    MOVED_WITH_GROUP = "MOVED_WITH_GROUP"
    CONFIRMED = "CONFIRMED"
    REPORTED = "REPORTED"
    NO_CLEAR_CAUSE = "NO_CLEAR_CAUSE"
    SUPPORTED = "SUPPORTED"


def _date(value: Any) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value is not None else None


_UNSET = object()


def _headline(ticker: str, profile: Any, governing_ref: Any = _UNSET) -> str:
    target = getattr(profile, "target_return", None)
    if target is None:
        return f"No close-to-close return is available for {ticker}."
    direction = "rose" if target > 0 else "fell" if target < 0 else "was flat"
    refs = (
        ("commodity", getattr(profile, "commodity_ticker", None), getattr(profile, "commodity_return", None), "ALIGNED"),
        ("sector", getattr(profile, "sector_ticker", None), getattr(profile, "sector_return", None), getattr(getattr(profile, "sector_comove", None), "band", None)),
        ("peers", "peer median", getattr(getattr(profile, "peer_summary", None), "median_return_pct", None), getattr(getattr(profile, "peer_comove", None), "band", None)),
        ("market", "SPY", getattr(profile, "market_return", None), getattr(getattr(profile, "market_comove", None), "band", None)),
    )
    names = {"sector": "its sector", "market": "the market", "peers": "its peers", "commodity": "its commodity"}
    if governing_ref is not _UNSET:
        if not governing_ref:
            return f"{ticker} {direction} {abs(target):.2f}%."
        kind = governing_ref.get("kind")
        selected = next((item for item in refs if item[0] == kind), None)
        if selected is None:
            return f"{ticker} {direction} {abs(target):.2f}%."
        label, ref_ticker, _, _ = selected
        value = governing_ref.get("return_pct")
        if value is None:
            return f"{ticker} {direction} {abs(target):.2f}%."
        refs = ((label, ref_ticker, value, governing_ref.get("band")),)
    for label, ref_ticker, value, band in refs:
        if value is None:
            continue
        ref_direction = "rose" if value > 0 else "fell" if value < 0 else "was flat"
        same_direction = (value > 0) == (target > 0) and value != 0 and target != 0
        band = getattr(band, "value", band)
        if label == "commodity" and same_direction:
            band = "ALIGNED"
        if not same_direction:
            return f"{ticker} {direction} {abs(target):.2f}% while {names[label]} {ref_direction} ({ref_ticker or label} {value:+.2f}%)."
        if band == "DIVERGENT":
            comparison = "ahead of" if target > value else "below"
            return f"{ticker} {direction} {abs(target):.2f}%, well {comparison} {names[label]} ({ref_ticker or label} {value:+.2f}%)."
        joiner = "as" if band in (None, "ALIGNED") else "while"
        shown = ref_ticker or label
        return f"{ticker} {direction} {abs(target):.2f}% {joiner} {names[label]} {ref_direction} ({shown} {value:+.2f}%)."
    return f"{ticker} {direction} {abs(target):.2f}%."


def _quote(item: Any, text: str, role: str, claim_id: str | None = None) -> dict[str, Any]:
    from catalyst_agents.attribution.quotes import _source_label, publisher_family

    return {
        "role": role,
        "text": normalize_text(text),
        "publisher": _source_label(item),
        **({"publisher_family": publisher_family(item), "origin": "web"}
           if getattr(item, "origin", None) == "web" else {}),
        **({"body_source": getattr(item, "body_source")}
           if getattr(item, "body_source", None) is not None else {}),
        "published_at": _date(getattr(item, "eligible_at", None)),
        "url": getattr(item, "canonical_url", None),
        "evidence_id": getattr(item, "evidence_id", None),
        **({"claim_id": claim_id} if claim_id is not None else {}),
    }


def grade_event_time_hypothesis(
    *, tier: str, status: str, hypotheses: Any, verifier: dict[str, Any],
    checks: Any, gates: Any, ceiling_codes: Any = (),
) -> dict[str, Any]:
    """Derive an event-time grade without allowing it to raise a prior ceiling."""
    if tier == "T1":
        return {"grade": AnswerGrade.ORDINARY.value, "reason_codes": ["TIER_T1_ORDINARY"]}
    if tier == "T2":
        return {"grade": AnswerGrade.MOVED_WITH_GROUP.value, "reason_codes": ["TIER_T2_ALIGNED_REFERENCE"]}

    primary_refs = [
        h.get("ref") for h in hypotheses
        if h.get("proposed_role") == "PRIMARY"
    ]
    reasons: list[str] = []
    if not primary_refs:
        return {"grade": AnswerGrade.NO_CLEAR_CAUSE.value, "reason_codes": ["NO_PRIMARY_HYPOTHESIS"]}
    primary_ref = primary_refs[0]
    primary_checks = [
        check for check in checks
        if check.get("hypothesis_ref") == primary_ref and check.get("kept")
    ]
    attribution_quotes = [c for c in primary_checks if "ATTRIBUTION" in c.get("roles", ())]
    event_quotes = [c for c in primary_checks if "EVENT" in c.get("roles", ())]
    failed_gate_names = sorted({
        g.get("gate", "unknown") for g in gates
        if g.get("result") == "ceiling:ABSTAIN"
    })
    ceiling_codes = set(ceiling_codes or ())
    reported_only_ceilings = {
        "MISSING_PRIMARY_CONFIRMATION", "MISSING_INDEPENDENT_CORROBORATION",
    }
    status_blocked = status == "ABSTAIN" and not (
        ceiling_codes and ceiling_codes <= reported_only_ceilings and not failed_gate_names
    )
    status_exception = None
    if status_blocked:
        reasons.append("ASSESSMENT_ABSTAINED")
    elif status == "ABSTAIN":
        status_exception = "REPORTED_CEILING_EXCEPTION:" + ",".join(sorted(ceiling_codes))
    if failed_gate_names:
        reasons.extend("GATE_ABSTAIN:" + name for name in failed_gate_names)
    if verifier.get("verdict") != "SUPPORTED":
        reasons.append("VERIFIER_NOT_SUPPORTED")
    if not attribution_quotes:
        reasons.append("NO_PRIMARY_ATTRIBUTION_QUOTE")
    if reasons and not all(reason.startswith("REPORTED_CEILING_EXCEPTION:") for reason in reasons):
        return {"grade": AnswerGrade.NO_CLEAR_CAUSE.value, "reason_codes": reasons,
                "primary_hypothesis_ref": primary_ref}

    key_quote = max(
        attribution_quotes,
        key=lambda quote: ("EVENT" in quote.get("roles", ()), len(quote.get("text", ""))),
    )
    from catalyst_agents.attribution.quotes import normalize_text
    corroborating_events = []
    for candidate in event_quotes:
        event_class = str(candidate.get("source_class", "")).lower()
        if event_class in _PRIMARY_SOURCE_CLASSES:
            corroborating_events.append(candidate)
            continue
        event_sentence = normalize_text(str(candidate.get("text", ""))).casefold()
        event_document = candidate.get("document_key") or candidate.get("document_id")
        event_fingerprint = set(candidate.get("provenance_fingerprint") or ())
        independent = any(
            event_document != (attribution.get("document_key") or attribution.get("document_id"))
            and event_sentence != normalize_text(str(attribution.get("text", ""))).casefold()
            and not event_fingerprint.intersection(
                attribution.get("provenance_fingerprint") or ()
            )
            for attribution in attribution_quotes
        )
        if independent:
            corroborating_events.append(candidate)
    event_quote = next((quote for quote in corroborating_events if quote is not key_quote), None)
    if event_quote is None and corroborating_events:
        event_quote = corroborating_events[0]
    corroborates = bool(corroborating_events)
    if corroborates:
        grade = "CONFIRMED"
        reasons = ["PRIMARY_SURVIVED", "VERIFIER_SUPPORTED", "ATTRIBUTION_PRESENT",
                   "INDEPENDENT_DOCUMENT_OR_PRIMARY_EVENT_PRESENT"]
    else:
        grade = "REPORTED"
        reasons = ["PRIMARY_SURVIVED", "VERIFIER_SUPPORTED", "ATTRIBUTION_PRESENT",
                   "EVENT_NOT_INDEPENDENTLY_CORROBORATED"]
    if status_exception:
        reasons.append(status_exception)
    unique_families = []
    unique_publishers = []
    seen_sentences = set()
    from catalyst_agents.attribution.quotes import normalize_text

    for quote in attribution_quotes:
        sentence_key = normalize_text(str(quote.get("text", ""))).casefold()
        family = quote.get("publisher_family") or "unknown"
        if sentence_key not in seen_sentences:
            seen_sentences.add(sentence_key)
            if family not in unique_families:
                unique_families.append(family)
                unique_publishers.append({
                    "publisher": quote.get("publisher"),
                    "publisher_family": family,
                })
    if len(unique_families) >= 2:
        reasons.append("ATTRIBUTION_HAS_MULTIPLE_PUBLISHER_FAMILIES")
    return {
        "grade": grade,
        "reason_codes": reasons,
        "primary_hypothesis_ref": primary_ref,
        "key_evidence_id": key_quote.get("evidence_id"),
        "key_quote_text": key_quote.get("text"),
        "event_evidence_id": event_quote.get("evidence_id") if event_quote else None,
        "event_quote_text": event_quote.get("text") if event_quote else None,
        "reported_by": unique_publishers,
    }


def grade_event_time_v2_hypothesis(
    *, tier: str, published: bool, hypotheses: Any, verifier: dict[str, Any],
    checks: Any, gates: Any, ceiling_codes: Any = (),
) -> dict[str, Any]:
    """Label evidence strength without changing the pre_close publication decision."""
    if tier == "T1":
        return {"grade": AnswerGrade.ORDINARY.value, "reason_codes": ["TIER_T1_ORDINARY"]}
    if tier == "T2":
        return {"grade": AnswerGrade.MOVED_WITH_GROUP.value, "reason_codes": ["TIER_T2_ALIGNED_REFERENCE"]}
    if not published or verifier.get("verdict") != "SUPPORTED":
        return {"grade": AnswerGrade.NO_CLEAR_CAUSE.value, "reason_codes": ["NOT_PUBLISHED"]}
    role_grade = grade_event_time_hypothesis(
        tier=tier, status="ANSWERED", hypotheses=hypotheses, verifier=verifier,
        checks=checks, gates=gates, ceiling_codes=ceiling_codes,
    )
    if role_grade.get("grade") in {"CONFIRMED", "REPORTED"}:
        return role_grade
    primary_refs = {
        h.get("ref") for h in hypotheses if h.get("proposed_role") == "PRIMARY"
    }
    primary_checks = [
        check for check in checks
        if check.get("hypothesis_ref") in primary_refs and check.get("kept")
    ]
    first_primary = primary_checks[0] if primary_checks else None
    return {
        "grade": AnswerGrade.SUPPORTED.value,
        "reason_codes": ["VERIFIER_SUPPORTED", "ATTRIBUTION_ROLE_UNVERIFIED"],
        **({
            "primary_hypothesis_ref": first_primary.get("hypothesis_ref"),
            "key_evidence_id": first_primary.get("evidence_id"),
            "key_quote_text": first_primary.get("text"),
        } if first_primary else {}),
    }


def possible_explanation_quotes(decision_trace: dict[str, Any], context_pack: Any) -> list[dict[str, Any]]:
    """Structured PRIMARY quotes that passed R1-R6 for an UNLINKED verifier result."""
    rounds = decision_trace.get("rounds", ())
    current = rounds[-1] if rounds else {}
    analyst = current.get("model_reported", {}).get("analyst", {})
    verifier = current.get("model_reported", {}).get("verifier", {})
    rejected_category = verifier.get("category") if verifier.get("verdict") == "REJECT" else None
    primary_refs = {
        item.get("ref") for item in analyst.get("hypotheses", ())
        if item.get("proposed_role") == "PRIMARY"
    }
    inventory = {item.evidence_id: item for item in context_pack.evidence_inventory}
    result = []
    for check in current.get("code_checks", {}).get("quotes", ()):
        event_time_mode = check.get("roles") is not None
        if not check.get("kept") or check.get("hypothesis_ref") not in primary_refs:
            continue
        if event_time_mode and "EVENT" not in check.get("roles", ()):
            continue
        if event_time_mode and rejected_category in {"OTHER_COMPANY", "OTHER_SESSION", "PREVIEW"}:
            continue
        if event_time_mode and "EVENT_SUBJECT_MATCH" not in check.get("role_reasons", {}).get("EVENT", ()):
            continue
        item = inventory.get(check.get("evidence_id"))
        if item is None:
            continue
        quote = _quote(item, check.get("text", ""), "PRIMARY")
        if event_time_mode:
            quote.update({
                "label": "Event verified before the move; link to the move is not established.",
                "roles": list(check.get("roles", ())),
                "role_reasons": check.get("role_reasons", {}),
                "publisher_family": check.get("publisher_family"),
                "event_time": check.get("event_time"),
                "r4b_path": check.get("r4b_path"),
            })
        result.append({key: value for key, value in quote.items() if key != "role" and key != "claim_id"})
    return result


def _reference(profile: Any, alignment_name: str, ticker: str | None) -> dict[str, Any]:
    alignment = getattr(profile, alignment_name, None)
    return {
        "ticker": ticker,
        "return_pct": getattr(alignment, "reference_return_pct", None),
        "band": getattr(getattr(alignment, "band", None), "value", None),
        "relation": getattr(getattr(alignment, "direction_relation", None), "value", None),
        "residual_pct": getattr(alignment, "residual_return_pct", None),
    }


def build_facts_snapshot(
    *, ticker: str, move_profile: Any, temporal_identity: Any,
    facts_text: str = "", evidence_inventory: Any = (), governing_ref: Any = _UNSET,
) -> dict[str, Any]:
    """Build the B4 facts projection for early progress and the final snapshot."""
    profile = move_profile
    peer_summary = getattr(profile, "peer_summary", None)
    peer_alignment = getattr(profile, "peer_comove", None)
    peers = {
        "median_return_pct": getattr(peer_summary, "median_return_pct", None),
        "band": getattr(getattr(peer_alignment, "band", None), "value", None),
        "relation": getattr(getattr(peer_alignment, "direction_relation", None), "value", None),
        "residual_pct": getattr(peer_alignment, "residual_return_pct", None),
        "count": getattr(peer_summary, "available_peer_count", None),
        "items": [{"ticker": p.ticker, "return_pct": p.return_pct} for p in getattr(peer_summary, "peer_returns", ())],
    }
    filings = []
    seen = set()
    publication_window = getattr(temporal_identity, "publication_window", None)
    if callable(publication_window):
        publication_start, publication_end = publication_window()
    else:
        publication_start = getattr(temporal_identity, "evidence_window_start_at", None) or temporal_identity.information_window_start_at
        publication_end = getattr(temporal_identity, "evidence_window_end_at", None) or temporal_identity.cutoff_at
    for item in evidence_inventory:
        url = getattr(item, "canonical_url", None)
        eligible = getattr(item, "eligible_at", None)
        if not url or "sec.gov/" not in url or url in seen or eligible is None:
            continue
        if ((eligible >= publication_start if temporal_identity.evidence_window_start_at is not None else eligible > publication_start)
                and eligible <= publication_end):
            seen.add(url)
            filings.append({"url": url, "eligible_at": eligible.isoformat()})
    filings.sort(key=lambda f: f["eligible_at"])
    window = {
            "information_window_start_at": temporal_identity.information_window_start_at.isoformat(),
            "cutoff_at": temporal_identity.cutoff_at.isoformat(),
            "session_open_at": temporal_identity.session_open_at.isoformat(),
        }
    if temporal_identity.evidence_window_start_at is not None:
        window.update({
            "evidence_window_start_at": temporal_identity.evidence_window_start_at.isoformat(),
            "evidence_window_end_at": temporal_identity.evidence_window_end_at.isoformat(),
        })
    return {
        "window": window,
        "facts": {
            "target_return_pct": getattr(profile, "target_return", None),
            "gap_return_pct": getattr(profile, "gap_return", None),
            "market": _reference(profile, "market_comove", None),
            "sector": _reference(profile, "sector_comove", getattr(profile, "sector_ticker", None)),
            "peers": peers,
            "commodity": ({"ticker": profile.commodity_ticker, "return_pct": profile.commodity_return}
                          if profile.commodity_ticker is not None or profile.commodity_return is not None else None),
            "comove_sentence": facts_text or None,
            "filings": filings,
        },
        "headline": _headline(ticker, profile, governing_ref),
    }


def derive_abstention_reason(trace: dict[str, Any]) -> dict[str, Any] | None:
    """Classify a recorded abstention from persisted, code-visible decisions.

    Returns ``None`` when an older trace does not contain enough evidence to
    distinguish the reason. This function is observational and never feeds
    back into attribution or answer rendering.
    """
    verifiers = [r.get("model_reported", {}).get("verifier", {}) for r in trace.get("rounds", ())]
    verifier = next((v for v in reversed(verifiers) if v), {})
    rounds = trace.get("rounds", ())
    current = rounds[-1] if rounds else {}
    reported = current.get("model_reported", {})
    analyst = reported.get("analyst", {})
    hypotheses = analyst.get("hypotheses", ())
    primary_refs = {h.get("ref") for h in hypotheses if h.get("proposed_role") == "PRIMARY"}
    checks = current.get("code_checks", {}).get("quotes", ())
    primary_checks = [c for c in checks if c.get("hypothesis_ref") in primary_refs]
    kept_primary = any(c.get("kept") for c in primary_checks)
    retrieval = trace.get("retrieval") or {}

    def result(code: str, **details: Any) -> dict[str, Any]:
        return {"reason_code": code, "details": details}

    # Empty inventory is the only safe trace-level proof of an evidence gap.
    included = retrieval.get("included_in_pack")
    evidence_count = retrieval.get("evidence_in_state")
    if included == 0 or (included is None and evidence_count == 0):
        return result("NO_STOCK_NEWS")
    if (current.get("desk_decision") or {}).get("gate", {}).get("reason_code") == "STALE_PRIMARY":
        return result("STALE_PRIMARY")
    if not primary_refs:
        if included is None and evidence_count is None:
            return None
        return result("NO_PRIMARY_PROPOSED")
    if not kept_primary:
        failed_rules = sorted({c.get("rule") for c in primary_checks
                               if c.get("kept") is False and isinstance(c.get("rule"), str)})
        if not primary_checks and included is None and evidence_count is None:
            return None
        return result("PRIMARY_QUOTES_FAILED", failed_quote_rule_codes=failed_rules)

    if analyst.get("recommended_status") == "ABSTAIN":
        return result("ALREADY_ABSTAIN")

    gate_checks = current.get("code_checks", {}).get("gates", ())
    failed_gates = sorted({g.get("gate") for g in gate_checks
                           if g.get("result") == "ceiling:ABSTAIN"
                           and isinstance(g.get("gate"), str)})
    if failed_gates:
        return result("GATE_FAILED", failed_gate_names=failed_gates)

    if verifier.get("skipped_because") == "already_abstain":
        return result("ALREADY_ABSTAIN")
    if verifier.get("called"):
        if verifier.get("verdict") == "REJECT":
            details = {"verifier_reason": verifier.get("category") or "OTHER"}
            if isinstance(verifier.get("reason"), str) and verifier["reason"]:
                details["verifier_note"] = verifier["reason"]
            return result("VERIFIER_REJECTED", **details)
        elif verifier.get("verdict") == "SUPPORTED":
            outcome = current.get("code_checks", {}).get("outcome", {})
            codes = outcome.get("ceiling_codes", ())
            return result("CEILING_ABSTAIN", ceiling_codes=sorted(set(codes)))
        else:
            return result("VERIFIER_NO_VERDICT")
    if verifier.get("skipped_because") == "no_primary":
        # With old traces, raw proposals plus R1-R6 records preserve the
        # distinction even though the verifier skip reason itself is shared.
        return result("NO_PRIMARY_PROPOSED") if not primary_refs else result(
            "PRIMARY_QUOTES_FAILED",
            failed_quote_rule_codes=sorted({c.get("rule") for c in primary_checks
                                            if c.get("kept") is False and c.get("rule")}),
        )
    if verifier.get("primary_proposal_state") == "PROPOSED_QUOTES_KEPT":
        outcome = current.get("code_checks", {}).get("outcome", {})
        return result("CEILING_ABSTAIN", ceiling_codes=sorted(set(outcome.get("ceiling_codes", ()))))
    return None


_ABSTENTION_REASON_TEXT = {
    "NO_STOCK_NEWS": "No in-window evidence about this stock was available to the analyst.",
    "NO_PRIMARY_PROPOSED": "No primary cause was proposed for publication.",
    "PRIMARY_QUOTES_FAILED": "The proposed primary cause had no quote that passed the quote checks.",
    "GATE_FAILED": "A required attribution gate blocked the proposed cause.",
    "VERIFIER_REJECTED": "The verifier rejected the proposed primary cause.",
    "VERIFIER_NO_VERDICT": "The verifier did not return a usable verdict for the primary cause.",
    "CEILING_ABSTAIN": "The evidence and policy checks limited the attribution to an abstention.",
    "ALREADY_ABSTAIN": "The analyst abstained before a cause could be verified.",
    "STALE_PRIMARY": "The leading candidate event happened before this session's window (prior close to close).",
}


def abstention_reason_text(code: str) -> str:
    return _ABSTENTION_REASON_TEXT[code]


def _abstention_reason(trace: dict[str, Any], assessment: Any) -> tuple[str, str]:
    reason = trace.get("abstention_reason")
    if not isinstance(reason, dict) or reason.get("reason_code") not in ABSTENTION_REASON_CODES:
        reason = derive_abstention_reason(trace)
    code = reason["reason_code"] if reason else "CEILING_ABSTAIN"
    return code, abstention_reason_text(code)


def build_answer_snapshot(
    *, run_id: str, ticker: str, move_profile: Any, temporal_identity: Any,
    facts_text: str, assessment: Any, validated_claim_plan: Any,
    context_pack: Any, decision_trace: dict[str, Any], abstention_context: dict[str, Any],
    evidence_window_policy: str = "pre_close_v1",
    governing_ref: Any = _UNSET,
) -> dict[str, Any]:
    """Project the exact code-rendered claims and frozen run facts to JSON."""
    profile = move_profile
    inventory = {item.evidence_id: item for item in context_pack.evidence_inventory}
    raw_rounds = decision_trace.get("rounds", ())
    last_round = raw_rounds[-1] if raw_rounds else {}
    raw_hypotheses = last_round.get("model_reported", {}).get("analyst", {}).get("hypotheses", ())
    raw_by_ref = {h.get("ref"): h for h in raw_hypotheses}
    checked = last_round.get("code_checks", {}).get("quotes", ())
    role_by_ref: dict[str, tuple[str, str]] = {}
    source_ref_by_id = {h.hypothesis_id: h.source_hypothesis_ref for h in assessment.normalized_hypotheses}
    for claim in validated_claim_plan.claims:
        source_ref = source_ref_by_id.get(claim.source_hypothesis_id)
        if source_ref and claim.role.value in {"PRIMARY", "SECONDARY", "CONTEXT"}:
            role_by_ref[source_ref] = (claim.role.value, claim.claim_id)

    sources = []
    for check in checked:
        if not check.get("kept"):
            continue
        ref = check.get("hypothesis_ref")
        role_and_claim = role_by_ref.get(ref)
        raw = raw_by_ref.get(ref, {})
        proposed_role = raw.get("proposed_role")
        role = role_and_claim[0] if role_and_claim else proposed_role
        if role not in {"PRIMARY", "SECONDARY", "CONTEXT"}:
            continue
        item = inventory.get(check.get("evidence_id"))
        if item is None:
            continue
        quote = _quote(item, check.get("text", ""), role, role_and_claim[1] if role_and_claim else None)
        if check.get("release_session") is not None:
            quote["release_session"] = check["release_session"]
            quote["release_time_et"] = check.get("release_time_et")
        if evidence_window_policy in ("event_time_v1", "event_time_v2"):
            quote.update({
                "roles": list(check.get("roles", ("LEAD",))),
                "publisher_family": check.get("publisher_family") or "unknown",
                "event_time": check.get("event_time"),
                "r4b_path": check.get("r4b_path"),
                "source_class": check.get("source_class"),
            })
        if role_and_claim:
            sources.append(quote)
    primary = next((q for q in sources if q["role"] == "PRIMARY"), None)
    secondary = next((q for q in sources if q["role"] == "SECONDARY"), None)
    def key_quote(q: dict[str, Any] | None) -> dict[str, Any] | None:
        if q is None:
            return None
        fields = ("text", "publisher", "published_at", "url", "evidence_id")
        payload = {field: q[field] for field in fields}
        for field in ("release_session", "release_time_et"):
            if q.get(field) is not None:
                payload[field] = q[field]
        return payload

    facts_projection = build_facts_snapshot(
        ticker=ticker, move_profile=profile, temporal_identity=temporal_identity,
        facts_text=facts_text, evidence_inventory=context_pack.evidence_inventory,
        governing_ref=governing_ref,
    )
    status = getattr(assessment.final_status, "value", assessment.final_status)
    answered = status != "ABSTAIN"
    snapshot: dict[str, Any] = {
        "schema_version": "answer_snapshot_v1",
        "run_id": run_id,
        "ticker": ticker,
        "session_date": temporal_identity.session_date,
        "outcome": "ANSWERED" if answered else "ABSTAINED",
        "status": status,
        **facts_projection,
        "key_sentence": key_quote(primary),
        "also_sentence": key_quote(secondary),
        "sources": sources,
        "limitations": (
            [ABSTAIN_LIMITATION]
            + list(validated_claim_plan.required_limitations)
            if not answered else
            list(validated_claim_plan.required_limitations)
        ),
    }
    if evidence_window_policy in ("event_time_v1", "event_time_v2"):
        grade_record = last_round.get("code_checks", {}).get("grade", {})
        grade = grade_record.get("grade", "NO_CLEAR_CAUSE")
        snapshot["grade"] = grade
        if evidence_window_policy == "event_time_v1":
            snapshot["outcome"] = (
                "ANSWERED" if grade in {"CONFIRMED", "REPORTED"}
                else "TIER_ONLY" if grade in {"ORDINARY", "MOVED_WITH_GROUP"}
                else "ABSTAINED"
            )
        attributions = [source for source in sources if "ATTRIBUTION" in source.get("roles", ())]
        events = [source for source in sources if "EVENT" in source.get("roles", ())]

        def best(items: list[dict[str, Any]], *, evidence_id: str | None = None, quote_text: str | None = None):
            if evidence_id is None or quote_text is None:
                return None
            exact = [source for source in items if source.get("evidence_id") == evidence_id and source.get("text") == quote_text]
            if not exact:
                return None
            items = exact
            return max(items, key=lambda source: (
                source.get("source_class") in _PRIMARY_SOURCE_CLASSES,
                len(source.get("text", "")),
            ), default=None)

        primary_source = best(attributions, evidence_id=grade_record.get("key_evidence_id"), quote_text=grade_record.get("key_quote_text"))
        if primary_source is None and evidence_window_policy == "event_time_v2":
            primary_source = best(sources, evidence_id=grade_record.get("key_evidence_id"), quote_text=grade_record.get("key_quote_text"))
            if primary_source is None:
                primary_source = next((source for source in sources if source.get("role") == "PRIMARY"), None)
        event_source = best(events, evidence_id=grade_record.get("event_evidence_id"), quote_text=grade_record.get("event_quote_text"))
        def role_projection(source: dict[str, Any] | None) -> dict[str, Any] | None:
            if source is None:
                return None
            return {field: source.get(field) for field in (
                "text", "publisher", "publisher_family", "origin", "published_at", "event_time",
                "r4b_path", "url", "evidence_id", "roles", "body_source",
                "release_session", "release_time_et",
            )}

        snapshot["key_sentence"] = role_projection(primary_source)
        snapshot["event_sentence"] = role_projection(event_source)
        unique_families = []
        seen_texts = set()
        for source in attributions:
            text_key = normalize_text(source["text"]).casefold()
            if text_key in seen_texts:
                continue
            seen_texts.add(text_key)
            family = source.get("publisher_family")
            publisher = source.get("publisher")
            if family and not any(existing["publisher_family"] == family for existing in unique_families):
                unique_families.append({"publisher": publisher, "publisher_family": family})
        if grade == "REPORTED":
            publishers = [item["publisher"].rstrip(". ") for item in unique_families if item.get("publisher")]
            snapshot["grade_label"] = (
                "Reported by " + (" and ".join(publishers) if publishers else "one publisher") + "."
            )
            if len(unique_families) >= 2:
                snapshot["reported_by"] = unique_families
        elif grade == "CONFIRMED":
            snapshot["grade_label"] = "Confirmed by attribution and event evidence."
        elif grade == "NO_CLEAR_CAUSE":
            snapshot["grade_label"] = "No clear cause was established."
        elif grade == "SUPPORTED":
            snapshot["grade_label"] = "Verifier supported; code could not establish an attribution role."
        elif grade == "ORDINARY":
            snapshot["grade_label"] = "Ordinary close."
        elif grade == "MOVED_WITH_GROUP":
            snapshot["grade_label"] = "Moved with the governing reference."
    if not answered:
        snapshot["abstention"] = abstention_block(decision_trace, assessment, abstention_context)
    return snapshot


def abstention_block(decision_trace: dict[str, Any], assessment: Any, abstention_context: dict[str, Any]) -> dict[str, Any]:
    code, reason = _abstention_reason(decision_trace, assessment)
    details = decision_trace.get("abstention_reason")
    if not isinstance(details, dict) or details.get("reason_code") != code:
        details = derive_abstention_reason(decision_trace)
    return {
        "reason_code": code,
        "reason_text": reason,
        "details": details["details"] if details else {},
        "possible_explanation": (abstention_context or {}).get("possible_explanation"),
        "leads": (abstention_context or {}).get("leads", []),
    }


_WEB_SOURCE_SUPPORTED_LIMITATION = (
    "Single user-selected web source; not corroborated by a second independent or primary source."
)
_WEB_SOURCE_TITLE_LIMITATION = (
    "The selected web source did not meet the company-news title and quote requirements."
)
_WEB_SOURCE_CEILING_CODES = {
    "MISSING_PRIMARY_CONFIRMATION",
    "MISSING_INDEPENDENT_CORROBORATION",
}


def classify_web_source_snapshot(
    snapshot: dict[str, Any],
    decision_trace: dict[str, Any],
    evidence_inventory: Any,
    *,
    web_source: bool = True,
    filter_removed_all: bool = False,
    evidence_window_policy: str = "pre_close_v1",
) -> dict[str, Any]:
    """Project the narrow supported-quote state for a one-page source run."""
    if not web_source:
        return snapshot
    if evidence_window_policy in ("event_time_v1", "event_time_v2") and snapshot.get("outcome") == "WEB_SOURCE_SUPPORTED":
        snapshot["grade"] = "REPORTED"
        snapshot["grade_label"] = "Reported by one user-selected web source."
        return snapshot
    if snapshot.get("outcome") != "ABSTAINED":
        return snapshot

    from copy import deepcopy
    from urllib.parse import urlsplit

    result = deepcopy(snapshot)
    rounds = decision_trace.get("rounds", ())
    current = rounds[-1] if rounds else {}
    reported = current.get("model_reported", {})
    analyst = reported.get("analyst", {})
    hypotheses = analyst.get("hypotheses", ())
    primary_refs = {
        item.get("ref") for item in hypotheses
        if item.get("proposed_role") == "PRIMARY"
        and item.get("cause_type") == "COMPANY_SPECIFIC_CATALYST"
    }
    checks = current.get("code_checks", {})
    quote = next((item for item in checks.get("quotes", ())
                  if item.get("kept") and item.get("hypothesis_ref") in primary_refs), None)
    gates = checks.get("gates", ())
    failed_gate = next((item for item in gates if item.get("result") != "no_change"), None)
    verifier = reported.get("verifier", {})

    def fail(code: str, *, details: dict[str, Any] | None = None,
             title_mismatch: bool = False) -> dict[str, Any]:
        result["outcome"] = "ABSTAINED"
        prior = result.get("abstention") or {}
        result["abstention"] = {
            **prior,
            "reason_code": code,
            "reason_text": abstention_reason_text(code),
            "details": {**prior.get("details", {}), **(details or {})},
        }
        limitations = list(result.get("limitations", ()))
        if title_mismatch and _WEB_SOURCE_TITLE_LIMITATION not in limitations:
            limitations.append(_WEB_SOURCE_TITLE_LIMITATION)
        result["limitations"] = limitations
        return result

    if not primary_refs:
        return fail("NO_PRIMARY_PROPOSED", details={"web_source_failure": "no_issuer_primary"},
                    title_mismatch=filter_removed_all)
    if quote is None:
        reason = derive_abstention_reason(decision_trace)
        return fail("PRIMARY_QUOTES_FAILED", details={
            **(reason.get("details", {}) if reason else {}),
            "web_source_failure": "quotes_failed",
        })
    if failed_gate is not None or not gates:
        gate_names = sorted({item.get("gate", "unknown") for item in gates
                             if item.get("result") != "no_change"})
        return fail("GATE_FAILED", details={"failed_gate_names": gate_names or ["unknown"]})
    if not verifier.get("called") or verifier.get("verdict") != "SUPPORTED":
        if not verifier.get("called"):
            return fail("VERIFIER_NO_VERDICT")
        details = {"verifier_reason": verifier.get("category") or "OTHER"}
        if isinstance(verifier.get("reason"), str) and verifier["reason"]:
            details["verifier_note"] = verifier["reason"]
        return fail("VERIFIER_REJECTED", details=details)
    if result.get("status") != "PARTIAL":
        return fail("CEILING_ABSTAIN", details={"web_source_failure": "non_ceiling_abstention"})

    limitations = list(result.get("limitations", ()))
    ceiling_codes = {
        code for limitation in limitations
        for code in _WEB_SOURCE_CEILING_CODES
        if code in limitation
    }
    if not ceiling_codes:
        return fail("CEILING_ABSTAIN", details={"web_source_failure": "non_ceiling_abstention"})

    inventory = {getattr(item, "evidence_id", None): item for item in evidence_inventory}
    item = inventory.get(quote.get("evidence_id"))
    if item is None:
        return fail("PRIMARY_QUOTES_FAILED", details={"web_source_failure": "missing_evidence"})
    source = _quote(item, quote.get("text", ""), "PRIMARY")
    source["domain"] = urlsplit(source.get("url") or "").hostname
    result["outcome"] = "WEB_SOURCE_SUPPORTED"
    result["key_sentence"] = {
        field: source[field]
        for field in ("text", "publisher", "published_at", "url", "evidence_id")
    }
    result["also_sentence"] = None
    result["sources"] = [source]
    result["limitations"] = [
        _WEB_SOURCE_SUPPORTED_LIMITATION,
        *(
            limitation for limitation in limitations
            if not any(prefix in limitation for prefix in ABSTAIN_LIMITATION_PREFIXES)
            and not any(code in limitation for code in _WEB_SOURCE_CEILING_CODES)
        ),
    ]
    result.pop("abstention", None)
    return result


__all__ = ["ABSTENTION_REASON_CODES", "AnswerGrade", "abstention_block", "abstention_reason_text", "build_answer_snapshot", "build_facts_snapshot", "derive_abstention_reason", "possible_explanation_quotes", "classify_web_source_snapshot", "grade_event_time_hypothesis", "grade_event_time_v2_hypothesis"]
