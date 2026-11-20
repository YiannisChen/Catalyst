from types import SimpleNamespace

from catalyst_agents.attribution.reported_reason import make_reported_reason


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def bind(self, **kwargs):
        return self

    def invoke(self, prompt):
        return SimpleNamespace(content=self.payload)


def _find(payload, *, event_date="2026-04-28", timing="INTRADAY", excerpt=None, earlier_story=None):
    llm = FakeLLM(payload)
    find = make_reported_reason(llm, budget=None, provider=None, model_id=None, deadline_epoch_ms=None,
                                earlier_story=earlier_story)
    items = [SimpleNamespace(
        excerpt_text=excerpt or "Acme said revenue grew 20% after strong demand.",
        evidence_id="e1", publisher="Reuters", eligible_at="2026-04-28T14:00:00Z",
        source_time_at=None, canonical_url="https://example.test/story",
    )]
    result = find(
        ticker="ACME", company_name="Acme", session_date="2026-04-28",
        stock_pct=4.2, peers_pct=1.2, sector_ticker="XLY", sector_pct=0.4, items=items,
    )
    return find, result


def _payload(*, found=True, quote="Acme said revenue grew 20% after strong demand.",
             event_date="2026-04-28", timing="INTRADAY", article=1, scope="COMPANY"):
    import json
    return json.dumps({
        "found": found, "article": article, "quote": quote, "cause": "strong demand",
        "scope": scope, "event_date": event_date, "event_timing": timing,
        "confidence": "high",
    })


def test_found_verbatim_returns_source_metadata():
    find, result = _find(_payload())
    assert result["quote"].startswith("Acme said")
    assert result["evidence_id"] == "e1"
    assert find.last_status == "found"


def test_non_verbatim_quote_is_rejected():
    find, result = _find(_payload(quote="Acme had revenue growth"))
    assert result is None
    assert find.last_status == "quote_not_verbatim"


def test_stale_event_is_rejected():
    find, result = _find(_payload(event_date="2026-04-24"))
    assert result is None
    assert find.last_status == "stale_event"


def test_previous_trading_day_evening_is_accepted():
    find, result = _find(_payload(event_date="2026-04-27", timing="AFTER_CLOSE"))
    assert result is not None
    assert find.last_status == "found"


def test_same_day_after_close_is_rejected():
    find, result = _find(_payload(timing="AFTER_CLOSE"))
    assert result is None
    assert find.last_status == "after_close"


def test_found_false_returns_none():
    find, result = _find(_payload(found=False))
    assert result is None
    assert find.last_status == "none"


def test_company_quote_must_name_the_company():
    text = "Revenue grew 20% after strong demand."
    find, result = _find(_payload(quote=text), excerpt=text)
    assert result is None
    assert find.last_status == "wrong_subject"


def test_company_quote_about_another_stock_move_is_rejected():
    text = "Shares of Intel (NASDAQ:INTC) jumped 14% after a report that Acme (NASDAQ:ACME) may use its foundry."
    find, result = _find(_payload(quote=text), excerpt=text)
    assert result is None
    assert find.last_status == "wrong_subject"


def test_name_matching():
    from catalyst_agents.attribution.reported_reason import (
        names_company,
        other_stock_move,
    )

    assert names_company("Bank of America said costs rose", ticker="BAC", company_name="Bank America")
    assert not names_company("The bank said costs rose", ticker="BAC", company_name="Bank America")
    assert names_company("Google cut prices", ticker="GOOGL", company_name="Alphabet")
    assert names_company("Goldman Sachs Group Inc. (NYSE: GS ) rose", ticker="GS", company_name="Goldman Sachs")
    assert not other_stock_move("Goldman Sachs Group Inc. (NYSE: GS ) rose 2%", ticker="GS")
    assert names_company("Meta cut its capex plan", ticker="META", company_name="Platforms")
    assert not names_company("now the stock fell", ticker="NOW", company_name="Servicenow")


def test_story_that_ran_before_the_previous_session_is_rejected():
    seen = {}

    def earlier(**kwargs):
        seen.update(kwargs)
        return {"published_at": "2026-04-20T21:08:00Z", "title": "Acme demand story"}

    find, result = _find(_payload(), earlier_story=earlier)
    assert result is None
    assert find.last_status == "stale_story"
    assert seen["before"].isoformat() == "2026-04-27T04:00:00+00:00"  # previous session, 00:00 New York
    assert seen["since"].isoformat() == "2026-04-13"


