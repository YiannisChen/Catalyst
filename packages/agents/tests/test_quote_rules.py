"""Q1c quote rules: only verbatim, complete, in-window full-text sentences survive."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from catalyst_agents.attribution.quotes import apply_quote_rules, check_quote

UTC = timezone.utc
WINDOW = SimpleNamespace(
    information_window_start_at=datetime(2026, 1, 14, 21, tzinfo=UTC),
    cutoff_at=datetime(2026, 1, 15, 21, tzinfo=UTC),
    session_open_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
    session_date="2026-01-15",
)
BODY = (
    "UnitedHealth Group Reports 2025 Results &#8226; Revenues were $447.6 "
    "billion, up 12% from the prior year. The company expects 2026 revenues "
    "of more than $439 billion. Revenue 447,567 400,278 12% 9%"
)


def _item(evidence_id="e1", state="FULL_TEXT", at=datetime(2026, 1, 15, 11, tzinfo=UTC)):
    return SimpleNamespace(
        evidence_id=evidence_id, content_state=state, eligible_at=at,
        excerpt_text=BODY, source_class="issuer_disclosure",
    )


def _check(text, item=None, supporting=("e1",)):
    item = item or _item()
    return check_quote(
        text, item.evidence_id, supporting_ids=set(supporting),
        items={item.evidence_id: item}, temporal_identity=WINDOW,
    )


def test_verbatim_complete_sentence_passes_after_entity_and_space_normalization():
    assert _check("Revenues were $447.6  billion, up 12% from the prior year.") == "OK"


def test_partial_sentence_and_paraphrase_fail():
    assert _check("up 12% from the prior year.") == "R2_NOT_COMPLETE_SENTENCE"
    assert _check("Revenues rose 12% from the prior year.") == "R2_NOT_VERBATIM"


def test_table_fragment_is_not_prose():
    assert _check("Revenue 447,567 400,278 12% 9%") != "OK"


def test_headline_state_window_and_support_rules():
    quote = "The company expects 2026 revenues of more than $439 billion."
    assert _check(quote, _item(state="TITLE_ONLY")) == "R3_NOT_FULL_TEXT"
    early = _item(at=datetime(2026, 1, 14, 12, tzinfo=UTC))
    assert _check(quote, early) == "R4_OUTSIDE_SESSION_WINDOW"
    assert _check(quote, supporting=()) == "R1_NOT_SUPPORTING_PACK_EVIDENCE"


def _pack():
    return SimpleNamespace(
        evidence_inventory=(_item(),), included_evidence_ids=("e1",),
        temporal_identity=WINDOW,
    )


def _payload(quote_text):
    return {
        "candidate_hypotheses": [
            {
                "hypothesis_ref": "h1",
                "statement": "model prose that must never be published",
                "mechanism": "model mechanism",
                "quotes": [{"evidence_id": "e1", "text": quote_text}],
                "supporting_evidence_ids": ["e1"],
            }
        ],
        "evidence_decisions": [
            {
                "evidence_id": "e1", "disposition": "SUPPORT",
                "supports_hypothesis_refs": ["h1"], "reason": "retained audit detail",
            }
        ],
        "proposed_missing_evidence": [
            {"proposal_ref": "p1", "related_hypothesis_refs": ["h1"]}
        ],
        "recommended_status": "SUFFICIENT",
    }


def test_statement_becomes_the_quote_and_mechanism_is_cleared():
    quote = "The company expects 2026 revenues of more than $439 billion."
    out, checks = apply_quote_rules(_payload(quote), _pack())
    (hypothesis,) = out["candidate_hypotheses"]
    assert quote in hypothesis["statement"]
    assert "model prose" not in hypothesis["statement"]
    assert hypothesis["mechanism"] is None
    assert [c.rule for c in checks] == ["OK"]


def test_a_hypothesis_without_a_valid_quote_is_dropped_everywhere():
    out, checks = apply_quote_rules(_payload("Revenue fell sharply."), _pack())
    assert out["candidate_hypotheses"] == []
    assert out["proposed_missing_evidence"][0]["related_hypothesis_refs"] == []
    assert "supports_hypothesis_refs" not in out["evidence_decisions"][0]
    assert out["evidence_decisions"][0]["reason"] == "retained audit detail"
    assert out["recommended_status"] == "ABSTAIN"
    assert checks[0].kept is False


def test_sentence_after_a_flattened_table_starts_a_sentence_but_mid_sentence_names_do_not():
    body = (
        "Net income 25,824 21,939 18% Revenue increased $24.9 billion or 14% "
        "with growth across each of our segments. Growth came from an increase "
        "in Google Cloud revenues of $7.8 billion, or 63%."
    )
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 15, 11, tzinfo=UTC), excerpt_text=body,
        source_class="issuer_disclosure",
    )
    ok = "Revenue increased $24.9 billion or 14% with growth across each of our segments."
    cut = "Google Cloud revenues of $7.8 billion, or 63%."
    assert _check(ok, item) == "OK"
    assert _check(cut, item) == "R2_NOT_COMPLETE_SENTENCE"


def test_enumerated_clause_ending_in_semicolon_is_quotable():
    body = (
        "China will (i) modify the additional rate of duty on those articles by "
        "suspending 24 percentage points of that rate for an initial period of 90 days; "
        "and (ii) adopt all necessary administrative measures."
    )
    item = SimpleNamespace(
        evidence_id="e1", content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 15, 11, tzinfo=UTC), excerpt_text=body,
        source_class="official_government",
    )
    clause = (
        "modify the additional rate of duty on those articles by suspending 24 "
        "percentage points of that rate for an initial period of 90 days;"
    )
    assert _check(clause, item) == "OK"


def test_a_quote_is_published_once_even_if_two_hypotheses_cite_it():
    quote = "The company expects 2026 revenues of more than $439 billion."
    payload = _payload(quote)
    second = {**payload["candidate_hypotheses"][0], "hypothesis_ref": "h2"}
    payload["candidate_hypotheses"].append(second)
    out, checks = apply_quote_rules(payload, _pack())
    assert [h["hypothesis_ref"] for h in out["candidate_hypotheses"]] == ["h1"]
    assert [c.rule for c in checks] == ["OK", "R5_DUPLICATE_QUOTE"]


def test_statement_trim_releases_quote_payload_ids_and_global_budget():
    first_quote = (
        "The company reported strong revenue growth across all business segments " * 11
    ).strip() + "."
    second_quote = (
        "The company expanded its customer base across several regions " * 11
    ).strip() + "."
    long_url = "https://example.com/" + "x" * 700
    first_item = _item("e1")
    first_item.excerpt_text = first_quote
    first_item.canonical_url = long_url
    second_item = _item("e2")
    second_item.excerpt_text = second_quote
    second_item.canonical_url = long_url
    pack = SimpleNamespace(
        evidence_inventory=(first_item, second_item),
        included_evidence_ids=("e1", "e2"),
        temporal_identity=WINDOW,
    )
    first_hypothesis = {
        **_payload(first_quote)["candidate_hypotheses"][0],
        "supporting_evidence_ids": ["e1", "e2"],
        "quotes": [
            {"evidence_id": "e1", "text": first_quote},
            {"evidence_id": "e2", "text": second_quote},
        ],
    }
    second_hypothesis = {
        **first_hypothesis,
        "hypothesis_ref": "h2",
        "supporting_evidence_ids": ["e2"],
        "quotes": [{"evidence_id": "e2", "text": second_quote}],
    }
    payload = {
        **_payload(first_quote),
        "candidate_hypotheses": [first_hypothesis, second_hypothesis],
    }

    out, checks = apply_quote_rules(payload, pack)

    assert [hypothesis["hypothesis_ref"] for hypothesis in out["candidate_hypotheses"]] == ["h1", "h2"]
    first_result, second_result = out["candidate_hypotheses"]
    assert first_result["supporting_evidence_ids"] == ["e1"]
    assert [quote["text"] for quote in first_result["quotes"]] == [first_quote]
    assert [quote["text"] for quote in second_result["quotes"]] == [second_quote]
    assert [check.rule for check in checks] == ["OK", "R5_STATEMENT_LENGTH", "OK"]
    assert [check.kept for check in checks] == [True, False, True]


def test_quote_reporting_another_sessions_move_is_rejected():
    from catalyst_agents.attribution.quotes import check_quote

    text = "Shares of UnitedHealth slumped on Wednesday, tanking 5.9% by the close."
    item = SimpleNamespace(
        evidence_id="ev:other-day", content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 15, 15, tzinfo=UTC), excerpt_text=text,
    )
    assert check_quote(
        text, "ev:other-day", supporting_ids={"ev:other-day"},
        items={"ev:other-day": item}, temporal_identity=WINDOW,
    ) == "R6_OTHER_SESSION_MOVE"


def _match_key_before_html_guard(text):
    """The comparison form before the HTML guard, kept to lock plain-text behaviour."""
    import re

    from catalyst_agents.attribution.quotes import _QUOTE_MARKS, normalize_text

    value = normalize_text(text).translate(_QUOTE_MARKS)
    value = re.sub(r"\s+([,.;:%)\]])", r"\1", value)
    return re.sub(r"([(\[$])\s+", r"\1", value)


def test_plain_text_match_key_is_unchanged_by_the_html_guard():
    from catalyst_agents.attribution.quotes import _match_key

    plain = [
        BODY,
        "Abbott Laboratories (NYSE: ABT ) stock fell on Thursday .",
        "Margins rose 13 % as costs < revenue and revenue > plan.",
        "Shares of <NYSE: ABT> fell; see &lt;b&gt;bold&lt;/b&gt; and a<b comparison.",
        "“Curly” ‘quotes’ and  extra   spaces here.",
        "",
    ]
    for text in plain:
        assert _match_key(text) == _match_key_before_html_guard(text)


def test_text_primitives_keep_the_quotes_compatibility_exports():
    from catalyst_agents.attribution import quotes, text

    for name in (
        "normalize_text", "_match_key", "_provenance_fingerprint", "_count_sentences",
        "_is_prose", "_starts_at_sentence",
    ):
        assert getattr(quotes, name) is getattr(text, name)
    assert quotes._QUOTE_MARKS is text._QUOTE_MARKS
    assert quotes._LIST_MARKER is text._LIST_MARKER


def test_sentence_counter_accepts_closing_brackets_after_terminal_punctuation():
    from catalyst_agents.attribution.text import _count_sentences

    assert _count_sentences("One ends.] Two ends!] Three ends.") == 3


def test_quote_matches_a_body_that_still_carries_html_markup():
    html_body = (
        "<p>UnitedHealth Group Reports 2025 Results &#8226;</p><p>Rev<b>enues</b> were "
        "<span class=\"num\">$447.6</span> billion, up 12% from the prior year.</p>"
        "<!-- tracking --><br/>The company expects 2026 revenues of more than $439 billion.</p>"
    )
    item = _item()
    item.excerpt_text = html_body
    assert _check("Revenues were $447.6 billion, up 12% from the prior year.", item) == "OK"
    assert _check("The company expects 2026 revenues of more than $439 billion.", item) == "OK"


def test_escaped_markup_is_text_not_a_tag():
    from catalyst_agents.attribution.quotes import _match_key

    assert "<b>" in _match_key("A &lt;b&gt; tag is shown.")
