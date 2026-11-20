from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from catalyst_agents.attribution.extraction import (
    AboutTicker,
    EventTiming,
    EventType,
    Extraction,
    Session,
)
from catalyst_agents.attribution.quotes import check_quote
from catalyst_agents.runtime.earnings_timing import (
    apply_earnings_timing,
    public_release_fields,
    run_earnings_timing_scope,
)

UTC = timezone.utc
BODY = "The company reported quarterly results that exceeded analyst expectations."


def test_earnings_timing_scope_appends_one_jsonl_record_per_run(monkeypatch, tmp_path):
    timings_path = tmp_path / "nested" / "timings.jsonl"
    monkeypatch.setenv("CATALYST_AG_TIMINGS_PATH", str(timings_path))

    for name in ("first_run", "second_run"):
        with run_earnings_timing_scope() as cache:
            cache._record_timing(name, 1.0)

    lines = timings_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert all("timings" in record for record in records)
    assert [next(iter(record["timings"])) for record in records] == ["first_run", "second_run"]


def test_earnings_timing_scope_ignores_timing_file_write_errors(monkeypatch, tmp_path):
    timings_path = tmp_path / "is-a-directory"
    timings_path.mkdir()
    monkeypatch.setenv("CATALYST_AG_TIMINGS_PATH", str(timings_path))

    with run_earnings_timing_scope() as cache:
        cache._record_timing("scope", 1.0)


@pytest.mark.parametrize(
    ("release_session", "accepted", "target", "expected_timing", "public_session"),
    [
        ("pre_market", "2026-11-02T12:00:00Z", "2026-11-02", EventTiming.BEFORE_OPEN, "pre"),
        ("intraday", "2026-11-02T14:30:00Z", "2026-11-02", EventTiming.INTRADAY, "intraday"),
        ("after_close", "2026-11-02T21:00:00Z", "2026-11-03", EventTiming.BEFORE_OPEN, "post"),
        ("non_trading_day", "2026-11-28T17:00:00Z", "2026-11-30", EventTiming.BEFORE_OPEN, "post"),
    ],
)
def test_verified_issuer_earnings_timing_sets_session_and_public_fields(
    release_session, accepted, target, expected_timing, public_session,
):
    extraction = Extraction(
        about_ticker=AboutTicker.ISSUER, session=Session.OTHER_SESSION,
        event_type=EventType.EARNINGS, event_timing=EventTiming.UNKNOWN,
    )
    timing = {
        "issuer_cik_match": True, "is_issuer_earnings": True,
        "release_session": release_session, "corrected_at": accepted,
        "release_local_time": "2026-11-02T09:30:00-05:00",
        "attributed_session_date": target,
    }

    checked = apply_earnings_timing(extraction, timing, session_date=target)

    assert checked.session is Session.THIS_SESSION
    assert checked.event_timing is expected_timing
    assert public_release_fields(timing) == {
        "release_session": public_session,
        "release_time_et": "2026-11-02T09:30-05:00",
    }


@pytest.mark.parametrize(("issuer_match", "flag"), [(True, None), (True, False), (False, True)])
def test_unknown_or_non_earnings_does_not_relax_model_session(issuer_match, flag):
    extraction = Extraction(
        about_ticker=AboutTicker.ISSUER, session=Session.OTHER_SESSION,
        event_type=EventType.EARNINGS, event_timing=EventTiming.AFTER_CLOSE,
    )
    timing = {
        "issuer_cik_match": issuer_match, "is_issuer_earnings": flag,
        "release_session": "after_close", "corrected_at": "2026-11-02T21:00:00Z",
        "release_local_time": "2026-11-02T16:00:00-05:00",
        "attributed_session_date": "2026-11-03",
    }

    checked = apply_earnings_timing(extraction, timing, session_date="2026-11-03")

    assert checked is extraction
    assert public_release_fields(timing) is None


def test_corrected_after_close_acceptance_can_pass_next_session_quote_window():
    item = SimpleNamespace(
        evidence_id="sec-1", content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 14, 22, tzinfo=UTC),
        excerpt_text=BODY, canonical_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm",
        publisher="Issuer", source_class="sec",
    )
    temporal = SimpleNamespace(
        information_window_start_at=datetime(2026, 1, 14, 21, tzinfo=UTC),
        cutoff_at=datetime(2026, 1, 15, 21, tzinfo=UTC),
        session_open_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        session_date="2026-01-15",
    )
    timing = {
        "issuer_cik_match": True, "is_issuer_earnings": True,
        "release_session": "after_close", "corrected_at": "2026-01-14T21:01:00Z",
        "release_local_time": "2026-01-14T16:01:00-05:00",
        "attributed_session_date": "2026-01-15",
    }
    assert check_quote(
        BODY, "sec-1", supporting_ids={"sec-1"}, items={"sec-1": item},
        temporal_identity=temporal, release_timing=timing,
    ) == "OK"


