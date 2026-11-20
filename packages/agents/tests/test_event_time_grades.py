from datetime import datetime, timezone
from types import SimpleNamespace

from catalyst_agents.attribution.answer_snapshot import (
    build_answer_snapshot,
    grade_event_time_hypothesis,
    grade_event_time_v2_hypothesis,
)
from catalyst_agents.attribution.quotes import (
    apply_quote_rules,
    classify_evidence_roles,
    publisher_family,
)


def _item(*, evidence_id, publisher, source_class="reported_news", url=None,
          published="2026-04-10T19:00:00+00:00", source_time=None):
    return SimpleNamespace(
        evidence_id=evidence_id,
        publisher=publisher,
        source_class=source_class,
        canonical_url=url,
        eligible_at=datetime.fromisoformat(published),
        source_time_at=(datetime.fromisoformat(source_time) if source_time else None),
    )


def test_real_pypl_sector_sentence_is_lead_not_attribution():
    # Real x58 excerpt from evt_smoke1/evt_smoke2, 24/7 Wall St.
    sentence = (
        "The payments and fintech sector is catching a meaningful bid on Monday, "
        "with three of its most actively traded names posting solid gains."
    )
    result = classify_evidence_roles(
        sentence,
        _item(evidence_id="web:pypl:article:web_body:0001", publisher="24/7 Wall St."),
        ticker="PYPL",
        session_date="2026-04-13",
        move_date_is_session=True,
        r4b_path="extracted_event_time",
    )
    assert result.roles == ("LEAD",)
    assert "ATTRIBUTION_SUBJECT_MISSING" in result.reasons["ATTRIBUTION"]


def test_real_motley_fool_question_is_lead():
    # Real x25 T excerpt from evt_smoke1; this is a hedged headline question.
    sentence = "Could this be a sign of trouble for its high dividend, and is a cut inevitable?"
    result = classify_evidence_roles(
        sentence,
        _item(evidence_id="web:t:fool:web_body:0001", publisher="The Motley Fool"),
        ticker="T",
        session_date="2026-04-23",
        move_date_is_session=False,
        r4b_path=None,
    )
    assert result.roles == ("LEAD",)


def test_real_now_ubs_downgrade_sentence_can_be_event_and_attribution():
    # Real x15 NOW quote from evt_smoke1, Benzinga.
    sentence = (
        "ServiceNow Inc (NYSE: NOW ) shares are sliding Friday after UBS analyst "
        "Karl Keirstead downgraded the stock from Buy to Neutral and sharply "
        "reduced his price target from $170 to $100."
    )
    result = classify_evidence_roles(
        sentence,
        _item(evidence_id="web:now:benzinga:web_body:0001", publisher="Benzinga"),
        ticker="NOW",
        session_date="2026-04-10",
        move_date_is_session=True,
        r4b_path="pre_deadline_publication",
    )
    assert set(result.roles) == {"ATTRIBUTION", "EVENT"}


def test_concrete_verified_event_for_another_issuer_is_not_event_role():
    sentence = "Apple announced a major contract Friday."
    item = _item(evidence_id="web:pypl:article:web_body:0001", publisher="Reuters")
    item.excerpt_text = sentence
    result = classify_evidence_roles(
        sentence,
        item,
        ticker="PYPL",
        session_date="2026-04-10",
        move_date_is_session=False,
        r4b_path="pre_deadline_publication",
    )

    assert result.roles == ("LEAD",)
    assert "EVENT_SUBJECT_MISMATCH" in result.reasons["EVENT"]


def _gap_day_quote(
    sentence, *, published, event_quote=None, event_relation="UNKNOWN",
    document_excerpt=None,
):
    item = _item(
        evidence_id="web:now:benzinga:web_body:0001", publisher="Benzinga",
        published=published,
    )
    item.content_state = "FULL_TEXT"
    item.excerpt_text = document_excerpt or sentence
    from catalyst_data.canonical.temporal import (
        build_temporal_identity,
        with_event_time_publication_window,
    )

    temporal = with_event_time_publication_window(
        build_temporal_identity("2026-04-10"), now=datetime(2026, 10, 10, tzinfo=timezone.utc),
    )
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=(item.evidence_id,),
        temporal_identity=temporal,
        observation=SimpleNamespace(ticker="NOW", gap_return=-8.0, target_return=-9.0),
    )
    quote = {"evidence_id": item.evidence_id, "text": sentence}
    if event_quote is not None:
        quote.update(event_time_quote=event_quote, event_relation=event_relation, event_date="2026-04-10")
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": [item.evidence_id], "quotes": [quote],
    }]}
    _, (check,) = apply_quote_rules(payload, pack, evidence_window_policy="event_time_v1")
    return check


