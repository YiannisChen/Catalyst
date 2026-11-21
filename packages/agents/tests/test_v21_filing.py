"""v2.1 rule ``filing``: SEC exhibits are quoted by their own units; the earnings fallback answers with the filing fact
and the release headline. With the rule off nothing changes."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from catalyst_agents.attribution import quotes as Q
from catalyst_agents.engine import earnings as E
from catalyst_data.ingestion.collectors import sec_structure

UTC = timezone.utc
URL = "https://www.sec.gov/Archives/edgar/data/6281/000000628126000050/ex991.htm"
HTML = """<html><body>
<p><b>Analog Devices Reports Record Fiscal Second Quarter 2026 Financial Results</b></p>
<ul><li>Revenue of $3.62 billion, with year-over-year growth across all end markets, led by Industrial</li>
<li>Returned $1.3 billion to shareholders via dividends and share repurchases in the second quarter</li></ul>
<p>WILMINGTON, Mass.--May 20, 2026 --Analog Devices, Inc. (Nasdaq: ADI), a global semiconductor leader, today
announced financial results for its fiscal second quarter 2026, which ended May 2, 2026. Revenue grew strongly in
every end market and the company raised its dividend for the quarter.</p>
</body></html>"""
TITLE = "Analog Devices Reports Record Fiscal Second Quarter 2026 Financial Results"
LEAD = ("WILMINGTON, Mass.--May 20, 2026 --Analog Devices, Inc. (Nasdaq: ADI), a global semiconductor leader, today "
        "announced financial results for its fiscal second quarter 2026, which ended May 2, 2026.")
BULLET = "Revenue of $3.62 billion, with year-over-year growth across all end markets, led by Industrial"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    path = tmp_path / "bodies.sqlite3"
    conn = sqlite3.connect(path)
    sec_structure.write(conn, URL, "0000006281-26-000050", sec_structure.parse(HTML))
    conn.commit()
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(path))
    return path


def excerpt() -> str:
    return "\n\n".join(u["text"] if u["kind"] != "bullet" else "• " + u["text"]
                       for u in sec_structure.parse(HTML)["units"])


def item(url=URL):
    return SimpleNamespace(canonical_url=url)


def test_parse_finds_title_bullets_and_paragraphs():
    s = sec_structure.parse(HTML)
    assert s["title"] == TITLE
    assert [u["kind"] for u in s["units"]] == ["title", "bullet", "bullet", "paragraph"]
    assert s["units"][1]["text"].startswith("Revenue of $3.62 billion")


def test_units_make_the_lead_sentence_bullet_and_title_quotable(store, monkeypatch):
    key = Q._match_key
    assert Q._filing_unit_rule(key(TITLE), item(), filing_rule=True) == "OK"
    assert Q._filing_unit_rule(key(LEAD), item(), filing_rule=True) == "OK"
    assert Q._filing_unit_rule(key(BULLET), item(), filing_rule=True) == "OK"
    # a fragment is still not a unit or a sentence
    assert Q._filing_unit_rule(key("a global semiconductor leader, today announced financial results"), item(), filing_rule=True) \
        == "R2_NOT_COMPLETE_UNIT"
    # a paragraph without a full stop (a deck cover line) is not a sentence
    assert Q._filing_unit_rule(key("Second Quarter 2026 Compared to the Second Quarter 2025"), SimpleNamespace(
        canonical_url=URL), filing_rule=True) == "R2_NOT_COMPLETE_UNIT"
    # part of the title is not the title
    assert Q._filing_unit_rule(key("Analog Devices Reports Record Fiscal Second Quarter"), item(), filing_rule=True) \
        == "R2_NOT_COMPLETE_UNIT"
    # the flattened excerpt alone would reject the lead sentence (it follows a headline with no full stop)
    flat = key(excerpt())
    assert not Q._starts_at_sentence(flat, flat.find(key(LEAD)))


def test_filing_rule_is_off_by_default_and_ignores_environment(store, monkeypatch):
    monkeypatch.setenv("CATALYST_V2_RULES", "all")
    assert Q._filing_unit_rule(Q._match_key(LEAD), item("https://www.reuters.com/x")) is None
    assert Q._filing_unit_rule(Q._match_key(LEAD), item()) is None
    assert Q._filing_unit_rule(Q._match_key(LEAD), item(), filing_rule=True) == "OK"


def test_exhibit_without_units_keeps_the_news_rule(tmp_path, monkeypatch):
    path = tmp_path / "empty.sqlite3"
    sqlite3.connect(path).execute(sec_structure.TABLE)
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(path))
    monkeypatch.setenv("CATALYST_V2_RULES", "all")
    assert Q._filing_unit_rule(Q._match_key(LEAD), item(), filing_rule=True) is None


def test_filing_quote_and_title_reads_propagate_store_failure(tmp_path, monkeypatch):
    from catalyst_data.storage.errors import StoreReadError

    path = tmp_path / "missing-units-table.sqlite3"
    sqlite3.connect(path).close()
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(path))
    with pytest.raises(StoreReadError):
        Q._filing_unit_rule(Q._match_key(LEAD), item(), filing_rule=True)
    with pytest.raises(StoreReadError):
        sec_structure.title_for(URL)


def test_filing_fact_is_metadata_only():
    fact = E.filing_fact("ANALOG DEVICES INC", "ADI", {
        "accession": "0000006281-26-000050", "accepted_at": datetime(2026, 5, 20, 11, 1, tzinfo=UTC),
        "items": ["2.02", "9.01"], "form": "8-K"})
    assert fact == ("Filing fact: ANALOG DEVICES INC (ADI) filed a Form 8-K with the SEC, accepted 2026-05-20 07:01 ET "
                    "(accession 0000006281-26-000050), reporting Item 2.02 Results of Operations and Financial "
                    "Condition; Item 9.01 Financial Statements and Exhibits.")


def test_fallback_quotes_the_headline_with_the_rule(store):
    prior, close = datetime(2026, 5, 19, 20, tzinfo=UTC), datetime(2026, 5, 20, 20, tzinfo=UTC)
    filings = [{"accession": "0000006281-26-000050", "accepted_at": datetime(2026, 5, 20, 11, tzinfo=UTC),
                "items": ["2.02", "9.01"], "form": "8-K"}]
    exhibit = {"evidence_id": "e1", "canonical_url": URL, "excerpt_text": excerpt()}
    kw = dict(tier="T3", filings=filings, z=-3.5, prior_close=prior, close=close, pack_items=[exhibit])
    off = E.decide(**kw)
    on = E.decide(**kw, title_for=sec_structure.title_for)
    assert off["quote_unit"] == "first_sentence"
    assert on["fire"] and on["quote_unit"] == "title" and on["quote"] == TITLE
