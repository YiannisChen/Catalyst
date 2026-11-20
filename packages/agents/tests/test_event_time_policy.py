import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    EventTimeAnalystDecision,
    EventTimeSourceQuote,
    SourceQuote,
)
from catalyst_agents.attribution.policy import (
    AttributionPolicy,
    resolve_attribution_policy,
)
from catalyst_agents.attribution.quotes import apply_quote_rules, check_quote
from catalyst_agents.observability.decision_trace import DecisionTrace
from catalyst_data.canonical.temporal import (
    TemporalIdentity,
    build_temporal_identity,
    with_event_time_publication_window,
)

UTC = timezone.utc


def _dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _temporal(session_date="2026-01-15"):
    return with_event_time_publication_window(
        build_temporal_identity(session_date), now=_dt("2026-10-10T12:00:00Z")
    )


def _check(sentence, *, published="2026-01-16T15:00:00Z", source="reported_news",
           metadata=None, profile=None, role="PRIMARY", excerpt=None, policy="event_time_v1"):
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT", eligible_at=_dt(published),
        excerpt_text=excerpt or sentence, source_class=source,
    )
    return check_quote(
        sentence, "e1", supporting_ids={"e1"}, items={"e1": item},
        temporal_identity=_temporal(metadata.get("_session_date", "2026-01-15") if metadata else "2026-01-15"), evidence_window_policy=policy,
        quote_metadata=metadata or {}, hypothesis_role=role, move_profile=profile,
    )


def test_event_time_v2_missing_event_time_is_unverified_not_blocking():
    sentence = "Shares of X fell 4% after the company cut its outlook sharply."
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT", eligible_at=_dt("2026-01-15T22:00:00Z"),
        excerpt_text=sentence, source_class="reported_news",
    )
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=("e1",),
        temporal_identity=_temporal(), observation=SimpleNamespace(ticker="X"),
    )
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": ["e1"], "quotes": [{"evidence_id": "e1", "text": sentence}],
    }]}
    _, (check,) = apply_quote_rules(payload, pack, evidence_window_policy="event_time_v2")
    assert check.rule == "OK"
    assert check.r4b == "PASS"
    assert check.r4b_path == "unverified"
    assert check.roles == ("ATTRIBUTION",)


def test_event_time_v2_keeps_verified_primary_source_event_role():
    sentence = "Shares of X fell after the company cut its outlook sharply."
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT", eligible_at=_dt("2026-01-16T15:00:00Z"),
        source_time_at=_dt("2026-01-15T18:00:00Z"), excerpt_text=sentence,
        source_class="issuer_disclosure",
    )
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=("e1",),
        temporal_identity=_temporal(), observation=SimpleNamespace(ticker="X"),
    )
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": ["e1"], "quotes": [{"evidence_id": "e1", "text": sentence}],
    }]}
    _, (check,) = apply_quote_rules(payload, pack, evidence_window_policy="event_time_v2")
    assert check.rule == "OK"
    assert check.r4b_path == "primary_source_time"
    assert "EVENT" in check.roles


def test_event_time_v2_explicit_after_deadline_and_after_close_still_fail():
    late = "Shares of X fell 4% after the company cut its outlook at 5 p.m. ET."
    assert _check(late, published="2026-01-15T22:00:00Z", policy="event_time_v2",
                  metadata={"event_time_quote": "cut its outlook at 5 p.m. ET", "event_relation": "INTRADAY",
                            "event_date": "2026-01-15"}) == "R4b_EVENT_TIME_CONTRADICTION"
    after_close = "Shares of X rose 4% after the company reported earnings after the close."
    assert _check(after_close, published="2026-01-15T22:00:00Z", policy="event_time_v2",
                  metadata={"event_time_quote": "reported earnings after the close", "event_relation": "AFTER_CLOSE",
                            "event_date": "2026-01-15"}) == "R4b_EVENT_TIME_CONTRADICTION"


def test_event_time_v2_gap_day_explicit_post_open_still_fails():
    sentence = "Shares of X fell 4% after the company cut its outlook at 10 a.m. ET."
    profile = SimpleNamespace(ticker="X", gap_return=-4.0, target_return=-4.5)
    assert _check(sentence, published="2026-01-15T22:00:00Z", policy="event_time_v2",
                  profile=profile) == "R4b_EVENT_TIME_CONTRADICTION"
    assert _check(sentence, published="2026-01-15T22:00:00Z", policy="event_time_v2",
                  profile=profile, metadata={"event_time_quote": "cut its outlook at 10 a.m. ET",
                                             "event_relation": "INTRADAY", "event_date": "2026-01-15"}) == "R4b_EVENT_TIME_CONTRADICTION"


