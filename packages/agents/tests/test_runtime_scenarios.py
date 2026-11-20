"""Fault-injection checks for docs/design/runtime_scenarios.md (backend-freeze-v2.1, every rule on).

Read-only: each test fixes what the frozen code does in one runtime situation that neither code reading nor a recorded
run settled. No production code changes; no paid service is called (stubs and temporary stores only).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from catalyst_agents.attribution.extraction import (
    AboutTicker,
    EventTiming,
    EventType,
    Extraction,
    Session,
    _make_cluster,
)
from catalyst_agents.engine import earnings as E
from catalyst_agents.engine import recency as R
from catalyst_agents.engine.rules import V2Rules

PRIOR = datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc)
CLOSE = datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc)


def _item(eid: str = "e1", url: str = "https://news.example.com/a", at: datetime = CLOSE - timedelta(hours=3),
          text: str = "Acme shares fell after the company cut its outlook on Tuesday.") -> dict:
    return {"evidence_id": eid, "canonical_url": url, "eligible_at": at.isoformat(), "excerpt_text": text,
            "source_class": "news", "publisher": "Example", "content_state": "FULL_TEXT"}


def _earnings_ext() -> Extraction:
    return Extraction(about_ticker=AboutTicker.ISSUER, session=Session.THIS_SESSION, event_type=EventType.EARNINGS,
                      event_summary="Acme reported quarterly results", event_timing=EventTiming.BEFORE_OPEN,
                      explicit_attribution=True, attribution_quote="Acme shares fell after results.",
                      event_quote="Acme reported results.")


# -- S5 / S7 / S11: the SEC submissions index ---------------------------------------------------------------------

def test_sec_index_offline_without_cache_is_a_store_failure(tmp_path, monkeypatch):
    """A missing offline cache cannot establish that the issuer has no filings."""
    from catalyst_data.ingestion.collectors import sec, sec_filings
    from catalyst_data.storage.errors import StoreReadError
    monkeypatch.setenv("CATALYST_SEC_OFFLINE", "1")
    monkeypatch.setenv("CATALYST_SEC_SUBMISSIONS_DIR", str(tmp_path / "subs"))
    monkeypatch.setattr(sec, "cik_for", lambda ticker, tickers_json=None: "0000000001")
    with pytest.raises(StoreReadError, match="SEC submissions cache"):
        sec_filings.filings_8k("ACME")


def test_sec_index_online_fetch_failure_falls_back_to_cache_or_raises(tmp_path, monkeypatch):
    """Online fetch failures may use a readable stale cache; without one they are explicit failures."""
    import requests
    from catalyst_data.ingestion.collectors import sec, sec_filings
    monkeypatch.delenv("CATALYST_SEC_OFFLINE", raising=False)
    monkeypatch.setenv("SEC_USER_AGENT", "test agent test@example.com")
    monkeypatch.setenv("CATALYST_SEC_SUBMISSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(sec, "cik_for", lambda ticker, tickers_json=None: "0000000001")
    monkeypatch.setattr(sec, "SEC_INTERVAL_S", 0)
    calls = []

    def down(*args, **kwargs):
        calls.append(args)
        raise requests.ConnectionError("sec down")

    monkeypatch.setattr(requests, "get", down)
    from catalyst_data.storage.errors import StoreReadError
    with pytest.raises(StoreReadError, match="load SEC submissions"):
        sec_filings.filings_8k("ACME")
    cached = {"name": "ACME CORP", "filings": {"recent": {
        "form": ["8-K"], "acceptanceDateTime": ["2026-01-06T12:00:00.000Z"], "items": ["2.02,9.01"],
        "accessionNumber": ["0000000001-26-000001"]}}}
    path = tmp_path / "0000000001.json"
    path.write_text(json.dumps(cached))
    old = time.time() - 7 * 3600  # older than REFRESH_HOURS: a refetch is attempted and fails
    os.utime(path, (old, old))
    filings = sec_filings.filings_8k("ACME")
    assert [f["accession"] for f in filings] == ["0000000001-26-000001"]
    assert sec_filings.company("ACME") == "ACME CORP"
    assert len(calls) >= 2


def test_sec_index_reads_only_the_recent_block(tmp_path, monkeypatch):
    """A submissions page that lists an older archive fails closed when that archive is not cached.

    The data line reads ``filings.files``. Skipping a missing page would hide an older 8-K and look like
    "no filing". Offline, the missing archive is a store failure.
    """
    from catalyst_data.ingestion.collectors import sec, sec_filings
    from catalyst_data.storage.errors import StoreReadError
    monkeypatch.setenv("CATALYST_SEC_OFFLINE", "1")
    monkeypatch.setenv("CATALYST_SEC_SUBMISSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(sec, "cik_for", lambda ticker, tickers_json=None: "0000000001")
    (tmp_path / "0000000001.json").write_text(json.dumps({"name": "BANK", "filings": {
        "recent": {"form": ["424B2"], "acceptanceDateTime": ["2026-01-20T12:00:00.000Z"], "items": [""],
                   "accessionNumber": ["0000000001-26-000900"]},
        "files": [{"name": "CIK0000000001-submissions-001.json", "filingFrom": "2025-01-01",
                   "filingTo": "2026-01-19"}]}}))
    with pytest.raises(StoreReadError, match="SEC submissions"):
        sec_filings.between("BANK", PRIOR, CLOSE)


def test_missing_sec_company_tickers_file(tmp_path, monkeypatch):
    """S3: without sec_company_tickers.json next to the bodies store the CIK lookup raises (the run then fails as
    SYSTEM_ERROR wherever the SEC index is read: anchor, fallback, fact check), the subject gate is off, and the
    company name falls back to the ticker."""
    from catalyst_data.ingestion.collectors import sec, sec_filings, web_discovery_v2
    store = tmp_path / "bodies.sqlite3"
    sqlite3.connect(store).close()
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(store))
    with pytest.raises(FileNotFoundError):
        sec.cik_for("ACME")
    with pytest.raises(FileNotFoundError):
        sec_filings.between("ACME", PRIOR, CLOSE, item="2.02")
    monkeypatch.setattr(web_discovery_v2, "_SEC_NAMES", None)
    monkeypatch.setattr(web_discovery_v2.Path, "is_file", lambda self: False)
    assert web_discovery_v2._company("ZZZQ") == "ZZZQ"


# -- S4 / S5: extractor and event-dater failures -------------------------------------------------------------------

class _Raising:
    def __init__(self, exc: BaseException):
        self.exc, self.calls = exc, 0

    def invoke(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


def _facts() -> dict:
    return {"ticker": "ACME", "company_name": "Acme", "session_date": "2026-01-06", "target_return": -5.0,
            "market_return": None, "sector_return": None, "observation": {}}


def test_extractor_timeout_or_transport_failure_only_loses_that_item():
    """S4: a model timeout (not a retryable type) and a transport failure (retried once) both leave the item invalid
    (extraction_failed:...); the stage goes on with the other items."""
    from catalyst_agents.nodes.desk_decision import extract_items
    from catalyst_agents.runtime.provider_capability import ModelTransportFailure
    ti = SimpleNamespace(session_date="2026-01-06", information_window_start_at=PRIOR, cutoff_at=CLOSE,
                         session_open_at=CLOSE - timedelta(hours=6, minutes=30), session_close_at=CLOSE)
    for exc, reason, calls in ((TimeoutError("read timed out"), "extraction_failed:TimeoutError", 1),
                               (ModelTransportFailure("502"), "extraction_failed:TechnicalRetryExhausted", 2)):
        llm = _Raising(exc)
        records, counts, _, _ = extract_items([_item()], facts=_facts(), temporal_identity=ti,
                                              quote_policy="pre_close_v1", llm=llm, provider_budget=None,
                                              provider_identity=None, model_identity=None, run_deadline_epoch_ms=None)
        assert records[0][1].valid is False and records[0][1].invalid_reason == reason
        assert llm.calls == calls


def test_extractor_past_deadline_makes_no_call():
    """S4/S13: past the run deadline the extractor is not called; the item is invalid (deadline_exceeded)."""
    from catalyst_agents.nodes.desk_decision import extract_items
    llm = _Raising(AssertionError("must not be called"))
    ti = SimpleNamespace(session_date="2026-01-06")
    records, *_ = extract_items([_item()], facts=_facts(), temporal_identity=ti, quote_policy="pre_close_v1",
                                llm=llm, provider_budget=None, provider_identity=None, model_identity=None,
                                run_deadline_epoch_ms=int(time.time() * 1000) - 1)
    assert records[0][1].invalid_reason == "deadline_exceeded" and llm.calls == 0


def test_event_dater_failure_leaves_the_record_undated_and_the_veto_off():
    """S5: a failed or skipped dating call gives no VERIFIED date, so the text recency veto does not fire (fail open)."""
    from catalyst_agents.graphs.desk_stage import DeskStage
    item, ext = _item(), _earnings_ext()
    for llm, deadline, status in ((_Raising(RuntimeError("boom")), None, "REJECTED:call_failed:RuntimeError"),
                                  (_Raising(AssertionError("no call")), int(time.time() * 1000) - 1,
                                   "REJECTED:deadline_exceeded")):
        stage = DeskStage(llm=llm, provider_budget=None, provider_identity=None, model_identity=None,
                          run_deadline_epoch_ms=deadline, rules=V2Rules.parse("recency"), ticker="ACME")
        out = stage.date({"records": [(item, ext)], "calls": {"logical_calls": 0, "provider_attempts": 0},
                          "attempts": [], "usages": [], "company_name": "Acme"})
        assert out["dates"]["e1"]["status"] == status
        cluster = _make_cluster([(item, ext)])
        assert R.text_stale(cluster, out["dates"], PRIOR, CLOSE) is False


def test_model_outage_turns_a_stage_into_an_ordinary_no_eligible_cause():
    """S4/S13 doubt: with the model down (every extraction fails) a stage does not fail; it abstains with
    NO_ELIGIBLE_CAUSE, the failures are visible only as invalid extractions in the desk_decision artifact, and the
    user-facing reason becomes NO_PRIMARY_PROPOSED."""
    from catalyst_agents.attribution.answer_snapshot import derive_abstention_reason
    from catalyst_agents.graphs.desk_stage import DeskStage
    from catalyst_agents.runtime.provider_capability import ModelTransportFailure
    ti = SimpleNamespace(session_date="2026-01-06", information_window_start_at=PRIOR, cutoff_at=CLOSE,
                         session_open_at=CLOSE - timedelta(hours=6, minutes=30), session_close_at=CLOSE)
    pack = SimpleNamespace(included_evidence_ids=("e1", "e2"), lead_only_evidence=(),
                           evidence_inventory=(_item("e1"), _item("e2", url="https://other.example.com/b")))
    observation = {"ticker": "ACME", "target_return": -5.0, "market_comove": None, "sector_comove": None}
    stage = DeskStage(llm=_Raising(ModelTransportFailure("connection refused")), provider_budget=None,
                      provider_identity=None, model_identity=None, run_deadline_epoch_ms=None,
                      rules=V2Rules.parse("all"), ticker="ACME")
    result = stage.run(pack, observation=observation, temporal_identity=ti, quote_policy="pre_close_v1",
                       company_name="Acme", market_facts={"session_close": CLOSE})
    assert result.gate_decision.reason_code == "NO_ELIGIBLE_CAUSE"
    assert {row["invalid_reason"] for row in result.extraction_results} == {"extraction_failed:TechnicalRetryExhausted"}
    trace = {"rounds": [{"desk_decision": result.artifact,
                         "model_reported": {"analyst": {"hypotheses": []},
                                            "verifier": {"called": False, "skipped_because": "no_primary"}},
                         "code_checks": {"quotes": [], "gates": []}}],
             "retrieval": {"included_in_pack": 2, "evidence_in_state": 2}}
    assert derive_abstention_reason(trace)["reason_code"] == "NO_PRIMARY_PROPOSED"


def test_issuer_earnings_and_guidance_are_two_clusters():
    """S6 (doc vs code): the unconditional issuer merge is within one event type only; an EARNINGS record and a
    GUIDANCE record of the same release stay two clusters, so the session has two material company events and the
    inferred-link basis (which needs a single one) is unavailable to either."""
    from catalyst_agents.attribution.extraction import (
        GateConfig,
        cluster_events,
        select_and_gate,
    )
    earnings = _earnings_ext()
    guidance = Extraction(about_ticker=AboutTicker.ISSUER, session=Session.THIS_SESSION, event_type=EventType.GUIDANCE,
                          event_summary="Acme cut its full-year outlook", event_timing=EventTiming.BEFORE_OPEN,
                          event_quote="Acme cut its outlook.")
    no_attr = Extraction(**{**earnings.__dict__, "explicit_attribution": False, "attribution_quote": ""})
    pr = {**_item("p1", url="https://acme.com/pr"), "source_class": "corporate_press_release"}
    clusters = cluster_events([(pr, no_attr), ({**pr, "evidence_id": "p2"}, guidance)])
    assert sorted(c.event_type.value for c in clusters) == ["EARNINGS", "GUIDANCE"]
    gate = select_and_gate(clusters, {"observation": {}, "session_close": CLOSE}, GateConfig("desk"))
    assert gate.publish is False and gate.reason_code == "NO_ELIGIBLE_CAUSE"
    single = select_and_gate(cluster_events([(pr, no_attr)]), {"observation": {}, "session_close": CLOSE},
                             GateConfig("desk"))
    assert single.publish and single.basis == "CONFIRMED_EVENT_INFERRED_LINK"


def test_no_inferred_company_cause_when_the_stock_moved_in_line_with_its_peers():
    """ORCL 07-14: no source states a link and the stock tracked its peers, so a product launch is not inferred."""
    from catalyst_agents.attribution.extraction import cluster_events
    from catalyst_agents.engine import gate as G
    earnings = _earnings_ext()
    no_attr = Extraction(**{**earnings.__dict__, "explicit_attribution": False, "attribution_quote": ""})
    pr = {**_item("p1", url="https://acme.com/pr"), "source_class": "corporate_press_release"}
    clusters = cluster_events([(pr, no_attr)])
    aligned = {"observation": {"peer_comove": {"band": "ALIGNED"}}, "session_close": CLOSE}
    assert G.gate(clusters, aligned).reason_code == "NO_ELIGIBLE_CAUSE"
    divergent = {"observation": {"peer_comove": {"band": "DIVERGENT"}}, "session_close": CLOSE}
    assert G.gate(clusters, divergent).basis == "CONFIRMED_EVENT_INFERRED_LINK"


def test_recency_veto_checks_the_primary_only_so_a_stale_secondary_is_published():
    """S7 doubt: the veto looks at the top cluster only; an eligible secondary whose verified date is before the window
    is still attached as SECONDARY."""
    from catalyst_agents.attribution.extraction import cluster_events
    from catalyst_agents.engine import gate as G
    fresh = {**_item("p1", url="https://acme.com/pr"), "source_class": "corporate_press_release"}
    old = _item("n1", url="https://news.example.com/upgrade")
    upgrade = Extraction(about_ticker=AboutTicker.ISSUER, session=Session.THIS_SESSION,
                         event_type=EventType.ANALYST_ACTION, event_summary="Broker upgraded Acme last month",
                         event_timing=EventTiming.UNKNOWN, explicit_attribution=True,
                         attribution_quote="Acme shares fell after the upgrade.", event_quote="Broker upgraded Acme.")
    clusters = cluster_events([(fresh, _earnings_ext()), (old, upgrade)])
    dates = {"p1": {"status": "VERIFIED", "date": "2026-01-06"}, "n1": {"status": "VERIFIED", "date": "2025-12-01"}}
    veto = lambda c: R.stale(c, dates=dates, prior_close=PRIOR, close=CLOSE, filings_202=None)  # noqa: E731
    decision = G.gate(clusters, {"observation": {}, "session_close": CLOSE}, veto=veto)
    assert decision.publish and decision.primary.event_type is EventType.EARNINGS
    assert decision.secondary is not None and decision.secondary.event_type is EventType.ANALYST_ACTION
    assert veto(decision.secondary) is True


# -- S9 / S11: v2.1 units, the fallback and the filing fact ----------------------------------------------------------

def test_exhibit_units_miss_is_not_cached_across_ingest(tmp_path, monkeypatch):
    """A normal missing row stays empty, and a subsequent ingest is visible without restart."""
    from catalyst_data.ingestion.collectors import sec_structure
    store = tmp_path / "bodies.sqlite3"
    conn = sqlite3.connect(store)
    conn.execute(sec_structure.TABLE)
    conn.commit()
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(store))
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99-1.htm"
    assert sec_structure.units_for(url) is None
    sec_structure.write(conn, url, "0000000001-26-000001",
                        {"units": [{"kind": "title", "text": "Acme Reports Fourth Quarter Results"}]})
    conn.commit()
    conn.close()
    assert sec_structure.units_for(url) == ({"kind": "title", "text": "Acme Reports Fourth Quarter Results"},)
    assert sec_structure.title_for(url) == "Acme Reports Fourth Quarter Results"


def test_fallback_ignores_material_items_on_the_same_filing_but_not_on_another():
    """S11: Item 5.02 filed together with 2.02 (PEP 2025-10-09) does not block the fallback; on a separate 8-K it does."""
    acc = "0000000001-26-000001"
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99-1.htm"
    text = ("Acme today reported fourth quarter results with revenue of $1.2 billion, up 8% from a year ago.\n\n"
            "More text follows here about the quarter and the outlook for the coming year.")
    pack = [{"evidence_id": "s1", "canonical_url": url, "excerpt_text": text}]
    same = [{"accession": acc, "accepted_at": CLOSE - timedelta(hours=10), "items": ["2.02", "5.02", "9.01"],
             "form": "8-K"}]
    assert E.decide(tier="T3", filings=same, z=2.5, prior_close=PRIOR, close=CLOSE, pack_items=pack)["fire"] is True
    other = same[:1] + [{"accession": "0000000001-26-000002", "accepted_at": CLOSE - timedelta(hours=9),
                         "items": ["5.02"], "form": "8-K"}]
    out = E.decide(tier="T3", filings=[{**same[0], "items": ["2.02"]}, other[1]], z=2.5, prior_close=PRIOR,
                   close=CLOSE, pack_items=pack)
    assert out["reason"] == "other_material_8k"


def test_research_z_short_history_is_none_and_a_missing_market_store_raises(tmp_path):
    """Short history/no SPY is legitimate no-data; a missing configured store is a failure.
    Production defaults the path to the relative data/m9/market_context.sqlite3 when CATALYST_MARKET_CONTEXT_DB is unset."""
    db = tmp_path / "market.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, close REAL, volume REAL)")
    days = [(datetime(2025, 9, 1) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(120)]
    for i, d in enumerate(days):
        conn.execute("INSERT INTO ohlcv VALUES ('SPY', ?, 100, ?, 1)", (d, 100 + i % 3))
        conn.execute("INSERT INTO ohlcv VALUES ('ACME', ?, 50, ?, 1)", (d, 50 + i % 5))
    conn.commit()
    conn.close()
    assert E.research_z(db, "ACME", days[-1]) is None          # 119 returns < MIN_OBS
    assert E.research_z(db, "ACME", "2026-03-02") is None      # no SPY bar on D -> ValueError -> None
    missing = tmp_path / "missing.sqlite3"
    from catalyst_data.storage.errors import StoreReadError
    with pytest.raises(StoreReadError, match="earnings research market returns"):
        E.research_z(missing, "ACME", days[-1])
    assert not missing.exists()                              # a missing file stays missing


def test_filing_fact_unverified_blocks_the_fallback(monkeypatch):
    """S11: the fact check fails when the accession is not in the issuer's own index or the exhibit is another
    filing's; the cascade then records fact_unverified and does not fire."""
    from catalyst_agents.graphs.cascade import Cascade
    from catalyst_data.ingestion.collectors import sec_filings
    acc = "0000000001-26-000001"
    decision = {"fire": True, "accession": acc}
    good = {"canonical_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99-1.htm"}
    me = SimpleNamespace(ctx=SimpleNamespace(ticker="ACME"))
    monkeypatch.setattr(sec_filings, "company", lambda t: "ACME CORP")
    monkeypatch.setattr(sec_filings, "filings_8k", lambda t: [])
    out = Cascade._verify_fact(me, decision, good)
    assert out["fact"] is None and out["fact_checks"]["accession_in_issuer_index"] is False
    monkeypatch.setattr(sec_filings, "filings_8k", lambda t: [
        {"accession": acc, "accepted_at": CLOSE - timedelta(hours=10), "items": ["2.02"], "form": "8-K"}])
    other = {"canonical_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000777/ex99-1.htm"}
    out = Cascade._verify_fact(me, decision, other)
    assert out["fact"] is None and out["fact_checks"]["exhibit_is_this_filing"] is False
    assert Cascade._verify_fact(me, decision, good)["fact"].startswith(E.FACT_LABEL)