def test_group_reason_skips_the_earlier_story_check():
    def earlier(**kwargs):
        raise AssertionError("not called for group reasons")

    find, result = _find(_payload(scope="GROUP"), earlier_story=earlier)
    assert result is not None
    assert find.last_status == "found"


def test_same_story_matching():
    from catalyst_agents.attribution.reported_reason import same_story

    exclude = {"aapl", "apple"}
    quote = ("Apple announced that longtime CEO Tim Cook will step down on September 1, 2026 to become "
             "executive chairman, with company veteran John Ternus taking over.")
    assert same_story(quote, "Apple CEO Tim Cook To Step Down In September, John Ternus Will Take Over",
                      exclude=exclude)
    assert not same_story(quote, "Apple CEO Tim Cook Buys Nike Stock", exclude=exclude)
    note = "Apple may absorb price increases without a hit to demand, according to a note from Morgan Stanley."
    assert not same_story(note, "Morgan Stanley says it was wrong on Qualcomm price target", exclude=exclude)


def test_corpus_lookup_without_a_store_returns_none(monkeypatch, tmp_path):
    from datetime import date, datetime, timezone

    from catalyst_agents.attribution.reported_reason import corpus_earlier_story

    monkeypatch.setenv("CATALYST_DB_PATH", str(tmp_path / "missing.sqlite3"))
    assert corpus_earlier_story(ticker="ACME", company_name="Acme", quote="Acme news",
                                before=datetime(2026, 4, 27, 4, tzinfo=timezone.utc),
                                since=date(2026, 4, 13)) is None


def test_corpus_lookup_finds_the_earlier_article(monkeypatch, tmp_path):
    import sqlite3
    from datetime import date, datetime, timezone

    from catalyst_agents.attribution.reported_reason import corpus_earlier_story

    db = tmp_path / "corpus.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE articles (article_id TEXT, published_utc TEXT, title TEXT, description TEXT)")
    conn.execute("CREATE TABLE article_tickers (article_id TEXT, ticker TEXT, reference_date TEXT)")
    rows = [("a1", "2026-04-20T21:08:00Z", "Acme CEO Tim Cook To Step Down, John Ternus Will Take Over", None),
            ("a2", "2026-04-27T13:00:00Z", "Acme CEO Tim Cook To Step Down, John Ternus Will Take Over", None)]
    conn.executemany("INSERT INTO articles VALUES (?,?,?,?)", rows)
    conn.executemany("INSERT INTO article_tickers VALUES (?,?,?)",
                     [("a1", "ACME", "2026-04-20"), ("a2", "ACME", "2026-04-27")])
    conn.commit()
    conn.close()
    monkeypatch.setenv("CATALYST_DB_PATH", str(db))
    quote = "Acme said CEO Tim Cook will step down and John Ternus will take over."
    hit = corpus_earlier_story(ticker="ACME", company_name="Acme", quote=quote,
                               before=datetime(2026, 4, 27, 4, tzinfo=timezone.utc), since=date(2026, 4, 13))
    assert hit and hit["published_at"].startswith("2026-04-20")
    late = corpus_earlier_story(ticker="ACME", company_name="Acme", quote=quote,
                                before=datetime(2026, 4, 20, 4, tzinfo=timezone.utc), since=date(2026, 4, 13))
    assert late is None


def test_quote_that_only_describes_the_move_is_rejected():
    text = "Acme stock jumped nearly 4% on Monday, finishing at $445."
    find, result = _find(_payload(quote=text), excerpt=text)
    assert result is None
    assert find.last_status == "move_only"


def test_move_with_a_cause_is_kept():
    from catalyst_agents.attribution.reported_reason import describes_move_only

    assert not describes_move_only("Shares rallied over 4% during Tuesday's trading session on the layoff news.")
    assert not describes_move_only("Acme shares are trading lower in sympathy with Beta, which cut guidance.")
    assert not describes_move_only("Acme rose 3% after it raised its outlook.")
    assert not describes_move_only("Acme expanded its partnership with Beta.")