def test_gap_day_same_day_date_only_premarket_publication_is_event_eligible():
    sentence = (
        "ServiceNow Inc. (NYSE: NOW) shares fell 8% Friday after UBS downgraded the stock "
        "from Buy to Neutral and cut its price target."
    )
    check = _gap_day_quote(
        sentence, published="2026-04-10T14:00:00+00:00",
        event_quote="Friday after UBS downgraded the stock from Buy to Neutral",
    )
    assert check.kept
    assert check.r4b == "PASS"
    assert check.r4b_path == "pre_deadline_publication_gap_implied"
    assert set(check.roles) == {"ATTRIBUTION", "EVENT"}


def test_gap_day_date_only_event_can_use_same_document_attribution():
    event_sentence = "UBS downgraded the stock from Buy to Neutral Friday."
    document_excerpt = (
        event_sentence + " ServiceNow Inc. (NYSE: NOW) shares fell 8% Friday after UBS "
        "downgraded the stock from Buy to Neutral and cut its price target."
    )
    check = _gap_day_quote(
        event_sentence, published="2026-04-10T14:00:00+00:00",
        event_quote="UBS downgraded the stock from Buy to Neutral Friday",
        document_excerpt=document_excerpt,
    )
    assert check.kept
    assert check.r4b_path == "pre_deadline_publication_gap_implied"
    assert check.roles == ("EVENT",)


def test_gap_day_explicit_post_open_event_clock_fails_r4b():
    sentence = (
        "ServiceNow Inc. (NYSE: NOW) shares fell 8% Friday after UBS downgraded the stock "
        "from Buy to Neutral at 11 a.m. and cut its price target."
    )
    check = _gap_day_quote(
        sentence, published="2026-04-10T14:00:00+00:00",
        event_quote="Friday after UBS downgraded the stock from Buy to Neutral at 11 a.m.",
        event_relation="INTRADAY",
    )
    assert not check.kept
    assert check.r4b == "FAIL"
    assert check.r4b_path is None


def test_gap_day_post_close_date_only_recap_is_attribution_only():
    sentence = (
        "ServiceNow Inc. (NYSE: NOW) shares fell 8% Friday after UBS downgraded the stock "
        "from Buy to Neutral and cut its price target."
    )
    check = _gap_day_quote(
        sentence, published="2026-04-10T21:00:00+00:00",
        event_quote="Friday after UBS downgraded the stock from Buy to Neutral",
    )
    assert check.kept
    assert check.r4b == "PASS"
    assert check.r4b_path == "extracted_event_time_date_only"
    assert check.roles == ("ATTRIBUTION",)


def test_real_hon_quote_about_spinoff_hona_is_not_hon_attribution():
    # Real x60 HON quote from grade_smoke1/evt_smoke1; HON is only a parenthetical comparator.
    sentence = (
        "Honeywell Aerospace ( HONA +0.03% ) stock plunged 20.8% through 9:50 a.m. ET "
        "in early trading on the Nasdaq Thursday, after missing on its first earnings report "
        "since spinning off from parent company Honeywell ( HON -1.11% ) in June."
    )
    result = classify_evidence_roles(
        sentence,
        _item(evidence_id="web:hon:tipranks:web_body:0001", publisher="TipRanks"),
        ticker="HON",
        session_date="2026-08-06",
        move_date_is_session=True,
        r4b_path="pre_deadline_publication",
    )
    assert "ATTRIBUTION" not in result.roles
    assert result.roles == ("EVENT",)


def test_nearest_moving_ticker_controls_subject_even_if_parent_is_mentioned():
    sentence = (
        "HONA shares plunged Friday after the company reported disappointing earnings, "
        "while parent HON was unchanged."
    )
    result = classify_evidence_roles(
        sentence, _item(evidence_id="hona", publisher="Reuters"), ticker="HON",
        session_date="2026-08-06", move_date_is_session=True,
        r4b_path="pre_deadline_publication",
    )
    assert "ATTRIBUTION" not in result.roles