# -- S10: stage two reads the web cache only ---------------------------------------------------------------------------

def test_discovery_v2_runtime_reads_cache_without_search(monkeypatch):
    """Stage two uses frozen cache readers and never reaches the Tavily client."""
    from catalyst_agents.runtime.web_evidence import WebEvidenceRetriever
    from catalyst_data import web_evidence
    from catalyst_data.canonical.temporal import (
        build_temporal_identity,
        with_event_time_publication_window,
    )
    from catalyst_data.ingestion.collectors import web_search
    monkeypatch.setattr(web_search, "server_key_pool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    monkeypatch.setattr(web_evidence, "cached_discovery_v2_rows", lambda *_: [])
    monkeypatch.setattr(web_evidence, "cached_roundup_segments", lambda *_args, **_kwargs: [])

    class EmptyRetriever:
        def retrieve(self, *_args, **_kwargs):
            return ()

    temporal = with_event_time_publication_window(build_temporal_identity("2026-01-06"))
    retriever = WebEvidenceRetriever(EmptyRetriever())
    assert retriever.retrieve("ACME", ticker="ACME", temporal_identity=temporal,
                              cutoff="2026-01-06T21:00:00Z", requested_manifest_id="manifest",
                              top_k=8, candidate_depth=20) == ()


def test_discovery_search_error_records_no_query_so_a_retry_searches_again(tmp_path, monkeypatch):
    """S10 (cache builder): a Tavily error propagates out of discover_v2 and leaves no query row (the next run of the
    builder searches again); the cache is unchanged, so a run reads what was there before."""
    from catalyst_data.ingestion.collectors import web_discovery_v2 as W
    monkeypatch.setattr(W, "_company", lambda ticker: "Acme")
    db = tmp_path / "web.sqlite3"

    def broken(*args, **kwargs):
        raise W.web_search.WebSearchError("tavily_keys_exhausted")

    case = {"ticker": "ACME", "session_date": "2026-01-06", "tier": "T3", "return_pct": -5.0}
    with pytest.raises(W.web_search.WebSearchError):
        W.discover_v2(case, search_fn=broken, fetcher=object(), db_path=db,
                      now=datetime(2026, 1, 12, tzinfo=timezone.utc))
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM web_discovery_v2_queries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM web_discovery_v2_rows").fetchone()[0] == 0
    conn.close()
    assert W.cached_rows_for_ticker_session("ACME", "2026-01-06", db_path=db) == []


def test_cached_rows_date_only_is_end_of_day_and_other_day_wraps_are_dropped(tmp_path):
    """S3/S10: a date-only web row is stamped 23:59:59Z (never before the move); a group wrap from another day is
    dropped; an issuer recap from a later day in the window is kept."""
    from catalyst_data.ingestion.collectors import web_discovery_v2 as W
    db = tmp_path / "web.sqlite3"
    conn = W._connect(db)
    def row(scope_key, family, url, published, scope):
        conn.execute("INSERT INTO web_discovery_v2_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (scope_key, "2026-01-06", family, url, url, "Pub", "pub.com", published, "url", "t", "body",
                      "fetch", None, scope, 0.9, "FULL_TEXT", "2026-01-07T00:00:00Z"))
        conn.execute("INSERT OR IGNORE INTO web_discovery_v2_case_links VALUES (?,?,?,?,?)",
                     ("c", "ACME", "2026-01-06", scope_key, family))
    row("ACME", "issuer_primary", "https://a.com/1", "2026-01-06", "issuer")
    row("ACME", "issuer_primary", "https://a.com/2", "2026-01-08T14:00:00Z", "issuer")
    row("MARKET", "market", "https://a.com/3", "2026-01-08T21:30:00Z", "group")
    conn.commit()
    conn.close()
    rows = {r["canonical_url"]: r["published_at"] for r in W.cached_rows_for_ticker_session("ACME", "2026-01-06",
                                                                                          db_path=db)}
    assert rows == {"https://a.com/1": "2026-01-06T23:59:59Z", "https://a.com/2": "2026-01-08T14:00:00Z"}