def test_event_time_v2_future_preview_still_fails():
    sentence = "Shares of X rose after the company will report earnings tomorrow."
    assert _check(sentence, published="2026-01-15T22:00:00Z", policy="event_time_v2") == "R4b_EVENT_TIME_CONTRADICTION"


def test_event_time_v2_keeps_publication_relative_r6_and_primary_r8_blocking():
    assert _check("Shares of X fell Wednesday after UBS cut its target.",
                  published="2026-01-15T22:00:00Z", policy="event_time_v2") == "R6_OTHER_SESSION_MOVE"
    assert _check("X slid amid broader AI worries.",
                  published="2026-01-15T20:00:00Z", policy="event_time_v2") == "R8_HEDGED_LINK"


def test_default_publication_window_stays_at_session_close():
    sentence = "Shares of X fell after the company cut its outlook sharply."
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT", eligible_at=_dt("2026-01-16T15:00:00Z"),
        excerpt_text=sentence, source_class="reported_news",
    )
    old_identity = TemporalIdentity.model_validate({
        key: value for key, value in _temporal().model_dump(mode="python").items()
        if key not in {"evidence_window_start_at", "evidence_window_end_at"}
    })
    assert check_quote(sentence, "e1", supporting_ids={"e1"}, items={"e1": item}, temporal_identity=old_identity) == "R4_OUTSIDE_SESSION_WINDOW"
    assert "evidence_window_policy" not in AttributionPolicy(
        verifier_enabled=True, corrective_enabled=False, quote_rules_version="q1", evidence_window_policy="pre_close_v1"
    ).model_dump(mode="json")
    assert "evidence_window_start_at" not in old_identity.model_dump(mode="json")


def test_v2_rules_policy_serialization_is_omitted_when_off_and_canonical_when_on():
    default = resolve_attribution_policy({}, evidence_window_policy="pre_close_v1", web_evidence="off")
    desk = resolve_attribution_policy({})
    # Literal pre-v2_rules policy bytes: omission preserves hashes of existing runs.
    assert default.model_dump_json() == (
        '{"verifier_enabled":true,"verifier_version":"primary_verifier_v7",'
        '"verifier_prompt_sha256":"' + default.verifier_prompt_sha256 + '",'
        '"corrective_enabled":false,"quote_rules_version":"q1c_quote_rules_v4",'
        '"force_full":false}'
    )
    assert desk.model_dump_json() == (
        '{"verifier_enabled":true,"verifier_version":"primary_verifier_v7",'
        f'"verifier_prompt_sha256":"{desk.verifier_prompt_sha256}",'
        '"corrective_enabled":false,"quote_rules_version":"q1c_quote_rules_desk_cascade_v1",'
        '"force_full":false,"evidence_window_policy":"desk_cascade_v1",'
        '"web_evidence":"discovery_v2",'
        '"stage1_verifier_version":"primary_verifier_v7",'
        f'"stage1_verifier_prompt_sha256":"{desk.stage1_verifier_prompt_sha256}",'
        '"stage2_verifier_version":"primary_verifier_event_time_p1",'
        f'"stage2_verifier_prompt_sha256":"{desk.stage2_verifier_prompt_sha256}",'
        f'"desk_context_version":"{desk.desk_context_version}"}}'
    )
    resolved = resolve_attribution_policy({"CATALYST_V2_RULES": "filing,recency,window"})
    assert resolved.v2_rules == "recency,window,filing"
    assert resolved.model_dump(mode="json")["v2_rules"] == "recency,window,filing"
    explicit = resolve_attribution_policy({"CATALYST_V2_RULES": "all"}, v2_rules="anchor,recency")
    assert explicit.v2_rules == "recency,anchor"