def test_move_date_must_resolve_to_session_and_same_day_page_is_accepted():
    sentence = "X shares fell 4% after the company cut its outlook."
    common = dict(ticker="X", session_date="2026-04-10", r4b_path="pre_deadline_publication")
    before = classify_evidence_roles(sentence, _item(evidence_id="before", publisher="Reuters", published="2026-04-09T16:00:00+00:00"), move_date_is_session=None, **common)
    same_day = classify_evidence_roles(sentence, _item(evidence_id="same", publisher="Reuters", published="2026-04-10T16:00:00+00:00"), move_date_is_session=None, **common)
    assert "ATTRIBUTION" not in before.roles
    assert "ATTRIBUTION" in same_day.roles
    future_dated_preview = classify_evidence_roles(
        "X shares fell on 2026-04-10 after the company cut its outlook.",
        _item(evidence_id="preview", publisher="Reuters", published="2026-04-09T16:00:00+00:00"),
        move_date_is_session=None, **common,
    )
    assert "ATTRIBUTION" not in future_dated_preview.roles


def test_stock_price_reporting_is_not_a_concrete_event():
    for sentence in (
        "X shares fell Friday after Reuters reported that the stock fell 8%.",
        "X shares fell Friday after Reuters reported that the stock hit a 52-week low.",
    ):
        result = classify_evidence_roles(
            sentence, _item(evidence_id="circular", publisher="Reuters"), ticker="X",
            session_date="2026-04-10", move_date_is_session=True,
            r4b_path="pre_deadline_publication",
        )
        assert "EVENT" not in result.roles, sentence


def test_news_link_phrasings_accept_concrete_company_events():
    for sentence in (
        "X announced a major contract Friday, and shares jumped 8% on the news.",
        "X shares jumped 8% on the announcement that X won a major contract.",
        "X shares jumped 8% on the report that X won a major contract.",
        "X shares jumped 8% on the results after X reported earnings.",
    ):
        result = classify_evidence_roles(
            sentence, _item(evidence_id="phrase", publisher="Reuters"), ticker="X",
            session_date="2026-04-10", move_date_is_session=True,
            r4b_path="pre_deadline_publication",
        )
        assert "ATTRIBUTION" in result.roles, sentence


def test_cross_sentence_heres_why_remains_out_of_scope():
    sentence = "X stock is falling today. Here’s why: the company reported a loss."
    result = classify_evidence_roles(
        sentence, _item(evidence_id="headline", publisher="Reuters"), ticker="X",
        session_date="2026-04-10", move_date_is_session=True,
        r4b_path="pre_deadline_publication",
    )
    assert "ATTRIBUTION" not in result.roles


def test_motley_fool_aliases_share_one_publisher_family():
    labels = ("Motley Fool", "The Motley Fool", "fool.com")
    assert {publisher_family(_item(evidence_id=label, publisher=label)) for label in labels} == {"motley_fool"}
    yahoo_copy = _item(evidence_id="yahoo", publisher="Yahoo Finance", url="https://finance.yahoo.com/news/x")
    yahoo_copy.byline = "The Motley Fool"
    assert publisher_family(yahoo_copy) == "motley_fool"
    wall_street = _item(evidence_id="wall", publisher="news", url="https://247wallst.com/story")
    assert publisher_family(wall_street) == "247_wall_street"


def test_event_time_quote_checks_persist_roles_reasons_and_timing_path():
    sentence = (
        "ServiceNow Inc (NYSE: NOW ) shares are sliding Friday after UBS analyst "
        "Karl Keirstead downgraded the stock from Buy to Neutral and sharply "
        "reduced his price target from $170 to $100."
    )
    item = _item(
        evidence_id="now-wire", publisher="Benzinga", url="https://benzinga.com/article",
        published="2026-04-10T18:00:00+00:00",
    )
    item = SimpleNamespace(**vars(item), content_state="FULL_TEXT", excerpt_text=sentence)
    from catalyst_data.canonical.temporal import (
        build_temporal_identity,
        with_event_time_publication_window,
    )

    temporal = with_event_time_publication_window(
        build_temporal_identity("2026-04-10"), now=datetime(2026, 10, 10, tzinfo=timezone.utc),
    )
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=(item.evidence_id,),
        temporal_identity=temporal, observation=SimpleNamespace(ticker="NOW"),
    )
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": [item.evidence_id],
        "quotes": [{"evidence_id": item.evidence_id, "text": sentence}],
    }]}
    _, (check,) = apply_quote_rules(payload, pack, evidence_window_policy="event_time_v1")
    assert check.kept and set(check.roles) == {"ATTRIBUTION", "EVENT"}
    assert check.r4b_path == "pre_deadline_publication"
    assert "ATTRIBUTION_STRONG_EVENT_LINK" in check.role_reasons["ATTRIBUTION"]
    assert check.document_key == "https://benzinga.com/article"
    assert check.provenance_fingerprint
    assert all(len(value) == 10 for value in check.provenance_fingerprint)
    rejected_payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": [item.evidence_id],
        "quotes": [{"evidence_id": item.evidence_id,
                    "text": "This invented sentence does not appear in the evidence document."}],
    }]}
    _, (rejected_check,) = apply_quote_rules(
        rejected_payload, pack, evidence_window_policy="event_time_v1",
    )
    assert not rejected_check.kept
    assert rejected_check.document_key == "https://benzinga.com/article"
    assert rejected_check.provenance_fingerprint == check.provenance_fingerprint