def test_stage_two_window_ends_at_now_before_d_plus_3():
    """S10: a run before D+3's close has its stage-2 window cut at the run time (no look-ahead, fewer recaps)."""
    from catalyst_data.canonical.temporal import (
        build_temporal_identity,
        with_event_time_publication_window,
    )
    ti = build_temporal_identity("2026-01-06")
    now = datetime(2026, 1, 7, 15, 0, tzinfo=timezone.utc)
    w = with_event_time_publication_window(ti, now=now, start_sessions_back=1)
    start, end = w.publication_window()
    assert end == now and start == ti.information_window_start_at
    full = with_event_time_publication_window(ti, now=datetime(2026, 2, 1, tzinfo=timezone.utc), start_sessions_back=1)
    assert full.publication_window()[1] == datetime(2026, 1, 9, 21, 0, tzinfo=timezone.utc)


# -- S12: what an abstention shows -----------------------------------------------------------------------------------

def _round(gate_reason: str) -> dict:
    return {"desk_decision": {"gate": {"reason_code": gate_reason}},
            "model_reported": {"analyst": {"hypotheses": []},
                               "verifier": {"called": False, "skipped_because": "no_primary"}},
            "code_checks": {"quotes": [], "gates": []}}


def test_abstention_reason_comes_from_the_last_round_only():
    """S12: stage 1 STALE_PRIMARY then stage 2 COMPETING_CAUSES shows NO_PRIMARY_PROPOSED: the stage-1 veto and the
    gate's own code are not in the user-facing reason (they stay in the trace)."""
    from catalyst_agents.attribution.answer_snapshot import derive_abstention_reason
    trace = {"rounds": [_round("STALE_PRIMARY"), _round("COMPETING_CAUSES")],
             "retrieval": {"included_in_pack": 6, "evidence_in_state": 6}}
    assert derive_abstention_reason(trace)["reason_code"] == "NO_PRIMARY_PROPOSED"
    trace["rounds"] = [_round("NO_ELIGIBLE_CAUSE"), _round("STALE_PRIMARY")]
    assert derive_abstention_reason(trace)["reason_code"] == "STALE_PRIMARY"
    trace["retrieval"] = {"included_in_pack": 0, "evidence_in_state": 0}
    assert derive_abstention_reason(trace)["reason_code"] == "NO_STOCK_NEWS"