def test_v2_rule_errors_are_reused_and_non_desk_rules_remain_supported():
    with pytest.raises(ValueError, match="unknown v2 rules"):
        resolve_attribution_policy({}, v2_rules="bogus")
    with pytest.raises(ValueError, match="earnings anchor"):
        resolve_attribution_policy({}, v2_rules="anchor")
    policy = resolve_attribution_policy({}, evidence_window_policy="pre_close_v1", v2_rules="filing")
    assert policy.v2_rules == "filing"


def test_event_time_v2_policy_uses_event_time_prompt_without_legacy_web_mode():
    policy = resolve_attribution_policy({}, evidence_window_policy="event_time_v2", web_evidence="off")
    assert policy.evidence_window_policy == "event_time_v2"
    assert policy.verifier_version == "primary_verifier_event_time_v2"
    assert policy.quote_rules_version == "q1c_quote_rules_event_time_v2"
    with pytest.raises(ValueError, match="unsupported web evidence policy"):
        resolve_attribution_policy(
            {}, evidence_window_policy="event_time_v2", web_evidence="cache_code3_why_roundups"
        )


def test_event_time_p1_verifier_is_p_plus_the_publication_note():
    from catalyst_agents.attribution.verifier import (
        EVENT_TIME_P1_PROMPT,
        EVENT_TIME_P1_VERIFIER_VERSION,
        PROMPT,
        build_prompt,
    )

    policy = resolve_attribution_policy({}, evidence_window_policy="event_time_p1", web_evidence="off")
    assert policy.evidence_window_policy == "event_time_p1"
    assert policy.verifier_version == EVENT_TIME_P1_VERIFIER_VERSION
    assert policy.verifier_prompt_sha256 == hashlib.sha256(EVENT_TIME_P1_PROMPT.encode()).hexdigest()
    assert policy.quote_rules_version == "q1c_quote_rules_event_time_p1"
    with pytest.raises(ValueError, match="requires desk_cascade_v1"):
        resolve_attribution_policy(
            {}, evidence_window_policy="event_time_p1", web_evidence="discovery_v2"
        )
    # P's question and criteria are kept verbatim; only the publication-time note is added.
    assert PROMPT.split("Question:", 1)[1] == EVENT_TIME_P1_PROMPT.split("Question:", 1)[1]
    context = SimpleNamespace(
        observation=SimpleNamespace(target_return=1.0, ticker="X"),
        temporal_identity=_temporal(),
    )
    p1 = build_prompt("primary", context, evidence_window_policy="event_time_p1")
    assert "Judge the session a quote describes, not its publication time" in p1
    assert "Judge the session" not in build_prompt("primary", context, evidence_window_policy="pre_close_v1")


def test_event_time_p1_quote_rules_use_publication_window_and_v2_contradiction_only():
    recap = "Shares of X rose after the company announced a new factory."
    assert _check(recap, published="2026-01-16T15:00:00Z", policy="event_time_p1") == "OK"
    # 2026-01-19 is a market holiday, so D+3 trading days from Friday 01-16 is Thursday 01-22.
    assert _check(recap, published="2026-01-26T15:00:00Z", policy="event_time_p1") == "R4a_OUTSIDE_PUBLICATION_WINDOW"
    contradiction = "Shares of X rose after the company reported earnings after the close."
    assert _check(contradiction, published="2026-01-15T22:00:00Z", policy="event_time_p1") == "R4b_EVENT_TIME_CONTRADICTION"
    assert _check("X slid amid broader AI worries.", published="2026-01-15T20:00:00Z",
                  policy="event_time_p1", role="PRIMARY") == "OK"
    assert _check("Shares of X fell Wednesday after UBS cut its target.",
                  published="2026-01-15T22:00:00Z", policy="event_time_p1") == "R6_OTHER_SESSION_MOVE"


def test_event_time_p1_quote_audit_records_but_does_not_apply_r8():
    sentence = "X slid amid broader AI worries."
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT", eligible_at=_dt("2026-01-15T20:00:00Z"),
        excerpt_text=sentence, source_class="reported_news", publisher="Example",
        canonical_url="https://example.com/article",
    )
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=("e1",),
        temporal_identity=_temporal(), observation=SimpleNamespace(ticker="X"),
    )
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": ["e1"], "quotes": [{"evidence_id": "e1", "text": sentence}],
    }]}
    _, (check,) = apply_quote_rules(payload, pack, evidence_window_policy="event_time_p1")
    assert check.rule == "OK"
    assert check.r4a == "PASS"
    assert check.r4b == "PASS"
    assert check.r8 == "FAIL"