def test_d_e_qualification_does_not_bypass_other_session_move_rule():
    item = SimpleNamespace(
        evidence_id="sec-1", content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 14, 22, tzinfo=UTC),
        excerpt_text="The company reported results. Shares rose Monday after the announcement.",
        canonical_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm",
        publisher="Issuer", source_class="sec",
    )
    temporal = SimpleNamespace(
        information_window_start_at=datetime(2026, 1, 14, 21, tzinfo=UTC),
        cutoff_at=datetime(2026, 1, 15, 21, tzinfo=UTC),
        session_open_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        session_date="2026-01-15",
    )
    timing = {
        "issuer_cik_match": True, "is_issuer_earnings": True,
        "release_session": "after_close", "corrected_at": "2026-01-14T21:01:00Z",
        "release_local_time": "2026-01-14T16:01:00-05:00",
        "attributed_session_date": "2026-01-15",
    }
    assert check_quote(
        "Shares rose Monday after the announcement.", "sec-1", supporting_ids={"sec-1"},
        items={"sec-1": item}, temporal_identity=temporal, release_timing=timing,
    ) == "R6_OTHER_SESSION_MOVE"


def test_quote_checks_keep_corrected_release_fields_for_public_cause():
    from catalyst_agents.attribution.quotes import apply_quote_rules

    item = SimpleNamespace(
        evidence_id="sec-1", content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 14, 22, tzinfo=UTC), excerpt_text=BODY,
        canonical_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm",
        publisher="Issuer", source_class="sec",
    )
    temporal = SimpleNamespace(
        information_window_start_at=datetime(2026, 1, 14, 21, tzinfo=UTC),
        cutoff_at=datetime(2026, 1, 15, 21, tzinfo=UTC),
        session_open_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        session_date="2026-01-15",
    )
    timing = {
        "issuer_cik_match": True, "is_issuer_earnings": True,
        "release_session": "after_close", "corrected_at": "2026-01-14T21:01:00Z",
        "release_local_time": "2026-01-14T16:01:00-05:00",
        "attributed_session_date": "2026-01-15",
    }
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=("sec-1",),
        temporal_identity=temporal, observation=SimpleNamespace(ticker="AAA"),
    )
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": ["sec-1"],
        "quotes": [{"evidence_id": "sec-1", "text": BODY}],
    }]}

    checked, checks = apply_quote_rules(
        payload, pack, release_timings={"sec-1": timing},
    )

    assert checks[0].kept
    assert checks[0].release_session == "post"
    assert checks[0].release_time_et == "2026-01-14T16:01-05:00"
    assert checked["candidate_hypotheses"][0]["quotes"][0]["release_session"] == "post"


_VALID_AUDIT_TIMING = {
    "issuer_cik_match": True, "is_issuer_earnings": True,
    "release_session": "after_close", "corrected_at": "2026-01-14T20:00:00Z",
    "release_local_time": "2026-01-14T16:00:00-05:00",
    "attributed_session_date": "2026-01-15",
}


@pytest.mark.parametrize(("field", "value"), [
    ("issuer_cik_match", False),
    ("attributed_session_date", "2026-01-14"),
    ("corrected_at", "2026-01-14T20:00:00"),
    ("corrected_at", None),
])
def test_quote_audit_uses_ordinary_window_for_unqualified_timing(field, value):
    from catalyst_agents.attribution.quotes import apply_quote_rules

    sentence = "The company reported quarterly results that exceeded analyst expectations."
    item = SimpleNamespace(
        evidence_id="sec-1", content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 16, 15, tzinfo=UTC), excerpt_text=sentence,
        canonical_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm",
        publisher="Issuer", source_class="sec",
    )
    temporal = SimpleNamespace(
        information_window_start_at=datetime(2026, 1, 14, 21, tzinfo=UTC),
        cutoff_at=datetime(2026, 1, 16, 21, tzinfo=UTC),
        session_open_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        session_date="2026-01-15",
        publication_window=lambda: (
            datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 16, 21, tzinfo=UTC),
        ),
    )
    timing = {**_VALID_AUDIT_TIMING, field: value}
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=("sec-1",),
        temporal_identity=temporal, observation=SimpleNamespace(ticker="AAA"),
    )
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": ["sec-1"], "quotes": [{"evidence_id": "sec-1", "text": sentence}],
    }]}

    _, checks = apply_quote_rules(payload, pack, evidence_window_policy="event_time_v2")

    assert checks[0].rule == "OK"
    assert checks[0].r4a == "PASS"
    assert checks[0].r4b_path != "issuer_earnings_release"
    assert checks[0].release_session is None


