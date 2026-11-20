"""v2 engine, graphs and llm layers: rule edges and the properties the design requires.

- the gate with hooks off equals the frozen desk gate;
- event merging (cluster_events) does not depend on record order;
- replay is deterministic (same key, same order; a missing answer raises; misses are recorded);
- within a stage an item is extracted once, and an identical event-date request is answered once per run.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from catalyst_agents.attribution.extraction import (
    AboutTicker,
    EventTiming,
    EventType,
    Extraction,
    GateConfig,
    Session,
    cluster_events,
    select_and_gate,
)
from catalyst_agents.engine import earnings as E
from catalyst_agents.engine import event_date as ED
from catalyst_agents.engine import gate as G
from catalyst_agents.engine import recency as R
from catalyst_agents.engine.rules import V2Rules

UTC = timezone.utc
PRIOR_CLOSE = datetime(2026, 4, 9, 20, 0, tzinfo=UTC)   # Thursday close (EDT)
CLOSE = datetime(2026, 4, 10, 20, 0, tzinfo=UTC)        # Friday close


def item(eid: str, *, host: str = "reuters.com", at: str = "2026-04-10T14:00:00Z", text: str = "", cls="reported_news"):
    return {"evidence_id": eid, "canonical_url": f"https://www.{host}/{eid}", "publisher": host, "eligible_at": at,
            "excerpt_text": text, "source_class": cls, "content_state": "FULL_TEXT"}


def ext(summary: str, *, etype=EventType.ANALYST_ACTION, scope=AboutTicker.ISSUER, attribution: str = "",
        event: str = "x happened", timing=EventTiming.BEFORE_OPEN):
    return Extraction(about_ticker=scope, session=Session.THIS_SESSION, event_type=etype, event_summary=summary,
                      event_timing=timing, explicit_attribution=bool(attribution), attribution_quote=attribution,
                      event_quote=event, mechanism_quote="", is_preview_or_evergreen=False)


OBS = {"market_comove": {"direction_relation": "OPPOSITE_DIRECTION", "band": "DIVERGENT"},
       "sector_comove": {"direction_relation": "OPPOSITE_DIRECTION", "band": "DIVERGENT"},
       "peer_comove": None, "target_return": -5.0, "commodity_return": None}
FACTS = {"observation": OBS, "session_close": CLOSE}


def records():
    return [
        (item("a", host="reuters.com"), ext("UBS downgrades ServiceNow to neutral", attribution="shares fell after UBS downgraded")),
        (item("b", host="cnbc.com"), ext("UBS downgrade of ServiceNow neutral rating", attribution="fell on the UBS downgrade")),
        (item("c", host="wsj.com"), ext("ServiceNow announces acquisition of a startup", etype=EventType.DEAL_MA)),
        (item("d", host="ft.com"), ext("Fed holds rates steady", etype=EventType.MACRO, scope=AboutTicker.GROUP)),
    ]


# -- gate --------------------------------------------------------------------------------------------------------
def test_gate_with_hooks_off_is_the_frozen_gate():
    clusters = cluster_events(records())
    frozen = select_and_gate(clusters, FACTS, GateConfig("desk"))
    for got in (G.select_and_gate(clusters, FACTS, GateConfig("desk")), G.gate(clusters, FACTS)):
        assert (got.publish, got.reason_code, got.basis, got.diagnostics) == \
            (frozen.publish, frozen.reason_code, frozen.basis, frozen.diagnostics)
        assert got.primary.members == frozen.primary.members


def test_veto_ends_the_stage_instead_of_falling_through():
    clusters = cluster_events(records())
    frozen = select_and_gate(clusters, FACTS, GateConfig("desk"))
    assert frozen.publish
    vetoed = G.gate(clusters, FACTS, veto=lambda c: c is frozen.primary or c.members == frozen.primary.members)
    assert not vetoed.publish and vetoed.reason_code == G.STALE_REASON


# -- event merging is order-insensitive -----------------------------------------------------------------------
def test_cluster_events_does_not_depend_on_record_order():
    rng = random.Random(7)
    words = ["ubs", "downgrade", "servicenow", "rating", "neutral", "fed", "rates", "deal", "startup", "guidance"]
    recs = []
    for n in range(14):
        etype = rng.choice([EventType.ANALYST_ACTION, EventType.DEAL_MA, EventType.EARNINGS])
        recs.append((item(f"e{n:02d}", host=f"h{n % 5}.com"), ext(" ".join(rng.sample(words, 4)), etype=etype)))

    def partition(rs):
        return sorted(sorted(str(i["evidence_id"]) for i, _ in c.members) for c in cluster_events(rs))

    base = partition(recs)
    for _ in range(25):
        shuffled = recs[:]
        rng.shuffle(shuffled)
        assert partition(shuffled) == base


# -- event dates ---------------------------------------------------------------------------------------------------
def answer(**kw):
    return json.dumps({"event_date": None, "precision": "DAY", "time_et": None, "date_phrase": "", **kw})


@pytest.mark.parametrize("phrase,day,status", [
    ("on April 8", "2026-04-08", "VERIFIED"),
    ("earlier today", "2026-04-10", "VERIFIED"),
    ("On Thursday and Friday", "2026-04-10", "VERIFIED"),
    ("on Thursday", "2026-04-08", "REJECTED:weekday"),
    ("on April 8", "2026-04-07", "REJECTED:month_day"),
    ("not in the text", "2026-04-08", "REJECTED:phrase_not_in_excerpt"),
    ("on April 8", "2026-04-11", "REJECTED:after_publication"),
])
def test_event_date_check(phrase, day, status):
    it = item("a", at="2026-04-10T15:00:00Z",
              text="Shares fell on April 8 and on Thursday and Friday; earlier today the bank said so.")
    assert ED.check(answer(event_date=day, date_phrase=phrase), it)["status"] == status


def test_month_day_without_year_resolves_to_latest_occurrence_before_publication():
    it = item("a", at="2026-04-10T15:00:00Z", text="The event happened April 8.")

    current_year = ED.check(answer(event_date="2026-04-08", date_phrase="April 8"), it)
    prior_year = ED.check(answer(event_date="2025-04-08", date_phrase="April 8"), it)

    assert current_year["status"] == "VERIFIED"
    assert prior_year["status"] == "REJECTED:month_day_year"
    assert prior_year["date"] is None


def test_explicit_year_month_day_keeps_existing_verification_behavior():
    it = item("a", at="2026-04-10T15:00:00Z", text="The event happened April 8, 2025.")

    result = ED.check(answer(event_date="2025-04-08", date_phrase="April 8, 2025"), it)

    assert result["status"] == "VERIFIED"
    assert result["date"] == "2025-04-08"


def test_no_phrase_means_no_date():
    assert ED.check(answer(event_date="2026-04-10"), item("a", text="x"))["status"] == "NONE"


# -- recency -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("day,time_et,inside", [
    ("2026-04-10", None, True), ("2026-04-10", "16:30", False), ("2026-04-09", None, True),
    ("2026-04-09", "17:00", True), ("2026-04-09", "10:00", False), ("2026-04-08", None, False),
])
def test_window(day, time_et, inside):
    assert R.in_window(day, time_et, PRIOR_CLOSE, CLOSE) is inside


def test_text_stale_needs_a_verified_date_and_none_inside():
    (cl,) = [c for c in cluster_events(records()[:2])]
    old = {"date": "2026-04-01", "status": "VERIFIED"}
    assert R.text_stale(cl, {"a": old}, PRIOR_CLOSE, CLOSE)
    assert not R.text_stale(cl, {"a": old, "b": {"date": "2026-04-10", "status": "VERIFIED"}}, PRIOR_CLOSE, CLOSE)
    assert not R.text_stale(cl, {"a": {"date": "2026-04-01", "status": "UNCHECKED"}}, PRIOR_CLOSE, CLOSE)
    assert not R.text_stale(cl, {}, PRIOR_CLOSE, CLOSE)


def test_wrong_year_for_month_day_does_not_trigger_recency_veto():
    cluster, = cluster_events(records()[:2])
    it = item("a", at="2026-04-10T15:00:00Z", text="The event happened April 8.")
    date_result = ED.check(answer(event_date="2025-04-08", date_phrase="April 8"), it)

    assert date_result["status"] == "REJECTED:month_day_year"
    assert not R.text_stale(cluster, {"a": date_result}, PRIOR_CLOSE, CLOSE)


def test_earnings_anchor():
    (cl,) = cluster_events([(item("a"), ext("ServiceNow quarterly results beat", etype=EventType.EARNINGS))])
    filing = lambda at: {"accession": "1", "accepted_at": at, "items": ["2.02"]}  # noqa: E731
    def provider(rows):
        return lambda start, end: [f for f in rows if start < f["accepted_at"] <= end]
    inside = provider([filing(CLOSE - timedelta(hours=12))])
    before = provider([filing(PRIOR_CLOSE - timedelta(days=9))])
    assert R.earnings_anchor(cl, inside, PRIOR_CLOSE, CLOSE) is False
    assert R.earnings_anchor(cl, before, PRIOR_CLOSE, CLOSE) is True
    assert R.earnings_anchor(cl, provider([]), PRIOR_CLOSE, CLOSE) is None
    (deal,) = cluster_events([(item("b"), ext("ServiceNow buys a startup", etype=EventType.DEAL_MA))])
    assert R.earnings_anchor(deal, before, PRIOR_CLOSE, CLOSE) is None


# -- earnings fallback -------------------------------------------------------------------------------------------
RELEASE = ("Contact: Investor Relations, ir@example.com\n\n• Q1 revenue grew 16% year over year and operating income grew "
           "18%.\n\nSANTA CLARA, Calif. – April 9, 2026 – Example Inc. (NASDAQ: EX) today reported results for the "
           "first quarter.")


def test_first_sentence_prefers_the_today_reported_paragraph():
    assert E.first_sentence(RELEASE).startswith("SANTA CLARA, Calif.")
    assert E.first_sentence("• Q1 revenue grew 16% year over year and operating income grew 18% overall.") \
        .startswith("Q1 revenue")
    long = "Example Reports Results; " + "x" * 500
    assert len(E.first_sentence(long)) <= 400


def test_fallback_conditions():
    acc = "0001234567-26-000123"
    filings = [{"accession": acc, "accepted_at": CLOSE - timedelta(hours=10), "items": ["2.02", "9.01"]}]
    exhibit = item("sec", host="sec.gov", text=RELEASE, cls="issuer_disclosure")
    exhibit["canonical_url"] = f"https://www.sec.gov/Archives/edgar/data/1/{acc.replace('-', '')}/ex99-1.htm"
    kw = dict(prior_close=PRIOR_CLOSE, close=CLOSE)
    assert E.decide(tier="T3", filings=filings, z=3.1, pack_items=[exhibit], **kw)["fire"]
    assert E.decide(tier="T2", filings=filings, z=3.1, pack_items=[exhibit], **kw)["reason"] == "tier_not_T3"
    assert E.decide(tier="T3", filings=filings, z=1.5, pack_items=[exhibit], **kw)["reason"] == "z_below_2"
    assert E.decide(tier="T3", filings=filings, z=None, pack_items=[exhibit], **kw)["reason"] == "no_research_z"
    assert E.decide(tier="T3", filings=filings, z=3.1, pack_items=[], **kw)["reason"] == "exhibit_not_in_pack"
    other = filings + [{"accession": "0009-26-1", "accepted_at": CLOSE - timedelta(hours=9), "items": ["5.02"]}]
    assert E.decide(tier="T3", filings=other, z=3.1, pack_items=[exhibit], **kw)["reason"] == "other_material_8k"
    late = [{**filings[0], "accepted_at": CLOSE + timedelta(hours=1)}]
    assert E.decide(tier="T3", filings=late, z=3.1, pack_items=[exhibit], **kw)["reason"] == "no_item_202"


def test_rules_parse():
    assert not V2Rules.parse("").any
    assert V2Rules.parse("all") == V2Rules(True, True, True, True, True)
    assert V2Rules.parse("v2") == V2Rules(True, True, True, True, False)
    assert V2Rules.parse("v2,filing") == V2Rules.parse("all")
    with pytest.raises(ValueError):
        V2Rules.parse("anchor")
    with pytest.raises(ValueError):
        V2Rules.parse("bogus")


# -- replay ------------------------------------------------------------------------------------------------------
class FakeClient:
    model_name = "m"
    capability_metadata = {"x": 1}

    def __init__(self):
        self.calls = 0

    def bind(self, **_):
        return self

    def invoke(self, messages, **_):
        from catalyst_agents.llm import ChatResult
        self.calls += 1
        return ChatResult(content=f"answer {self.calls}", usage_metadata={"input_tokens": 3, "output_tokens": 1})


def test_replay_is_deterministic(tmp_path, monkeypatch):
    from catalyst_agents.llm import record
    path = tmp_path / "r.jsonl"
    monkeypatch.setenv("CATALYST_LLM_RECORD", str(path))
    rec = record.wrap(FakeClient())
    first = [rec.invoke("same").content, rec.invoke("same").content, rec.bind(max_tokens=5).invoke("other").content]
    monkeypatch.delenv("CATALYST_LLM_RECORD")
    for _ in range(2):
        record._STORES.clear()
        monkeypatch.setenv("CATALYST_LLM_REPLAY", str(path))
        fake = FakeClient()
        rep = record.wrap(fake)
        assert [rep.invoke("same").content, rep.invoke("same").content,
                rep.bind(max_tokens=5).invoke("other").content] == first
        assert fake.calls == 0 and rep.capability_metadata == {"x": 1}
        with pytest.raises(record.ReplayMiss):
            rep.invoke("same")
    misses = tmp_path / "m.jsonl"
    record._STORES.clear()
    monkeypatch.setenv("CATALYST_LLM_RECORD_MISSES", str(misses))
    fake = FakeClient()
    rep = record.wrap(fake)
    rep.invoke("same"), rep.invoke("same")
    assert rep.invoke("new request").content == "answer 1" and fake.calls == 1
    assert len(misses.read_text().splitlines()) == 1


# -- no repeated model call for the same item within a stage ----------------------------------------------------
def test_extract_once_per_item_and_event_dates_once_per_request():
    from catalyst_agents.graphs.desk_stage import DeskStage
    from catalyst_agents.llm import ChatResult
    from catalyst_agents.nodes.desk_decision import extract_items

    class Counting:
        def __init__(self):
            self.seen = []

        def bind(self, **_):
            return self

        def invoke(self, messages, **_):
            self.seen.append(json.dumps(messages, sort_keys=True))
            return ChatResult(content=json.dumps({"event_date": None, "precision": "NONE", "time_et": None,
                                                  "date_phrase": ""}))

    llm = Counting()
    inventory = [item(f"i{n}", text=f"text {n}") | {"excerpt_text": f"text {n}"} for n in range(5)]

    class TI:
        session_date = "2026-04-10"
    extract_items(inventory, facts={"ticker": "X", "observation": {}}, temporal_identity=TI(), quote_policy="pre_close_v1",
                  llm=llm, provider_budget=None, provider_identity=None, model_identity=None, run_deadline_epoch_ms=None)
    assert len(llm.seen) == 5 and len(set(llm.seen)) == 5

    llm2 = Counting()
    stage = DeskStage(llm=llm2, provider_budget=None, provider_identity=None, model_identity=None,
                      run_deadline_epoch_ms=None, rules=V2Rules.parse("recency"), ticker="X")
    recs = records()
    state = {"records": recs, "calls": {"logical_calls": 0, "provider_attempts": 0}, "attempts": [], "usages": [],
             "company_name": "X"}
    stage.date(state)
    stage.date(state)  # stage 2 asks the same requests again: answered from the run's cache
    assert len(llm2.seen) == len(R.usable_ids(recs)) == len(set(llm2.seen))