def test_pre_deadline_publication_path_passes_event_time():
    sentence = "X fell after UBS cut its price target to $100."
    assert _check(sentence, published="2026-01-15T20:00:00Z") == "OK"


def test_post_deadline_event_time_quote_path_passes_relative_yesterday():
    sentence = "Shares of X fell 4% yesterday after the company cut its outlook."
    event_phrase = "yesterday after the company cut its outlook"
    assert _check(sentence, metadata={
        "event_time_quote": event_phrase, "event_relation": "INTRADAY", "event_date": "2026-01-15",
    }) == "OK"


def test_post_deadline_without_same_excerpt_event_quote_fails_r4b():
    sentence = "Shares of X fell 4% after the company cut its outlook sharply."
    assert _check(sentence, metadata={
        "event_time_quote": "Management changed its expectations on Wednesday.", "event_relation": "INTRADAY", "event_date": "2026-01-15",
        "unused": "not in source",
    }, excerpt=sentence) == "R4b_EVENT_TIME_UNVERIFIED"


def test_post_deadline_textbook_attribution_passes_r4b_without_becoming_event():
    sentence = "Shares of global payments technology company Visa (NYSE:V) jumped 4.2% in the morning session after BofA Securities upgraded the stock to Buy from Neutral."
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT", eligible_at=_dt("2026-01-15T22:00:00Z"),
        excerpt_text=sentence, source_class="reported_news",
    )
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=("e1",),
        temporal_identity=_temporal(), observation=SimpleNamespace(ticker="V"),
    )
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": ["e1"],
        "quotes": [{"evidence_id": "e1", "text": sentence}],
    }]}

    normalized, (check,) = apply_quote_rules(
        payload, pack, evidence_window_policy="event_time_v1"
    )

    assert check.rule == "OK"
    assert check.r4b_path == "attribution_implied"
    assert check.roles == ("ATTRIBUTION",)
    assert normalized["candidate_hypotheses"][0]["quotes"][0]["text"] == sentence


def test_unknown_event_time_metadata_allows_attribution_but_after_close_does_not():
    sentence = "Shares of X rose 4% in the morning session after the company reported earnings today."
    assert _check(sentence, published="2026-01-15T22:00:00Z", profile=SimpleNamespace(ticker="X"), metadata={
        "event_time_quote": "the company reported earnings today",
        "event_relation": "UNKNOWN", "event_date": "2026-01-15",
    }) == "OK"
    after_close = "Shares of X rose 4% in the morning session after the company reported earnings after the close."
    assert _check(after_close, published="2026-01-15T22:00:00Z", profile=SimpleNamespace(ticker="X"), metadata={
        "event_time_quote": "the company reported earnings after the close",
        "event_relation": "AFTER_CLOSE", "event_date": "2026-01-15",
    }) == "R4b_EVENT_TIME_UNVERIFIED"


def test_textbook_attributions_resolve_the_move_to_session_d():
    cases = (
        ("V", "2025-12-11",
         "Shares of global payments technology company Visa (NYSE:V) jumped 4.2% in the morning session after BofA Securities upgraded the stock to Buy from Neutral."),
        ("CMCSA", "2026-04-23",
         "Shares of telecommunications and media company Comcast (NASDAQ:CMCSA) jumped 8.9% in the afternoon session after the company reported first-quarter 2026 financial results that surpassed Wall Street's expectations for both revenue and profit."),
        ("CAT", "2026-04-30",
         "Caterpillar ( NYSE:CAT ) shares moved sharply higher after the company delivered a stronger-than-expected first quarter."),
        ("BA", "2026-05-15",
         "The announcement sent Boeing shares down more than 4%, as investors had expected a much larger order."),
    )
    for ticker, session_date, sentence in cases:
        item = SimpleNamespace(
            evidence_id="e1", content_state="FULL_TEXT",
            eligible_at=_dt(
                "2026-04-24T07:41:23Z" if ticker == "CMCSA"
                else f"{session_date}T22:00:00Z"
            ), excerpt_text=sentence,
            source_class="reported_news",
        )
        temporal = _temporal(session_date)
        pack = SimpleNamespace(
            evidence_inventory=(item,), included_evidence_ids=("e1",),
            temporal_identity=temporal, observation=SimpleNamespace(ticker=ticker),
        )
        payload = {"candidate_hypotheses": [{
            "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
            "supporting_evidence_ids": ["e1"],
            "quotes": [{"evidence_id": "e1", "text": sentence}],
        }]}

        _, (check,) = apply_quote_rules(
            payload, pack, evidence_window_policy="event_time_v1"
        )
        assert check.rule == "OK", (ticker, check.rule)
        assert check.r4b_path == "attribution_implied", ticker
        assert check.roles == ("ATTRIBUTION",), ticker