def test_divergent_same_direction_reference_headline_uses_well_ahead_wording():
    from catalyst_agents.attribution.answer_snapshot import _headline

    profile = SimpleNamespace(
        target_return=5.29,
        sector_ticker="XLP",
        sector_return=0.86,
        sector_comove=SimpleNamespace(band=SimpleNamespace(value="DIVERGENT")),
        market_return=None,
        market_comove=None,
        peer_summary=None,
        peer_comove=None,
        commodity_ticker=None,
        commodity_return=None,
    )
    assert _headline("TGT", profile) == (
        "TGT rose 5.29%, well ahead of its sector (XLP +0.86%)."
    )


def _quote_check(evidence_id, roles, family, document, *, source_class="reported_news", text="A sentence.", fingerprint=()):
    return {
        "hypothesis_ref": "h1", "evidence_id": evidence_id, "kept": True,
        "roles": roles, "publisher_family": family, "publisher": family,
        "document_id": document, "document_key": document,
        "provenance_fingerprint": list(fingerprint),
        "source_class": source_class, "text": text,
    }


def _grade(checks, *, status="PARTIAL", ceiling_codes=()):
    return grade_event_time_hypothesis(
        tier="T3", status=status,
        hypotheses=[{"ref": "h1", "proposed_role": "PRIMARY"}],
        verifier={"called": True, "verdict": "SUPPORTED"}, checks=checks, gates=[],
        ceiling_codes=ceiling_codes,
    )


def test_attribution_plus_different_document_event_is_confirmed():
    result = _grade([
        _quote_check("news-a", ["ATTRIBUTION"], "wire-a", "doc-a",
                     text="Shares fell after the board cut guidance."),
        _quote_check("event-b", ["EVENT"], "wire-b", "doc-b",
                     text="The board cut guidance on Tuesday."),
    ])
    assert result["grade"] == "CONFIRMED"


def test_syndicated_article_quotes_do_not_confirm_across_different_urls_or_text():
    original = _item(evidence_id="reuters", publisher="Reuters", url="https://reuters.com/article/1")
    yahoo_copy = _item(evidence_id="yahoo", publisher="Yahoo Finance", url="https://finance.yahoo.com/news/1")
    yahoo_copy.byline = "Reuters"
    result = _grade([
        _quote_check("reuters", ["ATTRIBUTION"], publisher_family(original), "url-a",
                     text="X shares fell after guidance was cut.", fingerprint=("shared-sentence",)),
        _quote_check("yahoo", ["EVENT"], publisher_family(yahoo_copy), "url-b",
                     text="The company cut guidance Friday.", fingerprint=("shared-sentence",)),
    ])
    assert result["grade"] == "REPORTED"


def test_any_independent_event_can_confirm_even_if_an_earlier_event_cannot():
    result = _grade([
        _quote_check("a", ["ATTRIBUTION"], "wire-a", "doc-a", text="Shares fell after guidance was cut."),
        _quote_check("b", ["EVENT"], "wire-a", "doc-a", text="The board cut guidance."),
        _quote_check("c", ["EVENT"], "primary:sec", "sec-doc", source_class="sec", text="Issuer filed an 8-K."),
    ])
    assert result["grade"] == "CONFIRMED"
    assert result["event_evidence_id"] == "c"