def test_possible_explanation_carries_the_verifier_models_own_sentence(monkeypatch):
    """S12 doubt: an UNLINKED rejection shows the rejected quotes (verbatim) with the verifier's reason, a sentence the
    model wrote; the abstention text is therefore not quotes-only."""
    from catalyst_agents.attribution import abstain_context
    monkeypatch.setattr(abstain_context, "related_news_items", lambda pack, ticker: [])
    model_sentence = "The quotes report a product launch but nothing shows it moved the stock."
    verifier = SimpleNamespace(last={"statement": "“Acme launched X.” — Pub, 2026-01-06 14:00 UTC",
                                     "verdict": False, "category": "UNLINKED", "reason": model_sentence})
    quotes = [{"text": "Acme launched X.", "publisher": "Pub", "published_at": "2026-01-06 14:00 UTC",
               "roles": [], "role_reasons": {}}]
    out = abstain_context.structured(verifier, None, "ACME", possible_quotes=quotes)
    assert out["possible_explanation"]["verifier_reason"] == model_sentence
    assert model_sentence in abstain_context.render_structured(out)


# -- S0 / S2 / S3: calendar edges and company identity -------------------------------------------------------------

def test_early_close_sets_the_close_and_the_next_window_start():
    """S0/S2: an early-close session closes 13:00 ET; the next session's window opens at that close; the stage-2 end
    uses each session's own close."""
    from catalyst_data.canonical.temporal import (
        build_temporal_identity,
        with_event_time_publication_window,
    )
    friday = build_temporal_identity("2026-11-27")  # day after Thanksgiving, early close
    assert friday.session_close_at == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    monday = build_temporal_identity("2026-11-30")
    assert monday.information_window_start_at == friday.session_close_at
    w = with_event_time_publication_window(build_temporal_identity("2026-11-23"), now=datetime(2027, 1, 4,
                                           tzinfo=timezone.utc), start_sessions_back=1)
    assert w.publication_window()[1] == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)  # D+3 = the half day (11-26 is a holiday)