def test_gap_day_afternoon_move_attribution_passes_but_explicit_intraday_event_does_not():
    sentence = "Shares of Comcast (NASDAQ:CMCSA) jumped 8.9% in the afternoon session after the company reported its first-quarter results."
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT",
        eligible_at=_dt("2026-04-23T18:00:00Z"), excerpt_text=sentence,
        source_class="reported_news",
    )
    temporal = _temporal("2026-04-23")
    observation = SimpleNamespace(ticker="CMCSA", gap_return=8.0, target_return=8.9)
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=("e1",),
        temporal_identity=temporal, observation=observation,
    )

    def quote(metadata):
        payload = {"candidate_hypotheses": [{
            "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
            "supporting_evidence_ids": ["e1"],
            "quotes": [{"evidence_id": "e1", "text": sentence, **metadata}],
        }]}
        return apply_quote_rules(payload, pack, evidence_window_policy="event_time_v1")[1][0]

    implied = quote({})
    assert (implied.rule, implied.r4b_path, implied.roles) == (
        "OK", "attribution_implied", ("ATTRIBUTION",),
    )
    explicit_after_open = quote({
        "event_time_quote": "the company reported its first-quarter results during trading",
        "event_relation": "INTRADAY", "event_date": "2026-04-23",
    })
    assert explicit_after_open.rule == "R4b_EVENT_TIME_UNVERIFIED"


def test_session_reference_does_not_resolve_beyond_previous_day():
    sentence = "Shares of Comcast (NASDAQ:CMCSA) jumped 8.9% in the afternoon session after the company reported first-quarter results."
    assert _check(
        sentence, published="2026-04-25T07:00:00Z",
        metadata={"_session_date": "2026-04-23"},
    ) == "R4b_EVENT_TIME_UNVERIFIED"


def test_strong_attribution_links_cover_causal_constructions_and_reject_hedges():
    from catalyst_agents.attribution.quotes import (
        _hedged_link_without_strong_link,
        classify_evidence_roles,
    )

    def roles(sentence, ticker="X"):
        item = SimpleNamespace(eligible_at=_dt("2026-01-15T20:00:00Z"))
        return classify_evidence_roles(
            sentence, item, ticker=ticker, session_date="2026-01-15",
            move_date_is_session=True, r4b_path=None,
        ).roles

    positives = (
        ("A surprise earnings report sent X shares down 4%.", "X"),
        ("X shares rose 5% on news that the company won a major contract.", "X"),
        ("X stock gained 3% following the company’s acquisition announcement.", "X"),
        ("X shares jumped 6% in reaction to its earnings upgrade.", "X"),
        ("X shares rose as investors cheered the company’s approved product launch.", "X"),
    )
    for sentence, ticker in positives:
        assert "ATTRIBUTION" in roles(sentence, ticker), sentence

    negatives = (
        ("The rumor sent X shares down amid worries about demand.", "X"),
        ("X shares fell as investors weighed the outlook.", "X"),
    )
    for sentence, ticker in negatives:
        assert "ATTRIBUTION" not in roles(sentence, ticker), sentence
    assert _hedged_link_without_strong_link(
        "X shares fell on easing worries that demand would weaken."
    )


def test_wrong_weekday_is_rejected_by_publication_relative_r6():
    sentence = "Shares of X fell 4% on Friday after the company cut its outlook."
    assert _check(sentence, metadata={
        "event_time_quote": "on Friday after the company cut its outlook", "event_relation": "INTRADAY", "event_date": "2026-01-16",
    }) == "R6_OTHER_SESSION_MOVE"


def test_event_time_rejects_after_close_and_gap_day_intraday():
    sentence = "Shares of X fell 4% after executives cut the outlook during trading."
    assert _check(sentence, metadata={
        "event_time_quote": "executives cut the outlook during trading", "event_relation": "AFTER_CLOSE", "event_date": "2026-01-15",
    }) == "R4b_EVENT_TIME_UNVERIFIED"