def test_verbatim_sentence_across_publishers_is_one_provenance():
    sentence = "The company cut guidance Friday."
    result = _grade([
        _quote_check("a", ["ATTRIBUTION"], "reuters", "doc-a", text=sentence, fingerprint=("same",)),
        _quote_check("b", ["EVENT"], "yahoo_finance", "doc-b", text=sentence, fingerprint=("same",)),
    ])
    assert result["grade"] == "REPORTED"


def test_same_publisher_family_distinct_article_provenance_confirms():
    result = _grade([
        _quote_check("news-a", ["ATTRIBUTION"], "benzinga", "doc-a",
                     text="Shares of NOW fell after UBS cut its target."),
        _quote_check("news-b", ["EVENT"], "benzinga", "doc-b",
                     text="UBS downgraded ServiceNow to Neutral and reduced the target."),
    ])
    assert result["grade"] == "CONFIRMED"


def test_attribution_without_separate_event_is_reported():
    # Same-session event text in the D+1 recap is also an event record, but not
    # independent of its attribution because both quotes belong to one article.
    sentence = "Shares of X fell 4% yesterday after the company cut its outlook."
    result = _grade([_quote_check(
        "recap", ["ATTRIBUTION", "EVENT"], "wire-a", "doc-a", text=sentence,
    )])
    assert result["grade"] == "REPORTED"


def test_recap_plus_issuer_event_is_confirmed():
    sentence = "Shares of X fell 4% yesterday after the company cut its outlook."
    result = _grade([
        _quote_check("recap", ["ATTRIBUTION", "EVENT"], "wire-a", "doc-a", text=sentence),
        _quote_check("issuer", ["EVENT"], "primary:issuer_disclosure", "doc-issuer",
                     source_class="issuer_disclosure", text="The company cut its outlook on Wednesday."),
    ])
    assert result["grade"] == "CONFIRMED"


def test_syndicated_copies_from_one_family_remain_reported():
    sentence = "The company cut its outlook on Wednesday."
    result = _grade([
        _quote_check("copy-a", ["ATTRIBUTION"], "wire-a", "doc-a", text=sentence, fingerprint=("same",)),
        _quote_check("copy-b", ["EVENT"], "wire-a", "doc-b", text=sentence, fingerprint=("same",)),
    ])
    assert result["grade"] == "REPORTED"
    assert result["reported_by"] == [{"publisher": "wire-a", "publisher_family": "wire-a"}]


def test_yahoo_rehost_uses_original_publisher_family_when_url_identifies_it():
    item = _item(evidence_id="yahoo-copy", publisher="Yahoo Finance",
                 url="https://www.benzinga.com/news/example")
    assert publisher_family(item) == "benzinga"


def test_yahoo_syndicator_without_original_author_cannot_confirm():
    item = _item(evidence_id="yahoo-unknown", publisher="Yahoo Finance",
                 url="https://finance.yahoo.com/news/example")
    assert publisher_family(item) == "unknown"
    result = _grade([
        _quote_check("wire", ["ATTRIBUTION"], "reuters", "doc-a", text="Shares fell after guidance was cut.", fingerprint=("shared",)),
        _quote_check("yahoo", ["EVENT"], publisher_family(item), "doc-b", text="The company cut guidance Friday.", fingerprint=("shared",)),
    ])
    assert result["grade"] == "REPORTED"


def test_same_document_is_not_independent_even_with_different_sentences():
    result = _grade([
        _quote_check("a", ["ATTRIBUTION"], "benzinga", "same-doc", text="Shares fell after UBS cut its target."),
        _quote_check("b", ["EVENT"], "benzinga", "same-doc", text="UBS downgraded the shares."),
    ])
    assert result["grade"] == "REPORTED"


def test_event_only_hypothesis_is_no_clear_cause():
    result = _grade([_quote_check("event", ["EVENT"], "wire-a", "doc-a")])
    assert result["grade"] == "NO_CLEAR_CAUSE"


def test_only_missing_confirmation_ceilings_do_not_block_reported_grade():
    result = _grade(
        [_quote_check("recap", ["ATTRIBUTION"], "wire-a", "doc-a")],
        status="ABSTAIN", ceiling_codes=["MISSING_PRIMARY_CONFIRMATION"],
    )
    assert result["grade"] == "REPORTED"
    assert any(code.startswith("REPORTED_CEILING_EXCEPTION:") for code in result["reason_codes"])


