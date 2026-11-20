from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.collectors import web_discovery_helpers, web_search
from catalyst_data.ingestion.collectors import web_discovery_v2 as v2

SESSION = "2026-03-06"
NOW = datetime(2026, 3, 20, tzinfo=timezone.utc)


class _NoFetcher:
    def fetch_page(self, _url):
        return None


def _case(tier="T3", **extra):
    return {
        "case_key": "TSLA",
        "ticker": "TSLA",
        "session_date": SESSION,
        "return_pct": 4.2,
        "spy_pct": 0.8,
        "sector_etf": "XLK",
        "tier": tier,
        **extra,
    }


def _set_company(monkeypatch):
    monkeypatch.setattr(web_discovery_helpers, "_company", lambda _ticker: "Tesla, Inc.")


def _coverage(db_path):
    conn = v2._connect(db_path)
    try:
        coverage.ensure_schema(conn)
        conn.commit()
        attempts = coverage.list_attempts(conn)
        facts = coverage.list_facts(conn)
        return attempts, facts
    finally:
        conn.close()


def _specs_by_attempt(attempts):
    return {attempt.attempt_id: json.loads(attempt.query_spec_json) for attempt in attempts}


def test_old_scope_family_cache_does_not_satisfy_a_different_query(tmp_path, monkeypatch):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"
    old_case = _case(tier="T2", return_pct=4.2)
    old_query = next(query for _, family, query in v2.queries_for_case(old_case)
                     if family == "sector")
    conn = v2._connect(db_path)
    conn.execute(
        "INSERT INTO web_discovery_v2_queries VALUES (?,?,?,?,?,?,?,?)",
        ("SECTOR:XLK", SESSION, "sector", old_query, "2026-03-06T14:00:00Z", 1, 0, 1),
    )
    conn.commit()
    conn.close()

    new_case = _case(tier="T2", return_pct=-4.2)
    new_query = next(query for _, family, query in v2.queries_for_case(new_case)
                     if family == "sector")
    calls = []
    v2.discover_v2(
        new_case,
        search_fn=lambda query, _key, **_kwargs: calls.append(query) or {"results": []},
        fetcher=_NoFetcher(),
        db_path=db_path,
        max_credits=1,
        now=NOW,
    )

    assert old_query != new_query
    assert calls == [new_query]
    attempts, facts = _coverage(db_path)
    fact_by_attempt = {fact.attempt_id: fact for fact in facts}
    current = [attempt for attempt in attempts
               if attempt.source_id == "daily:discovery_v2"
               and json.loads(attempt.query_spec_json).get("family") == "sector"]
    assert len(current) == 1
    assert json.loads(current[0].query_spec_json)["query"] == new_query
    assert fact_by_attempt[current[0].attempt_id].status == "EMPTY"


@pytest.mark.parametrize("cap", [True, 1.0, 1.5, "1"])
def test_credit_cap_must_be_a_non_boolean_integer(tmp_path, monkeypatch, cap):
    _set_company(monkeypatch)
    with pytest.raises(ValueError):
        v2.discover_v2(
            _case(tier="T2"), search_fn=lambda *_args, **_kwargs: {"results": []},
            fetcher=_NoFetcher(), db_path=tmp_path / "web.sqlite3", max_credits=cap,
            now=NOW,
        )


def test_newer_failed_attempt_invalidates_older_exact_request_cache(tmp_path, monkeypatch):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"
    case = _case(tier="T2")
    v2.discover_v2(
        case, search_fn=lambda *_args, **_kwargs: {"results": []},
        fetcher=_NoFetcher(), db_path=db_path, max_credits=1, now=NOW,
    )
    attempts, _facts = _coverage(db_path)
    original = next(attempt for attempt in attempts
                    if json.loads(attempt.query_spec_json).get("family") == "sector")

    conn = v2._connect(db_path)
    try:
        newer = coverage.start_attempt(
            conn, logical_task_id=original.logical_task_id, attempt_id="synthetic-newer-failure",
            source_id=original.source_id, product=original.product,
            subject_kind=original.subject_kind, subject_id=original.subject_id,
            query_spec=json.loads(original.query_spec_json),
            coverage_policy_version=original.coverage_policy_version,
            requested_from=original.requested_from, requested_until=original.requested_until,
        )
        conn.commit()
        coverage.record_coverage(
            conn, coverage_id="synthetic-newer-failure-fact",
            attempt_id=newer.attempt_id, status="ERROR", reason="ERROR",
            recorded_at="2026-03-21T00:00:00Z",
            coverage_from=None, coverage_until=None, enumeration_complete=None,
        )
        conn.commit()
    finally:
        conn.close()

    calls = []
    sector_query = next(query for _, family, query in v2.queries_for_case(case)
                        if family == "sector")
    v2.discover_v2(
        case, search_fn=lambda query, _key, **_kwargs: calls.append(query) or {"results": []},
        fetcher=_NoFetcher(), db_path=db_path, max_credits=1, now=NOW,
    )

    assert calls == [sector_query]