def test_event_date_consistency_uses_complete_cited_sentence():
    sentence = "Shares of X fell 4% yesterday after the company cut its outlook."
    assert _check(sentence, metadata={
        "event_time_quote": "after the company cut its outlook",
        "event_relation": "PRIOR_DAY", "event_date": "2026-01-14",
    }) == "R4b_EVENT_TIME_UNVERIFIED"
    profile = SimpleNamespace(gap_return=-4.0, target_return=-5.0)
    assert _check(sentence, metadata={
        "event_time_quote": "executives cut the outlook during trading", "event_relation": "INTRADAY", "event_date": "2026-01-15",
    }, profile=profile) == "R4b_EVENT_TIME_UNVERIFIED"


def test_event_time_prior_day_relation_and_official_source_time_paths():
    sentence = "Shares of X fell after the company cut its outlook on January 14."
    assert _check(sentence, metadata={
        "event_time_quote": "the company cut its outlook on January 14", "event_relation": "PRIOR_DAY", "event_date": "2026-01-14",
    }) == "OK"
    assert _check(sentence, published="2026-01-15T20:00:00Z", source="sec") == "OK"


def test_official_source_owned_time_before_deadline_passes_after_publication():
    sentence = "Shares of X fell after the company cut its outlook sharply."
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT", eligible_at=_dt("2026-01-16T15:00:00Z"),
        source_time_at=_dt("2026-01-15T20:00:00Z"), excerpt_text=sentence,
        source_class="issuer_disclosure",
    )
    assert check_quote(
        sentence, "e1", supporting_ids={"e1"}, items={"e1": item},
        temporal_identity=_temporal(), evidence_window_policy="event_time_v1",
    ) == "OK"


def test_event_time_trace_records_r4a_r4b_path_and_r8_independently():
    sentence = "Shares of X fell 4% yesterday after the company cut its outlook."
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT", eligible_at=_dt("2026-01-16T15:00:00Z"),
        excerpt_text=sentence, source_class="reported_news",
    )
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=("e1",),
        temporal_identity=_temporal(), observation=None,
    )
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": ["e1"],
        "quotes": [{"evidence_id": "e1", "text": sentence,
                    "event_time_quote": "yesterday after the company cut its outlook",
                    "event_relation": "INTRADAY", "event_date": "2026-01-15"}],
    }]}
    _, (check,) = apply_quote_rules(payload, pack, evidence_window_policy="event_time_v1")
    assert (check.r4a, check.r4b, check.r4b_path, check.r8) == (
        "PASS", "PASS", "extracted_event_time", "PASS",
    )


def test_event_time_trace_records_predeadline_and_official_source_paths():
    pre_sentence = "X fell after UBS cut its price target to $100."
    official_sentence = "Shares of X fell after the company cut its outlook sharply."
    pre_item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT", eligible_at=_dt("2026-01-15T20:00:00Z"),
        excerpt_text=pre_sentence, source_class="reported_news",
    )
    official_item = SimpleNamespace(
        evidence_id="e2", content_state="FULL_TEXT", eligible_at=_dt("2026-01-16T15:00:00Z"),
        source_time_at=_dt("2026-01-15T20:00:00Z"), excerpt_text=official_sentence,
        source_class="issuer_disclosure",
    )
    pack = SimpleNamespace(
        evidence_inventory=(pre_item, official_item), included_evidence_ids=("e1", "e2"),
        temporal_identity=_temporal(), observation=None,
    )
    payload = {"candidate_hypotheses": [
        {"hypothesis_ref": "h1", "proposed_role": "PRIMARY", "supporting_evidence_ids": ["e1"],
         "quotes": [{"evidence_id": "e1", "text": pre_sentence}]},
        {"hypothesis_ref": "h2", "proposed_role": "SECONDARY", "supporting_evidence_ids": ["e2"],
         "quotes": [{"evidence_id": "e2", "text": official_sentence}]},
    ]}
    _, checks = apply_quote_rules(payload, pack, evidence_window_policy="event_time_v1")
    assert [check.r4b_path for check in checks] == [
        "pre_deadline_publication", "primary_source_time",
    ]