def test_headline_uses_only_tier_governing_reference():
    from catalyst_agents.attribution.answer_snapshot import _headline
    from catalyst_agents.attribution.move_profile import (
        AlignmentBand,
        AvailabilityState,
        DirectionRelation,
        MoveProfile,
        SessionAlignment,
    )
    from catalyst_agents.attribution.tiers import decide_tier

    profile = MoveProfile(
        target_return=5.0, commodity_ticker="USO", commodity_return=0.1,
        sector_ticker="XLE", sector_return=2.0,
        sector_comove=SessionAlignment(
            schema_version="session_alignment_v1", reference_kind="SECTOR",
            target_return_pct=5.0, reference_return_pct=2.0, residual_return_pct=3.0,
            direction_relation=DirectionRelation.SAME_DIRECTION, band=AlignmentBand.ALIGNED,
            availability=AvailabilityState.AVAILABLE,
        ),
    )
    decision = decide_tier(profile, filings_in_window=0)
    governing = decision.governing_ref
    assert governing["kind"] == "sector"
    assert _headline("X", profile, governing) == "X rose 5.00% as its sector rose (XLE +2.00%)."
    assert _headline("X", profile, None) == "X rose 5.00%."
    zero_reference = profile.model_copy(update={"sector_return": 0.0, "sector_comove": None})
    zero_governing = {"kind": "sector", "ticker": "XLE", "return_pct": 0.0, "band": "ALIGNED"}
    assert _headline("X", zero_reference, zero_governing) == "X rose 5.00% while its sector was flat (XLE +0.00%)."
    flat_target = profile.model_copy(update={"target_return": 0.0, "sector_comove": None})
    assert decide_tier(flat_target, filings_in_window=0).governing_ref is None
    assert _headline("X", flat_target, None) == "X was flat 0.00%."


def test_snapshot_selects_key_sentence_by_evidence_and_exact_quote():
    from catalyst_agents.attribution.answer_snapshot import build_answer_snapshot

    item = _item(evidence_id="chunk", publisher="Reuters")
    context = SimpleNamespace(evidence_inventory=(item,))
    assessment = SimpleNamespace(
        final_status="PARTIAL",
        normalized_hypotheses=(SimpleNamespace(hypothesis_id="id", source_hypothesis_ref="h1"),),
    )
    claim = SimpleNamespace(role=SimpleNamespace(value="PRIMARY"), claim_id="c1", source_hypothesis_id="id")
    plan = SimpleNamespace(claims=(claim,), required_limitations=())
    trace = {"rounds": [{
        "model_reported": {"analyst": {"hypotheses": [{"ref": "h1", "proposed_role": "PRIMARY"}]}},
        "code_checks": {"quotes": [
            {"hypothesis_ref": "h1", "evidence_id": "chunk", "text": "Event quote.", "kept": True, "roles": ["EVENT"]},
            {"hypothesis_ref": "h1", "evidence_id": "chunk", "text": "Attribution quote.", "kept": True, "roles": ["ATTRIBUTION"]},
        ], "grade": {"grade": "REPORTED", "key_evidence_id": "chunk", "key_quote_text": "Attribution quote."}},
    }]}
    temporal = SimpleNamespace(
        session_date="2026-04-10", information_window_start_at=datetime(2026, 4, 9, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 4, 10, tzinfo=timezone.utc), session_open_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        publication_window=lambda: (datetime(2026, 4, 9, tzinfo=timezone.utc), datetime(2026, 4, 10, tzinfo=timezone.utc)),
        evidence_window_start_at=None,
    )
    snapshot = build_answer_snapshot(
        run_id="run", ticker="X", move_profile=SimpleNamespace(
            target_return=None, commodity_ticker=None, commodity_return=None,
            gap_return=None, market_comove=None, sector_comove=None,
            sector_ticker=None, peer_summary=None, peer_comove=None,
        ),
        temporal_identity=temporal, facts_text="", assessment=assessment,
        validated_claim_plan=plan, context_pack=context, decision_trace=trace,
        abstention_context={}, evidence_window_policy="event_time_v1",
    )
    assert snapshot["key_sentence"]["text"] == "Attribution quote."