def test_a_share_class_without_a_peer_entry_gets_its_other_class_as_its_peer(tmp_path):
    """S2/S3 doubt: GOOG has no peer-map entry; the GICS fallback ranks same-sub-industry names by correlation, and the
    other share class of the same company (GOOGL) is a candidate, so it comes first."""
    import random

    from catalyst_data.ingestion import scope
    db = tmp_path / "market.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, close REAL, volume REAL)")
    rnd = random.Random(7)
    prices = {"GOOG": 100.0, "GOOGL": 100.0, "META": 100.0}
    for i in range(90):
        day = (datetime(2026, 1, 2) + timedelta(days=i)).strftime("%Y-%m-%d")
        common, own = rnd.gauss(0, 0.01), rnd.gauss(0, 0.01)
        prices["GOOG"] *= 1 + common
        prices["GOOGL"] *= 1 + common + rnd.gauss(0, 0.0005)
        prices["META"] *= 1 + 0.3 * common + own
        for s, p in prices.items():
            conn.execute("INSERT INTO ohlcv VALUES (?, ?, ?, ?, 1)", (s, day, p, p))
    conn.commit()
    peer_map = {"benchmark": "SPY", "targets": {"GOOGL": {"sector_etf": "XLC", "direct_peers": ["META"]}},
                "gics": {"tickers": {t: ["Communication Services", "Interactive Media & Services"]
                                     for t in ("GOOG", "GOOGL", "META")}}}
    _, _, peers = scope.resolve_context(conn, db, peer_map, "GOOG", "2026-03-31")
    assert peers[0] == "GOOGL"


def test_a_renamed_issuer_matches_on_a_generic_first_word_and_its_old_ticker_is_unmapped(tmp_path, monkeypatch):
    """S3 doubt: the SEC map knows only the current ticker. BNY (Bank of New York Mellon) is matched by the first word
    of its name, 'bank', so any bank headline names it; the old ticker BK has no CIK (no SEC index, no filing fact) and
    is matched only as the bare ticker."""
    from catalyst_data.ingestion.collectors import sec
    from catalyst_data.retrieval.web_bodies import subject_matcher
    store = tmp_path / "bodies.sqlite3"
    sqlite3.connect(store).close()
    (tmp_path / "sec_company_tickers.json").write_text(json.dumps(
        {"0": {"cik_str": 1390777, "ticker": "BNY", "title": "Bank of New York Mellon Corp"}}))
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(store))
    assert subject_matcher("BNY")("Bank of America shares fall on loan losses")
    assert not subject_matcher("BK")("Bank of New York Mellon raises its outlook")
    assert sec.cik_for("BK") is None and sec.cik_for("BNY") == "0001390777"