def test_event_time_publication_outside_d3_window_fails_r4a():
    sentence = "Shares of X fell after the company cut its outlook sharply."
    assert _check(sentence, published="2026-01-22T15:00:00Z", metadata={
        "event_time_quote": "the company cut its outlook", "event_relation": "INTRADAY", "event_date": "2026-01-15",
    }) == "R4a_OUTSIDE_PUBLICATION_WINDOW"


def test_r8_rejects_only_hedged_primary_link_and_accepts_strong_link():
    assert _check("X slid amid broader AI worries.", published="2026-01-15T20:00:00Z") == "R8_HEDGED_LINK"
    assert _check("X fell after UBS cut its price target to $100.", published="2026-01-15T20:00:00Z") == "OK"
    assert _check("X slid amid broader AI worries.", role="SECONDARY", published="2026-01-15T20:00:00Z") == "OK"


def test_real_v5_trace_lines_cover_prior_session_and_class_c_cases():
    # Verbatim lines read from v5_r1m/v5_r2m/v5_r3m decision_trace artifacts.
    x58 = "The payments and fintech sector is catching a meaningful bid on Monday, with three of its most actively traded names posting solid gains."
    assert _check(x58, published="2026-04-14T15:00:00Z", metadata={
        "event_time_quote": "catching a meaningful bid on Monday",
        "event_relation": "INTRADAY", "event_date": "2026-04-13",
        "_session_date": "2026-04-13",
    }) == "OK"

    x25 = "Recently, AT&T reported earnings that showed a deterioration in its free cash."
    assert _check(x25, published="2026-04-28T15:00:00Z", metadata={
        "event_time_quote": "AT&T reported earnings", "event_relation": "PRIOR_DAY",
        "event_date": "2026-04-22", "_session_date": "2026-04-27",
    }) == "R4b_EVENT_TIME_UNVERIFIED"

    x60 = "Shares are moving lower in extended trading after the company cut its full-year outlook, citing supply chain constraints."
    assert _check(x60, published="2026-08-07T15:00:00Z", metadata={
        "event_time_quote": "the company cut its full-year outlook",
        "event_relation": "AFTER_CLOSE", "event_date": "2026-08-06",
        "_session_date": "2026-08-06",
    }) == "R4b_EVENT_TIME_UNVERIFIED"

    x29 = "Target’s summer sale was moved up by a month compared to last year, likely to compete with Amazon’s sale ."
    assert _check(x29, published="2026-06-24T20:00:00Z", role="PRIMARY",
                  metadata={"_session_date": "2026-06-24"}) == "R8_HEDGED_LINK"


def test_quote_trace_extensions_are_absent_for_legacy_check():
    from catalyst_agents.attribution.quotes import QuoteCheck
    trace = DecisionTrace("run:test")
    trace.quote_checks(1, (QuoteCheck("h", "e", "q", True, "OK"),))
    assert set(trace.payload["rounds"][0]["code_checks"]["quotes"][0]) == {
        "hypothesis_ref", "evidence_id", "kept", "rule", "text"
    }


def test_event_time_fields_are_confined_to_the_opt_in_model_schema():
    assert set(SourceQuote.model_fields) == {"evidence_id", "text"}
    assert "event_time_quote" not in AnalystDecision.model_json_schema()["$defs"]["SourceQuote"]["properties"]
    assert "event_time_quote" in EventTimeSourceQuote.model_json_schema()["properties"]
    assert "event_time_quote" in EventTimeAnalystDecision.model_json_schema()["$defs"]["EventTimeSourceQuote"]["properties"]


def test_event_time_rejects_invented_or_unanchored_timing():
    sentence = "Shares fell after the company cut its outlook at 3 pm."
    assert _check(sentence, metadata={
        "event_time_quote": "company cut its outlook at 3 pm",
        "event_relation": "BEFORE_OPEN", "event_date": "2026-01-15",
    }) == "R4b_EVENT_TIME_UNVERIFIED"
    assert _check("Shares fell after the company cut its outlook sharply.", metadata={
        "event_time_quote": "company cut its outlook", "event_relation": "INTRADAY",
        "event_date": "2026-01-15",
    }) == "R4b_EVENT_TIME_UNVERIFIED"