def test_quote_audit_uses_qualified_corrected_time_for_issuer_release():
    from catalyst_agents.attribution.quotes import apply_quote_rules

    sentence = "The company reported quarterly results that exceeded analyst expectations."
    item = SimpleNamespace(
        evidence_id="sec-1", content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 14, 22, tzinfo=UTC), excerpt_text=sentence,
        canonical_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm",
        publisher="Issuer", source_class="sec",
    )
    temporal = SimpleNamespace(
        information_window_start_at=datetime(2026, 1, 14, 21, tzinfo=UTC),
        cutoff_at=datetime(2026, 1, 15, 21, tzinfo=UTC),
        session_open_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        session_date="2026-01-15",
        publication_window=lambda: (
            datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 16, 21, tzinfo=UTC),
        ),
    )
    timing = {
        "issuer_cik_match": True, "is_issuer_earnings": True,
        "release_session": "after_close", "corrected_at": "2026-01-14T21:01:00Z",
        "release_local_time": "2026-01-14T16:01:00-05:00",
        "attributed_session_date": "2026-01-15",
    }
    item.release_timing = timing
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=("sec-1",),
        temporal_identity=temporal, observation=SimpleNamespace(ticker="AAA"),
    )
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": ["sec-1"], "quotes": [{"evidence_id": "sec-1", "text": sentence}],
    }]}

    _, checks = apply_quote_rules(payload, pack, evidence_window_policy="event_time_v2")

    assert checks[0].rule == "OK"
    assert checks[0].r4a == "PASS"
    assert checks[0].r4b_path == "issuer_earnings_release"
    assert checks[0].release_session == "post"
    assert checks[0].release_time_et == "2026-01-14T16:01-05:00"


def test_quote_rules_use_only_explicit_release_timings(monkeypatch):
    from catalyst_agents.attribution.quotes import apply_quote_rules

    item = SimpleNamespace(
        evidence_id="sec-1", content_state="FULL_TEXT",
        eligible_at=datetime(2026, 1, 14, 22, tzinfo=UTC), excerpt_text=BODY,
        canonical_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm",
        publisher="Issuer", source_class="sec",
    )
    temporal = SimpleNamespace(
        information_window_start_at=datetime(2026, 1, 14, 21, tzinfo=UTC),
        cutoff_at=datetime(2026, 1, 15, 21, tzinfo=UTC),
        session_open_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        session_date="2026-01-15",
    )
    timing = {
        "issuer_cik_match": True, "is_issuer_earnings": True,
        "release_session": "after_close", "corrected_at": "2026-01-14T21:01:00Z",
        "release_local_time": "2026-01-14T16:01:00-05:00",
        "attributed_session_date": "2026-01-15",
    }
    pack = SimpleNamespace(
        evidence_inventory=(item,), included_evidence_ids=("sec-1",),
        temporal_identity=temporal, observation=SimpleNamespace(ticker="AAA"),
    )
    payload = {"candidate_hypotheses": [{
        "hypothesis_ref": "h1", "proposed_role": "PRIMARY",
        "supporting_evidence_ids": ["sec-1"], "quotes": [{"evidence_id": "sec-1", "text": BODY}],
    }]}

    with run_earnings_timing_scope() as cache:
        monkeypatch.setattr(cache, "for_context_pack", lambda _: {"sec-1": timing})
        explicit, explicit_checks = apply_quote_rules(payload, pack, release_timings={"sec-1": timing})
        absent, absent_checks = apply_quote_rules(payload, pack)

    assert explicit_checks[0].release_session == "post"
    assert explicit["candidate_hypotheses"][0]["quotes"][0]["release_session"] == "post"
    assert absent_checks[0].release_session is None
    assert absent["candidate_hypotheses"][0]["quotes"][0].get("release_session") is None