def test_cli_error_output_does_not_include_provider_exception_text(tmp_path, monkeypatch, capsys):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"
    cases_path = tmp_path / "cases.json"
    tiers_path = tmp_path / "tiers.json"
    cases_path.write_text(json.dumps([{
        "case_id": "TSLA", "ticker": "TSLA", "session_date": SESSION,
        "return_pct": 4.2, "spy_pct": 0.8, "sector_etf": "XLK",
    }]))
    tiers_path.write_text(json.dumps({"TSLA": "T2"}))
    monkeypatch.setenv("CATALYST_WEB_SEARCH_DB", str(db_path))
    monkeypatch.setattr(web_discovery_helpers.tavily_pool, "keys_from_env", lambda: ["synthetic-key"])

    def provider_exception(*_args, **_kwargs):
        raise web_search.WebSearchError("provider failed token=synthetic-secret")

    monkeypatch.setattr(web_discovery_helpers, "_call_tavily", provider_exception)
    monkeypatch.setattr(
        "sys.argv",
        ["web_discovery_v2", "run", "--cases-file", str(cases_path),
         "--tiers-from", str(tiers_path), "--max-credits", "1"],
    )

    assert v2.main() == 0
    output = capsys.readouterr().out
    assert "synthetic-secret" not in output


def test_t1_plan_has_auditable_not_applicable_outcome_without_provider_attempt(
    tmp_path, monkeypatch
):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"
    calls = []
    case = _case(tier="T1")

    result = v2.discover_v2(
        case, search_fn=lambda *args, **kwargs: calls.append((args, kwargs)),
        fetcher=_NoFetcher(), db_path=db_path, max_credits=4, now=NOW,
    )

    attempts, facts = _coverage(db_path)
    conn = v2._connect(db_path)
    try:
        outcomes = conn.execute(
            "SELECT outcome,reason FROM web_discovery_v2_plan_outcomes "
            "WHERE case_key=? AND ticker=? AND session_date=?",
            (case["case_key"], case["ticker"], SESSION),
        ).fetchall()
    finally:
        conn.close()

    assert result["credits_used"] == 0
    assert calls == []
    assert attempts == []
    assert facts == []
    assert len(outcomes) == 1
    assert outcomes[0][0] == "NOT_APPLICABLE"


def test_plan_outcomes_are_immutable_and_idempotent_but_keep_new_outcomes(tmp_path):
    db_path = tmp_path / "web.sqlite3"
    args = {
        "case_key": "TSLA", "ticker": "TSLA", "session": SESSION,
        "scope_key": "TSLA", "family": "issuer_primary", "pass_number": 2,
        "outcome": "NOT_APPLICABLE", "reason": "pass1_relevant_issuer_result",
        "evidence": {"request_hash": "synthetic-request", "url": "https://news.example/item"},
    }
    conn = v2._connect(db_path)
    try:
        v2._record_plan_outcome(conn, **args)
        v2._record_plan_outcome(conn, **args)
        same_count = conn.execute(
            "SELECT COUNT(*) FROM web_discovery_v2_plan_outcomes "
            "WHERE case_key='TSLA' AND ticker='TSLA' AND session_date=? AND pass=2",
            (SESSION,),
        ).fetchone()[0]
        assert same_count == 1

        v2._record_plan_outcome(
            conn, **{**args, "outcome": "NOT_EVALUATED", "reason": "pass1_not_completed",
                     "evidence": {"request_hash": "synthetic-later-request"}},
        )
        total_count = conn.execute(
            "SELECT COUNT(*) FROM web_discovery_v2_plan_outcomes "
            "WHERE case_key='TSLA' AND ticker='TSLA' AND session_date=? AND pass=2",
            (SESSION,),
        ).fetchone()[0]
        assert total_count == 2

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE web_discovery_v2_plan_outcomes SET reason='changed'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM web_discovery_v2_plan_outcomes")
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [{"url": "https://news.example/item", "title": 7}]},
        {"results": [{"url": "https://news.example/item", "content": ["bad"]}]},
        {"results": [{"url": "https://news.example/item", "raw_content": {"bad": True}}]},
        {"results": [{"url": "https://news.example/item", "published_date": 123}]},
        {"error": "provider response failed", "results": []},
    ],
    ids=("title-type", "content-type", "raw-content-type", "published-date-type", "explicit-error"),
)
def test_invalid_optional_fields_and_explicit_error_record_error(
    tmp_path, monkeypatch, payload
):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"
    case = _case(tier="T2")

    with pytest.raises(web_search.WebSearchError):
        v2.discover_v2(
            case, search_fn=lambda *_args, **_kwargs: payload,
            fetcher=_NoFetcher(), db_path=db_path, max_credits=1, now=NOW,
        )

    attempts, facts = _coverage(db_path)
    assert len(attempts) == 1
    assert len(facts) == 1
    assert facts[0].attempt_id == attempts[0].attempt_id
    assert (facts[0].status, facts[0].reason) == ("ERROR", "ERROR")


