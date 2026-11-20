"""Step 3: PRIMARY published quote must name its subject."""
import json
from types import SimpleNamespace as NS

import pytest
from catalyst_agents.attribution.analyst import AttributionStatus, HypothesisRole
from catalyst_agents.attribution.assessment import _subject_ceiling


@pytest.fixture(autouse=True)
def _sec_names(tmp_path, monkeypatch):
    names = {"0": {"ticker": "SLB", "title": "SLB N.V."}, "1": {"ticker": "DE", "title": "DEERE & CO"},
             "2": {"ticker": "AMT", "title": "AMERICAN TOWER CORP"}}
    (tmp_path / "sec_company_tickers.json").write_text(json.dumps(names))
    (tmp_path / "bodies.sqlite3").touch()
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(tmp_path / "bodies.sqlite3"))


def _check(target, cause, quote, *, url="https://example.com/news", sector=None, commodity=None):
    hypothesis = NS(
        normalized_role=HypothesisRole.PRIMARY,
        cause_type=NS(value=cause),
        statement=f"“{quote}” — Benzinga, 2026-03-03 15:05 UTC, {url}",
        supporting_evidence_ids=("e1",),
    )
    item = NS(
        evidence_id="e1", source_class=NS(value="reported_news"), canonical_url=url,
        excerpt_text=f"Headline. {quote} More text.",
    )
    pack = NS(
        observation=NS(ticker=target, sector_ticker=sector, commodity_ticker=commodity),
        evidence_inventory=(item,),
    )
    return _subject_ceiling((hypothesis,), pack)


def test_company_subject_and_opinion_exclusion():
    assert _check("SLB", "COMPANY_SPECIFIC_CATALYST", "Halliburton Company's HAL auction of equipment has been called off by Venezuela after intervention from the U.S. administration.") == (AttributionStatus.ABSTAIN, "primary_off_subject:COMPANY_SPECIFIC_CATALYST")
    assert _check("TMUS", "COMPANY_SPECIFIC_CATALYST", "The company recently signed a more than $1 billion agreement with Google.") == (AttributionStatus.ABSTAIN, "primary_off_subject:COMPANY_SPECIFIC_CATALYST")
    assert _check("AMT", "COMPANY_SPECIFIC_CATALYST", "American Tower (NYSE: AMT ) and Crown Castle lease the same thing.", url="https://www.benzinga.com/Opinion/26/06/x") == (AttributionStatus.ABSTAIN, "primary_off_subject:COMPANY_SPECIFIC_CATALYST")
    assert _check("DE", "COMPANY_SPECIFIC_CATALYST", "Jefferies upgraded Deere (DE) to Hold from Underperform with a $550 price target.") == (None, "")


def test_sector_commodity_and_macro_group_subjects():
    assert _check("AVGO", "SECTOR_MOVE", "The chip sector advanced after Microsoft Corp reported stronger-than-expected earnings.", sector="XLK") == (None, "")
    assert _check("NEM", "COMMODITY", "Gold rebounded more than 1% on bargain buying.", commodity="GLD") == (None, "")
    quote = "U.S. stocks fell as oil spiked."
    assert _check("GS", "MACRO_EVENT", quote) == (None, "")
    assert _check("GS", "EARNINGS", quote) == (AttributionStatus.ABSTAIN, "primary_off_subject:EARNINGS")


def test_url_and_publisher_do_not_count_as_naming_the_target():
    quote = "Halliburton Company's HAL auction of equipment has been called off."
    assert _check("SLB", "EARNINGS", quote, url="https://www.fool.com/why-SLB-stock-fell/") == (
        AttributionStatus.ABSTAIN, "primary_off_subject:EARNINGS"
    )


def test_gate_is_off_without_the_sec_name_map(monkeypatch):
    monkeypatch.delenv("CATALYST_NEWS_BODIES_DB")
    assert _check("TMUS", "EARNINGS", "Verizon signed a deal.") == (None, "")


def test_segment_wording_names_the_sector():
    quote = "There wasn't any company-specific news for Salesforce, but the entire enterprise software segment was down harshly today."
    assert _check("INTU", "SECTOR_MOVE", quote, sector="XLK") == (None, "")


def _store_title(tmp_path, chunk_id, title):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "bodies.sqlite3")
    conn.execute("CREATE TABLE IF NOT EXISTS web_body_chunks (chunk_id TEXT, title TEXT)")
    conn.execute("INSERT INTO web_body_chunks VALUES (?, ?)", (chunk_id, title))
    conn.commit()
    conn.close()


def _check_stored(target, quote, title, tmp_path):
    eid = "web:abc:web_body:0001"
    _store_title(tmp_path, eid, title)
    hypothesis = NS(
        normalized_role=HypothesisRole.PRIMARY, cause_type=NS(value="EARNINGS"),
        statement=f"“{quote}” — Benzinga, 2026-03-03 15:05 UTC, https://example.com/a",
        supporting_evidence_ids=(eid,),
    )
    item = NS(evidence_id=eid, source_class=NS(value="reported_news"),
              canonical_url="https://example.com/a", excerpt_text=f"Lead. {quote} More.")
    pack = NS(observation=NS(ticker=target, sector_ticker=None, commodity_ticker=None),
              evidence_inventory=(item,))
    return _subject_ceiling((hypothesis,), pack)


def test_unnamed_sentence_in_an_article_about_the_target_counts(tmp_path):
    quote = "The U.S. large pharma company reported adjusted earnings of $1.58, beating the Wall Street estimate of $1.42."
    assert _check_stored("DE", quote, "Deere Beats Q1 Estimates", tmp_path) == (None, "")


def test_article_about_another_company_still_fails(tmp_path):
    quote = "The move aligns with the administration's broader strategy to restore oil production."
    assert _check_stored("SLB", quote, "Halliburton's Asset Sale Halt Paves the Way", tmp_path) == (
        AttributionStatus.ABSTAIN, "primary_off_subject:EARNINGS"
    )