def test_preview_and_after_close_metadata_do_not_pass_pre_deadline_path():
    sentence = "The company said it will announce results tomorrow after close."
    assert _check(sentence, published="2026-01-15T15:00:00Z", metadata={
        "event_time_quote": "will announce results tomorrow after close",
        "event_relation": "INTRADAY", "event_date": "2026-01-15",
    }) == "R4b_EVENT_TIME_UNVERIFIED"
    contradictory = "Shares fell after the company cut its outlook at 3 pm ET."
    assert _check(contradictory, published="2026-01-15T15:00:00Z", metadata={
        "event_time_quote": "cut its outlook at 3 pm ET",
        "event_relation": "BEFORE_OPEN", "event_date": "2026-01-15",
    }) == "R4b_EVENT_TIME_UNVERIFIED"
    pacific = "Shares fell after the company cut its outlook at 3 pm PT."
    assert _check(pacific, published="2026-01-15T15:00:00Z", metadata={
        "event_time_quote": "cut its outlook at 3 pm PT",
        "event_relation": "BEFORE_OPEN", "event_date": "2026-01-15",
    }) == "R4b_EVENT_TIME_UNVERIFIED"


def test_event_time_keeps_explicit_and_all_relative_r6_mentions():
    assert _check("Shares fell 4% on January 14 after the company cut its outlook.", metadata={
        "event_time_quote": "company cut its outlook", "event_relation": "PRIOR_DAY",
        "event_date": "2026-01-14",
    }) == "R6_OTHER_SESSION_MOVE"
    assert _check("Shares fell 4% on Thursday and fell 2% on Wednesday.", metadata={
        "event_time_quote": "on Wednesday", "event_relation": "PRIOR_DAY",
        "event_date": "2026-01-14",
    }) == "R6_OTHER_SESSION_MOVE"
    assert _check("Shares fell yesterday after guidance was cut on Wednesday.",
                  published="2026-01-16T15:00:00Z", metadata={
        "event_time_quote": "guidance was cut on Wednesday", "event_relation": "PRIOR_DAY",
        "event_date": "2026-01-14",
    }) == "OK"


def test_r8_requires_causal_connector_and_concrete_event_without_month_false_positive():
    assert _check("Shares slid amid AI worries, traders said.", published="2026-01-15T20:00:00Z") == "R8_HEDGED_LINK"
    assert _check("Shares fell as concerns about demand mounted.", published="2026-01-15T20:00:00Z") == "R8_HEDGED_LINK"
    assert _check("Shares fell after UBS cut its target; UBS published the update in May.", published="2026-01-15T20:00:00Z") == "OK"
    assert _check("Shares fell amid the UBS cut, traders said.", published="2026-01-15T20:00:00Z") == "OK"
    assert _check("Shares may fall amid worries about demand.", published="2026-01-15T20:00:00Z") == "R8_HEDGED_LINK"


def test_gap_deadline_requires_matching_move_direction():
    sentence = "Shares rose after the company raised its outlook during trading."
    profile = SimpleNamespace(gap_return=-4.0, target_return=5.0)
    assert _check(sentence, published="2026-01-15T17:00:00Z", profile=profile, metadata={
        "event_time_quote": "company raised its outlook during trading",
        "event_relation": "INTRADAY", "event_date": "2026-01-15",
    }) == "OK"


def test_attribute_only_temporal_identity_remains_compatible_on_event_helpers():
    from catalyst_agents.attribution.answer_snapshot import build_facts_snapshot
    from catalyst_agents.attribution.assessment import _in_pack_failed_parse

    temporal = SimpleNamespace(
        session_date="2026-01-15", session_open_at=_dt("2026-01-15T14:30:00Z"),
        information_window_start_at=_dt("2026-01-14T21:00:00Z"),
        cutoff_at=_dt("2026-01-15T21:00:00Z"), evidence_window_start_at=None,
    )
    build_facts_snapshot(ticker="X", move_profile=SimpleNamespace(target_return=None, commodity_ticker=None,
                                                                    commodity_return=None), temporal_identity=temporal)
    item = SimpleNamespace(parse_quality="failed", content_state="FULL_TEXT",
                           material_capability="MATERIAL_CAPABLE", eligible_at=_dt("2026-01-15T20:00:00Z"))
    assert _in_pack_failed_parse({"e1": item}, SimpleNamespace(temporal_identity=temporal))