def test_missing_optional_search_result_fields_remain_accepted(tmp_path, monkeypatch):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"
    result = v2.discover_v2(
        _case(tier="T2"),
        search_fn=lambda *_args, **_kwargs: {
            "results": [{"url": "https://news.example/item"}],
        },
        fetcher=_NoFetcher(), db_path=db_path, max_credits=1, now=NOW,
    )

    attempts, facts = _coverage(db_path)
    assert result["credits_used"] == 1
    assert len(attempts) == 1
    assert len(facts) == 1
    assert facts[0].status == "OK"


def test_full_text_fetched_at_is_not_before_page_response_completion(tmp_path, monkeypatch):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"
    search_finished = "2026-03-20T15:00:00+00:00"
    page_finished = "2026-03-20T15:00:05+00:00"
    clock = {"now": "2026-03-20T14:59:00+00:00"}

    def search(*_args, **_kwargs):
        clock["now"] = search_finished
        return {"results": [{
            "url": "https://news.example/tesla-deliveries",
            "title": "Tesla shares rise after delivery report",
            "published_date": SESSION,
        }]}

    class _TimedFetcher:
        def fetch_page(self, url):
            clock["now"] = page_finished
            return url, "<html><title>Tesla</title><body>synthetic page</body></html>"

    monkeypatch.setattr(v2, "_observed_at", lambda: clock["now"])
    monkeypatch.setattr(v2.fetch, "extract", lambda *_args: "Tesla deliveries " + ("synthetic article text " * 40))
    monkeypatch.setattr(v2.web_search, "classify_page_time", lambda *_args: (
        "page", datetime(2026, 3, 6, 16, tzinfo=timezone.utc), "published",
    ))
    from catalyst_data.ingestion.storage import page_versions
    monkeypatch.setattr(page_versions, "record", lambda *_args, **_kwargs: None)

    v2.discover_v2(
        _case(tier="T2"), search_fn=search, fetcher=_TimedFetcher(), db_path=db_path,
        max_credits=1, now=NOW,
    )

    conn = v2._connect(db_path)
    try:
        row = conn.execute(
            "SELECT status,fetched_at FROM web_discovery_v2_rows "
            "WHERE url='https://news.example/tesla-deliveries'",
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["status"] == "FULL_TEXT"
    assert row["fetched_at"] >= page_finished


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": [{"title": "Tesla shares move", "published_date": SESSION}]},
    ],
    ids=("missing-results", "missing-result-url"),
)
def test_malformed_result_payload_records_error_not_empty(tmp_path, monkeypatch, payload):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"
    case = _case(tier="T2")

    try:
        v2.discover_v2(
            case,
            search_fn=lambda *_args, **_kwargs: payload,
            fetcher=_NoFetcher(),
            db_path=db_path,
            max_credits=1,
            now=NOW,
        )
    except web_search.WebSearchError:
        pass

    attempts, facts = _coverage(db_path)
    assert len(attempts) == 1
    assert len(facts) == 1
    assert facts[0].attempt_id == attempts[0].attempt_id
    assert (facts[0].status, facts[0].reason) == ("ERROR", "ERROR")