def test_no_local_earnings_record_returns_to_normal_timing_checks(monkeypatch):
    from catalyst_agents.runtime.earnings_timing import EarningsTimingCache
    from catalyst_data import read_api

    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", "/test/news.sqlite3")
    monkeypatch.setattr(
        read_api, "earnings_release_timing",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            read_api.SecFilingDataError("no SEC filing rows matched the requested local data")
        ),
    )
    item = {
        "canonical_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/main.htm",
    }

    assert EarningsTimingCache().for_item(item, ticker="AAA") is None


def test_earnings_timing_uses_the_shared_sec_read_configuration(monkeypatch, tmp_path):
    from catalyst_agents.runtime.earnings_timing import EarningsTimingCache
    from catalyst_data import read_api
    from catalyst_data.ingestion.collectors import sec, sec_filings

    store = tmp_path / "migration" / "dev-r4.sqlite3"
    projection_store = tmp_path / "migration" / "dev-projection.sqlite3"
    ticker_mapping = store.parent / "sec_company_tickers.json"
    submissions_dir = tmp_path / "sec-submissions"
    correction_table = (
        Path(read_api.__file__).resolve().parents[3]
        / "data/corrections/sec_acceptance_v1.csv"
    )
    for path in (ticker_mapping, correction_table):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path != correction_table:
            path.touch()
    submissions_dir.mkdir()
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(store))
    monkeypatch.setenv("CATALYST_SEC_FILING_PROJECTION_DB", str(projection_store))
    monkeypatch.setattr(sec, "_tickers_path", lambda: ticker_mapping)
    monkeypatch.setattr(sec_filings, "_dir", lambda: submissions_dir)
    calls = []

    def read(path, **kwargs):
        calls.append((path, kwargs))
        return []

    monkeypatch.setattr(read_api, "earnings_release_timing", read)
    item = {
        "canonical_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/main.htm",
    }

    assert EarningsTimingCache().for_item(item, ticker="AAA") is None
    assert len(calls) == 1
    path, kwargs = calls[0]
    assert callable(kwargs.pop("timing_callback"))
    assert (path, kwargs) == (
        str(projection_store), {
            "accessions": ["0000000001-26-000001"],
            "immutable": False,
            "correction_table_path": correction_table,
            "ticker_mapping_path": ticker_mapping,
            "submissions_dir": submissions_dir,
        },
    )


def test_earnings_timing_defaults_projection_store_to_body_store(monkeypatch, tmp_path):
    from catalyst_agents.runtime.earnings_timing import EarningsTimingCache
    from catalyst_data import read_api
    from catalyst_data.ingestion.collectors import sec, sec_filings

    store = tmp_path / "news.sqlite3"
    ticker_mapping = tmp_path / "sec_company_tickers.json"
    submissions_dir = tmp_path / "submissions"
    correction_table = Path(read_api.__file__).resolve().parents[3] / "data/corrections/sec_acceptance_v1.csv"
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(store))
    monkeypatch.delenv("CATALYST_SEC_FILING_PROJECTION_DB", raising=False)
    monkeypatch.setattr(sec, "_tickers_path", lambda: ticker_mapping)
    monkeypatch.setattr(sec_filings, "_dir", lambda: submissions_dir)
    observed = []
    monkeypatch.setattr(
        read_api, "earnings_release_timing",
        lambda path, **kwargs: observed.append(path) or [],
    )
    item = {
        "canonical_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/main.htm",
    }

    assert EarningsTimingCache().for_item(item, ticker="AAA") is None
    assert observed == [str(store)]


def test_verifier_receives_only_code_marked_earnings_timing_context():
    from catalyst_agents.attribution.verifier import build_prompt

    pack = SimpleNamespace(
        observation=SimpleNamespace(
            ticker="AAA", target_return=2.0, market_comove=None, sector_comove=None,
            commodity_ticker=None, commodity_return=None, gap_return=None,
        ),
        temporal_identity=SimpleNamespace(
            session_date="2026-11-02",
            session_open_at=datetime(2026, 11, 2, 14, 30, tzinfo=UTC),
            session_close_at=datetime(2026, 11, 2, 21, tzinfo=UTC),
        ),
    )

    marked = build_prompt(
        "“The company reported results.” — SEC filing, intraday release 2026-11-02T11:10-05:00 ET",
        pack,
    )
    ordinary = build_prompt("Ordinary quote.", pack)

    assert "intraday release may explain the full daily close-to-close bar" in marked
    assert "intraday release may explain the full daily close-to-close bar" not in ordinary