def test_event_time_v2_grade_labels_do_not_change_pre_close_publication_outcome():
    hypotheses = [{"ref": "h1", "proposed_role": "PRIMARY"}]
    checks = [{"hypothesis_ref": "h1", "evidence_id": "q1", "text": "A checked quote.",
               "kept": True, "roles": ["LEAD"]}]
    supported = grade_event_time_v2_hypothesis(
        tier="T3", published=True, hypotheses=hypotheses,
        verifier={"verdict": "SUPPORTED"}, checks=checks, gates=(),
    )
    no_publication = grade_event_time_v2_hypothesis(
        tier="T3", published=False, hypotheses=hypotheses,
        verifier={"verdict": "SUPPORTED"}, checks=checks, gates=(),
    )
    assert supported["grade"] == "SUPPORTED"
    assert no_publication["grade"] == "NO_CLEAR_CAUSE"

    item = _item(evidence_id="q1", publisher="Reuters")
    context = SimpleNamespace(evidence_inventory=(item,))
    assessment = SimpleNamespace(
        final_status="PARTIAL",
        normalized_hypotheses=(SimpleNamespace(hypothesis_id="id", source_hypothesis_ref="h1"),),
    )
    claim = SimpleNamespace(role=SimpleNamespace(value="PRIMARY"), claim_id="c1", source_hypothesis_id="id")
    plan = SimpleNamespace(claims=(claim,), required_limitations=())
    trace = {"rounds": [{
        "model_reported": {"analyst": {"hypotheses": [{"ref": "h1", "proposed_role": "PRIMARY"}]},
                           "verifier": {"verdict": "SUPPORTED"}},
        "code_checks": {"quotes": [{"hypothesis_ref": "h1", "evidence_id": "q1", "text": "A checked quote.",
                                       "kept": True, "roles": ["LEAD"]}],
                        "grade": supported},
    }]}
    temporal = SimpleNamespace(
        session_date="2026-04-10", information_window_start_at=datetime(2026, 4, 9, tzinfo=timezone.utc),
        cutoff_at=datetime(2026, 4, 10, tzinfo=timezone.utc), session_open_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        publication_window=lambda: (datetime(2026, 4, 9, tzinfo=timezone.utc), datetime(2026, 4, 10, tzinfo=timezone.utc)),
        evidence_window_start_at=None,
    )
    kwargs = dict(
        run_id="run", ticker="X", move_profile=SimpleNamespace(
            target_return=None, commodity_ticker=None, commodity_return=None, gap_return=None,
            market_comove=None, sector_comove=None, sector_ticker=None, peer_summary=None, peer_comove=None,
        ), temporal_identity=temporal, facts_text="", assessment=assessment,
        validated_claim_plan=plan, context_pack=context, decision_trace=trace,
        abstention_context={}, evidence_window_policy="event_time_v2",
    )
    answered = build_answer_snapshot(**kwargs)
    trace["rounds"][0]["code_checks"]["grade"] = no_publication
    relabelled = build_answer_snapshot(**kwargs)
    assert answered["grade"] == "SUPPORTED"
    assert relabelled["grade"] == "NO_CLEAR_CAUSE"
    assert answered["outcome"] == relabelled["outcome"] == "ANSWERED"


def test_possible_explanation_quotes_exclude_other_company_subject_and_verifier_categories():
    from catalyst_agents.attribution.answer_snapshot import possible_explanation_quotes

    sentence = "HONA shares plunged Friday after the company reported disappointing earnings, while parent HON was unchanged."
    item = _item(evidence_id="hona", publisher="Reuters", url="https://reuters.com/story")
    item.source_class = "reported_news"
    pack = SimpleNamespace(evidence_inventory=(item,))
    roles = classify_evidence_roles(
        sentence, item, ticker="HON", session_date="2026-08-06", move_date_is_session=True,
        r4b_path="pre_deadline_publication",
    )
    check = {"hypothesis_ref": "h1", "evidence_id": "hona", "kept": True,
             "text": sentence, "roles": list(roles.roles),
             "role_reasons": {key: list(value) for key, value in roles.reasons.items()},
             "publisher_family": "reuters", "r4b_path": "pre_deadline_publication"}
    trace = {"rounds": [{
        "model_reported": {"analyst": {"hypotheses": [{"ref": "h1", "proposed_role": "PRIMARY"}]},
                           "verifier": {"verdict": "REJECT", "category": "UNLINKED"}},
        "code_checks": {"quotes": [check]},
    }]}
    assert possible_explanation_quotes(trace, pack) == []
    check["role_reasons"]["EVENT"] = ["EVENT_SUBJECT_MATCH"]
    for category in ("OTHER_COMPANY", "OTHER_SESSION", "PREVIEW"):
        trace["rounds"][0]["model_reported"]["verifier"]["category"] = category
        assert possible_explanation_quotes(trace, pack) == []