def test_failed_provider_request_consumes_the_job_budget_before_retry(tmp_path, monkeypatch, capsys):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"
    cases_path = tmp_path / "cases.json"
    tiers_path = tmp_path / "tiers.json"
    cases_path.write_text(json.dumps([{
        "case_id": "TSLA",
        "ticker": "TSLA",
        "session_date": SESSION,
        "return_pct": 4.2,
        "spy_pct": 0.8,
        "sector_etf": "XLK",
    }]))
    tiers_path.write_text(json.dumps({"TSLA": "T3"}))
    monkeypatch.setenv("CATALYST_WEB_SEARCH_DB", str(db_path))
    monkeypatch.setattr(web_discovery_helpers.tavily_pool, "keys_from_env", lambda: ["synthetic-key"])

    sent = []

    def provider_error(query, _key, *, include_domains, **_kwargs):
        sent.append((query, include_domains))
        raise web_search.WebSearchError("tavily_network_error")

    def call_tavily(client, query, *, start, end, include_raw_content=False, **_kwargs):
        return client(query, "synthetic-key", start=start, end=end,
                      include_raw_content=include_raw_content)

    monkeypatch.setattr(v2, "_tavily_search_v2", provider_error)
    monkeypatch.setattr(web_discovery_helpers, "_call_tavily", call_tavily)
    monkeypatch.setattr(v2._time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "sys.argv",
        ["web_discovery_v2", "run", "--cases-file", str(cases_path),
         "--tiers-from", str(tiers_path), "--max-credits", "1",
         "--max-credits-total", "1"],
    )

    assert v2.main() == 0
    capsys.readouterr()

    assert len(sent) == 1
    attempts, facts = _coverage(db_path)
    assert len(attempts) == 1
    assert len(facts) == 1
    assert (facts[0].status, facts[0].reason) == ("ERROR", "ERROR")


def test_conditional_pass_two_gate_is_audited_without_provider_attempt(tmp_path, monkeypatch):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"

    def search(query, _key, **_kwargs):
        if query.startswith("Why did Tesla"):
            return {"results": [{
                "url": "https://news.example/tesla-move",
                "title": "Tesla shares rise after delivery report",
                "published_date": SESSION,
            }]}
        return {"results": []}

    v2.discover_v2(
        _case(), search_fn=search, fetcher=_NoFetcher(), db_path=db_path,
        max_credits=4, now=NOW,
    )
    attempts, _facts = _coverage(db_path)
    specs = _specs_by_attempt(attempts)
    assert not any(spec.get("pass") == 2 for spec in specs.values())

    conn = v2._connect(db_path)
    try:
        outcome = conn.execute(
            "SELECT outcome,reason FROM web_discovery_v2_plan_outcomes "
            "WHERE case_key=? AND ticker=? AND session_date=? AND scope_key=? "
            "AND family=? AND pass=2",
            ("TSLA", "TSLA", SESSION, "TSLA", "issuer_primary"),
        ).fetchone()
        outcome = tuple(outcome) if outcome is not None else None
    finally:
        conn.close()

    assert outcome == ("NOT_APPLICABLE", "pass1_relevant_issuer_result")


def test_conditional_pass_two_deferred_by_budget_is_not_empty(tmp_path, monkeypatch):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"

    v2.discover_v2(
        _case(), search_fn=lambda *_args, **_kwargs: {"results": []},
        fetcher=_NoFetcher(), db_path=db_path, max_credits=1, now=NOW,
    )
    attempts, facts = _coverage(db_path)
    specs = _specs_by_attempt(attempts)
    conditional = [(attempt, fact, specs[attempt.attempt_id])
                   for attempt in attempts for fact in facts
                   if fact.attempt_id == attempt.attempt_id
                   and specs[attempt.attempt_id].get("pass") == 2]

    assert len(conditional) == 1
    _, fact, _ = conditional[0]
    assert (fact.status, fact.reason) == ("NOT_FETCHED", "deferred")


def test_bounded_tavily_response_does_not_assert_source_enumeration_complete(
    tmp_path, monkeypatch
):
    _set_company(monkeypatch)
    db_path = tmp_path / "web.sqlite3"
    results = [{
        "url": f"https://news.example/item-{index}",
        "title": f"Market article {index}",
        "published_date": SESSION,
    } for index in range(10)]

    v2.discover_v2(
        _case(tier="T2"),
        search_fn=lambda *_args, **_kwargs: {"results": results},
        fetcher=_NoFetcher(),
        db_path=db_path,
        max_credits=1,
        now=NOW,
    )
    attempts, facts = _coverage(db_path)

    assert len(attempts) == 1
    assert len(facts) == 1
    assert facts[0].status == "OK"
    assert facts[0].enumeration_complete is None
    spec = json.loads(attempts[0].query_spec_json)
    assert spec["max_results"] == 10
    assert spec["include_domains"]
