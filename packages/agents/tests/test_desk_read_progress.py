from __future__ import annotations

from catalyst_agents.attribution.extraction import AboutTicker, Extraction
from catalyst_agents.nodes.desk_decision import read_reason_code


def test_read_reason_code_says_what_the_article_says_about_the_move() -> None:
    assert read_reason_code(Extraction.invalid("extraction_failed:Boom")) == "read_failed"
    assert read_reason_code(Extraction(about_ticker=AboutTicker.OTHER)) == "read_not_about_company"
    assert read_reason_code(Extraction(about_ticker=None)) == "read_not_about_company"
    stated = Extraction(about_ticker=AboutTicker.ISSUER, explicit_attribution=True,
                        attribution_quote="Shares fell after the guidance cut.")
    assert read_reason_code(stated) == "read_states_cause"
    event = Extraction(about_ticker=AboutTicker.GROUP, event_quote="The bank raised its target.")
    assert read_reason_code(event) == "read_reports_event"
    assert read_reason_code(Extraction(about_ticker=AboutTicker.ISSUER)) == "read_no_cause"


def test_verifier_reasoning_setting(monkeypatch) -> None:
    from catalyst_agents.attribution.verifier import (
        VERIFIER_REASONING_ENV,
        verifier_reasoning_options,
    )

    off = {"extra_body": {"thinking": {"type": "disabled"}}}
    monkeypatch.delenv(VERIFIER_REASONING_ENV, raising=False)
    assert verifier_reasoning_options() == off
    monkeypatch.setenv(VERIFIER_REASONING_ENV, "default")
    assert verifier_reasoning_options() == {}
    monkeypatch.setenv(VERIFIER_REASONING_ENV, "off")
    assert verifier_reasoning_options() == {"extra_body": {"thinking": {"type": "disabled"}}}
    monkeypatch.setenv(VERIFIER_REASONING_ENV, "low")
    assert verifier_reasoning_options() == {"reasoning_effort": "low"}
    monkeypatch.setenv(VERIFIER_REASONING_ENV, "budget:1500")
    assert verifier_reasoning_options() == {"extra_body": {"thinking": {"type": "enabled", "budget_tokens": 1500}}}
    monkeypatch.setenv(VERIFIER_REASONING_ENV, "nonsense")
    assert verifier_reasoning_options() == off
