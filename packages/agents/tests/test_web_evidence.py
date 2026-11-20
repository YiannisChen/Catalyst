from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from catalyst_agents.runtime.web_evidence import WebEvidenceRetriever
from catalyst_data.canonical.temporal import (
    build_temporal_identity,
    with_event_time_publication_window,
)


class _Base:
    def __init__(self, rows=()):
        self.rows = rows

    def retrieve(self, *_args, **_kwargs):
        return self.rows


def test_legacy_runtime_modes_are_rejected():
    for mode in ("cache_code3", "cache_code3_why", "cache_code3_why_roundups", "roundups_only"):
        with pytest.raises(ValueError, match="unsupported web evidence runtime mode"):
            WebEvidenceRetriever(_Base(), mode=mode)


def test_historical_attribution_policy_modes_still_roundtrip():
    from catalyst_agents.attribution.policy import AttributionPolicy

    payload = {
        "verifier_enabled": True,
        "corrective_enabled": False,
        "quote_rules_version": "legacy",
        "web_evidence": "cache_code3_why_roundups",
        "web_on_demand": True,
        "investigator_mode": "agent_v3",
    }
    policy = AttributionPolicy.model_validate(payload)
    assert AttributionPolicy.model_validate(policy.model_dump(mode="json")) == policy


def test_legacy_web_policies_are_rejected():
    from catalyst_agents.attribution.policy import resolve_attribution_policy

    for mode in ("cache_code3", "cache_code3_why", "cache_code3_why_roundups", "roundups_only"):
        with pytest.raises(ValueError, match="unsupported web evidence policy"):
            resolve_attribution_policy({"CATALYST_EVIDENCE_WINDOW_POLICY": "event_time_v1",
                                        "CATALYST_WEB_EVIDENCE": mode})


def test_policy_web_fields_are_opt_in_and_fail_closed():
    from catalyst_agents.attribution.policy import resolve_attribution_policy

    assert resolve_attribution_policy({}).web_evidence == "discovery_v2"  # A′ is the default
    default = resolve_attribution_policy({}, evidence_window_policy="pre_close_v1")
    assert "web_evidence" not in default.model_dump(mode="json")
    assert "web_on_demand" not in default.model_dump(mode="json")
    enabled = resolve_attribution_policy({"CATALYST_EVIDENCE_WINDOW_POLICY": "desk_cascade_v1",
                                          "CATALYST_WEB_EVIDENCE": "discovery_v2"})
    assert enabled.web_evidence == "discovery_v2"
    with pytest.raises(ValueError, match="no longer supported"):
        resolve_attribution_policy({"CATALYST_EVIDENCE_WINDOW_POLICY": "desk_cascade_v1",
                                    "CATALYST_WEB_EVIDENCE": "discovery_v2",
                                    "CATALYST_WEB_ON_DEMAND": "1"})
    with pytest.raises(ValueError, match="no longer supported"):
        resolve_attribution_policy({}, evidence_window_policy="desk_cascade_v1",
                                   web_evidence="discovery_v2", web_on_demand=True)

    with pytest.raises(ValueError, match="requires desk_cascade_v1"):
        resolve_attribution_policy(
            {}, evidence_window_policy="event_time_p1", web_evidence="discovery_v2"
        )


def test_discovery_v2_serves_top_full_text_rows_and_roundups_by_ticker_date(tmp_path, monkeypatch):
    db = tmp_path / "web.sqlite3"
    monkeypatch.setenv("CATALYST_WEB_SEARCH_DB", str(db))
    from catalyst_data.ingestion.collectors import cnbc_roundups, web_search

    def no_provider(*_args, **_kwargs):
        raise AssertionError("discovery_v2 runtime must not make provider calls")

    monkeypatch.setattr(web_search, "server_key_pool", no_provider)
    monkeypatch.setattr(cnbc_roundups, "discover_day", no_provider)
    from catalyst_data.ingestion.collectors.web_discovery_v2 import (
        _connect as connect_v2,
    )

    conn = connect_v2(db)
    rows = []
    links = []
    for index in range(15):
        scope = "issuer" if index % 2 == 0 else "group"
        family = f"family-{index}"
        url = f"https://example.com/v2/{index}"
        rows.append((f"TSLA:{index}", "2026-01-15", family, url, url, "Example",
                     "example.com", "2026-01-15T18:00:00Z", "published_at",
                     f"Tesla headline {index}", f"Distinct full text story {index}. " * 8,
                     "fetch", f"{index + 1:064x}", scope, float(100 - index),
                     "FULL_TEXT", "2026-01-20T00:00:00Z"))
        links.append((f"case-{index}", "TSLA", "2026-01-15", f"TSLA:{index}", family))
    rows.append(("TSLA:duplicate", "2026-01-15", "family-duplicate",
                 "https://example.com/v2/0", "https://example.com/v2/0", "Example",
                 "example.com", "2026-01-15T18:00:00Z", "published_at",
                 "Duplicate canonical URL", "Duplicate body.", "fetch", "d" * 64,
                 "group", 99.5, "FULL_TEXT", "2026-01-20T00:00:00Z"))
    links.append(("case-duplicate", "TSLA", "2026-01-15", "TSLA:duplicate", "family-duplicate"))
    for index, (status, scope, date, ticker) in enumerate((
        ("IRRELEVANT", "issuer", "2026-01-15", "TSLA"),
        ("SPAM", "group", "2026-01-15", "TSLA"),
        ("UNDATED", "issuer", "2026-01-15", "TSLA"),
        ("FULL_TEXT", "issuer", "2026-01-15", "MSFT"),
        ("FULL_TEXT", "issuer", "2026-01-14", "TSLA"),
        ("FULL_TEXT", "market", "2026-01-15", "TSLA"),
    )):
        family = f"excluded-{index}"
        url = f"https://example.com/excluded/{index}"
        rows.append((f"TSLA:excluded:{index}", "2026-01-15", family, url, url,
                     "Example", "example.com", None, None, f"excluded {index}",
                     f"Excluded text {index}.", "fetch", f"{index + 30:064x}", scope,
                     1000.0, status, "2026-01-20T00:00:00Z"))
        links.append((f"excluded-case-{index}", ticker, date, f"TSLA:excluded:{index}", family))
    conn.executemany("INSERT INTO web_discovery_v2_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO web_discovery_v2_case_links VALUES (?,?,?,?,?)", links)
    conn.commit()
    conn.close()

    from catalyst_data.ingestion.collectors import cnbc_roundups
    conn = cnbc_roundups._connect(db)
    conn.execute("""INSERT INTO roundup_segments
        (session_date,roundup_kind,url,canonical_url,title,published_at,published_time_source,
         body_source,ticker,segment_index,segment_text,segment_sha256,page_sha256,fetched_at,credits)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("2026-01-15", "premarket", "https://www.cnbc.com/roundup",
         "https://www.cnbc.com/roundup", "CNBC roundup", "2026-01-15T14:00:00Z",
         "jsonld.datePublished", "fetch", "TSLA", 1,
         "Tesla shares advanced after an unrelated battery supplier announcement.",
         "segment-hash", "page-hash", "2026-01-20T00:00:00Z", 1))
    conn.commit()
    conn.close()

    identity = with_event_time_publication_window(
        build_temporal_identity("2026-01-15"), now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    retriever = WebEvidenceRetriever(_Base())
    result = retriever.retrieve("Tesla catalyst", ticker="TSLA", temporal_identity=identity,
                                cutoff="2026-01-15T21:00:00Z", requested_manifest_id="manifest",
                                top_k=8, candidate_depth=20)
    web = [item for item in result if getattr(item, "origin", None) == "web"]
    v2 = [item for item in web if "/v2/" in item.canonical_url]
    roundup = [item for item in web if "cnbc.com" in item.canonical_url]
    assert len(v2) == 12
    assert [item.canonical_url.rsplit("/", 1)[-1] for item in v2] == [str(i) for i in range(12)]
    assert len(roundup) == 1
    assert all(item.provider == "tavily" and item.content_state == "FULL_TEXT" for item in web)
    assert all("excluded" not in item.canonical_url for item in web)
    trace = retriever.web_trace(used_evidence_ids=tuple(item.chunk_id for item in web))
    assert trace["rows_offered"] == 13
    assert any(row.get("roundup_kind") == "premarket" for row in trace["offered_rows"])


def test_discovery_v2_serves_cnbc_segment_excerpt_with_page_level_provenance(tmp_path, monkeypatch):
    db = tmp_path / "web.sqlite3"
    monkeypatch.setenv("CATALYST_WEB_SEARCH_DB", str(db))
    from catalyst_data.ingestion.collectors import cnbc_roundups
    conn = cnbc_roundups._connect(db)
    conn.executemany("""INSERT INTO roundup_segments
        (session_date,roundup_kind,url,canonical_url,title,published_at,published_time_source,
         body_source,ticker,segment_index,segment_text,segment_sha256,page_sha256,fetched_at,credits)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
        ("2026-01-15", "premarket", "https://www.cnbc.com/page", "https://www.cnbc.com/page",
         "CNBC roundup", "2026-01-15T14:00:00Z", "jsonld.datePublished", "fetch", ticker,
         index, text, f"segment-{index}", "same-page-sha", "2026-01-20T00:00:00Z", 1)
        for ticker, index, text in (
            ("TSLA", 1, "Tesla shares rose after a distinct battery production expansion was announced."),
            ("TSLA", 2, "Tesla shares fell after a separate vehicle recall was announced."),
        )
    ])
    conn.commit(); conn.close()
    identity = with_event_time_publication_window(
        build_temporal_identity("2026-01-15"), now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    retriever = WebEvidenceRetriever(_Base())
    result = retriever.retrieve("Tesla catalyst", ticker="TSLA", temporal_identity=identity,
                                cutoff="2026-01-15T21:00:00Z", requested_manifest_id="manifest",
                                top_k=8, candidate_depth=20)
    web = [item for item in result if getattr(item, "origin", None) == "web"]
    assert len(web) == 2
    assert all(item.canonical_url.endswith(("#segment-1", "#segment-2")) for item in web)
    assert all("catalyst" not in item.content_text for item in web)
    assert len({item.content_hash for item in web}) == 1
    assert len({item.dedup_cluster_id for item in web}) == 1
    assert len({item.chunk_id for item in web}) == 2
    assert all(item.publisher == "CNBC" and item.origin == "web" for item in web)
    used = retriever.web_trace(used_evidence_ids=tuple(item.chunk_id for item in web))["used_rows"]
    assert {row["roundup_kind"] for row in used} == {"premarket"}
    assert {row["segment_index"] for row in used} == {1, 2}


def test_discovery_v2_preserves_cached_order_and_appends_roundup_segments(tmp_path, monkeypatch):
    db = tmp_path / "web.sqlite3"
    monkeypatch.setenv("CATALYST_WEB_SEARCH_DB", str(db))
    from catalyst_data.ingestion.collectors.web_discovery_v2 import (
        _connect as v2_connect,
    )
    conn = v2_connect(db)
    conn.execute("""INSERT INTO web_discovery_v2_rows
        (scope_key,session_date,family,canonical_url,url,publisher,publisher_family,published_at,
         date_source,title,body_text,body_source,html_sha256,scope,relevance,status,fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("scope-1", "2026-01-15", "family", "https://example.com/why/1", "https://example.com/why/1",
         "Example", "example.com", "2026-01-15T15:00:00Z", "published_at", "Tesla why story",
         "Tesla shares rallied after a distinct product launch moved into production. " * 5,
         "fetch", "a" * 64, "issuer", 1.0, "FULL_TEXT", "2026-01-20T00:00:00Z"))
    conn.execute("INSERT INTO web_discovery_v2_case_links VALUES (?,?,?,?,?)",
                 ("case", "TSLA", "2026-01-15", "scope-1", "family"))
    conn.commit(); conn.close()
    from catalyst_data.ingestion.collectors import cnbc_roundups
    conn = cnbc_roundups._connect(db)
    conn.execute("""INSERT INTO roundup_segments
        (session_date,roundup_kind,url,canonical_url,title,published_at,published_time_source,
         body_source,ticker,segment_index,segment_text,segment_sha256,page_sha256,fetched_at,credits)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("2026-01-15", "premarket", "https://www.cnbc.com/page", "https://www.cnbc.com/page",
         "Tesla roundup", "2026-01-15T14:00:00Z", "jsonld.datePublished", "fetch", "TSLA", 1,
         "Tesla shares rallied after a separate product launch moved into production.",
         "segment-hash", "page-hash", "2026-01-20T00:00:00Z", 1))
    conn.commit(); conn.close()
    identity = with_event_time_publication_window(
        build_temporal_identity("2026-01-15"), now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    retriever = WebEvidenceRetriever(_Base())
    result = retriever.retrieve("Tesla catalyst", ticker="TSLA", temporal_identity=identity,
                                cutoff="2026-01-15T21:00:00Z", requested_manifest_id="manifest",
                                top_k=8, candidate_depth=20)
    web = [item for item in result if getattr(item, "origin", None) == "web"]
    assert len(web) == 2
    assert web[0].canonical_url == "https://example.com/why/1"
    assert web[1].canonical_url.endswith("#segment-1")


def test_web_wrapper_preserves_observed_retrieval_contract(monkeypatch):
    from catalyst_agents.runtime import retrieval_adapter

    local = object()
    observation = object()

    class Adapter:
        def __init__(self, base):
            assert base is not None

        def retrieve_with_observations(self, query, **kwargs):
            assert query == "why"
            return (local,), observation

    monkeypatch.setattr(retrieval_adapter, "AgentRetrieverAdapter", Adapter)
    wrapper = WebEvidenceRetriever(_Base())
    evidence, observed = wrapper.retrieve_with_observations(
        "why", ticker="TSLA", temporal_identity=None, cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="manifest", top_k=8, candidate_depth=20)
    assert evidence == (local,)
    assert observed is observation


def test_web_wrapper_does_not_double_wrap_agent_adapter():
    local = object()
    observation = object()

    class AgentAdapter:
        def retrieve_with_observations(self, query, **kwargs):
            assert query == "why"
            return (local,), observation

    wrapper = WebEvidenceRetriever(AgentAdapter())
    evidence, observed = wrapper.retrieve_with_observations(
        "why", ticker="TSLA", temporal_identity=None, cutoff="2026-01-15T21:00:00Z",
        requested_manifest_id="manifest", top_k=8, candidate_depth=20)
    assert evidence == (local,)
    assert observed is observation


def test_snapshot_source_includes_web_body_source():
    from catalyst_agents.attribution.answer_snapshot import _quote

    source = SimpleNamespace(
        origin="web", body_source="tavily_raw", publisher="Example News",
        canonical_url="https://example.com/story", eligible_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
        evidence_id="web_1", source_class="reported_news",
    )
    quote = _quote(source, "Tesla announced a dated factory expansion.", "PRIMARY")
    assert quote["body_source"] == "tavily_raw"
    assert quote["origin"] == "web"


def _ordered_selection_fixture():
    identity = with_event_time_publication_window(
        build_temporal_identity("2026-01-15"), now=datetime(2026, 10, 10, tzinfo=timezone.utc)
    )
    first_body = (
        "Tesla announced a distinct manufacturing expansion at its Fremont factory. "
        "The expansion adds capacity for the next model year."
    )
    second_body = "Tesla opened a separate research office in Austin, Texas."
    rows = [
        {"scope_key": "TSLA", "canonical_url": "https://example.com/first",
         "html_sha256": "1" * 64, "published_at": "2026-01-15T15:00:00Z",
         "body_text": first_body, "title": "Tesla factory expansion", "publisher": "Example",
         "publisher_family": "example.com", "body_source": "fetch"},
        {"scope_key": "TSLA", "canonical_url": "https://example.com/second",
         "html_sha256": "2" * 64, "published_at": "2026-01-15T16:00:00Z",
         "body_text": second_body, "title": "Tesla research office", "publisher": "Example",
         "publisher_family": "example.com", "body_source": "fetch"},
    ]
    locals_by_query = {
        "first": SimpleNamespace(
            canonical_url="https://local.example/transcript", content_text=first_body,
            corpus_manifest_id="manifest", data_runtime_identity=None,
        ),
        "second": SimpleNamespace(
            canonical_url="https://local.example/filing",
            content_text="The filing describes an unrelated debt redemption.",
            corpus_manifest_id="manifest", data_runtime_identity=None,
        ),
    }
    kwargs = {
        "ticker": "TSLA", "temporal_identity": identity,
        "cutoff": "2026-01-15T21:00:00Z", "requested_manifest_id": "manifest",
        "top_k": 8, "candidate_depth": 20,
    }
    return rows, locals_by_query, kwargs


def _patch_ordered_selection_rows(monkeypatch, rows):
    from catalyst_data import web_evidence

    monkeypatch.setattr(web_evidence, "cached_discovery_v2_rows",
                        lambda *_args, **_kwargs: [dict(row) for row in rows])
    monkeypatch.setattr(web_evidence, "cached_roundup_segments", lambda *_args, **_kwargs: [])


def test_web_row_selection_waits_for_first_submitted_task(monkeypatch):
    rows, locals_by_query, kwargs = _ordered_selection_fixture()
    _patch_ordered_selection_rows(monkeypatch, rows)

    class SerialBase:
        def retrieve(self, query, **_kwargs):
            return (locals_by_query[query],)

    def web_urls(result):
        return [item.canonical_url for item in result if getattr(item, "origin", None) == "web"]

    class OrderedRetriever(WebEvidenceRetriever):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            self.second_waiting = threading.Event()

        def _selected_rows_for(self, *args, research_submission_index=None, **kw):
            if research_submission_index == 1:
                self.second_waiting.set()
            return super()._selected_rows_for(
                *args, research_submission_index=research_submission_index, **kw)

    for deadline in (time.monotonic() + 5, None):
        serial = WebEvidenceRetriever(SerialBase())
        serial_outputs = {
            query: web_urls(serial.retrieve(
                query, research_submission_index=index,
                research_deadline_monotonic=deadline, **kwargs))
            for index, query in enumerate(("first", "second"))
        }
        assert serial_outputs == {
            "first": ["https://example.com/second"],
            "second": ["https://example.com/second"],
        }

        retriever = OrderedRetriever(
            SerialBase(),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            second = pool.submit(
                retriever.retrieve, "second", research_submission_index=1,
                research_deadline_monotonic=deadline, **kwargs)
            assert retriever.second_waiting.wait(timeout=5)
            first = pool.submit(
                retriever.retrieve, "first", research_submission_index=0,
                research_deadline_monotonic=deadline, **kwargs)
            concurrent_outputs = {
                "first": web_urls(first.result(timeout=10)),
                "second": web_urls(second.result(timeout=10)),
            }
        assert concurrent_outputs == serial_outputs


def test_web_row_selection_timeout_fails_closed_deterministically(monkeypatch):
    rows, locals_by_query, kwargs = _ordered_selection_fixture()
    _patch_ordered_selection_rows(monkeypatch, rows)

    class SerialBase:
        def retrieve(self, query, **_kwargs):
            return (locals_by_query[query],)

    retriever = WebEvidenceRetriever(
        SerialBase(),
    )
    result = retriever.retrieve(
        "second", research_submission_index=1,
        research_deadline_monotonic=time.monotonic() + 0.05, **kwargs)
    assert [item for item in result if getattr(item, "origin", None) == "web"] == []

    leader_result = retriever.retrieve(
        "first", research_submission_index=0,
        research_deadline_monotonic=time.monotonic() + 5, **kwargs)
    assert [item for item in leader_result if getattr(item, "origin", None) == "web"] == []


def test_web_row_selection_without_deadline_uses_bounded_default(monkeypatch):
    from catalyst_agents.runtime import web_evidence

    rows, locals_by_query, kwargs = _ordered_selection_fixture()
    _patch_ordered_selection_rows(monkeypatch, rows)
    monkeypatch.setattr(web_evidence, "_DEFAULT_SELECTION_WAIT_SECONDS", 0.05)

    class SerialBase:
        def retrieve(self, query, **_kwargs):
            return (locals_by_query[query],)

    retriever = WebEvidenceRetriever(
        SerialBase(),
    )
    started = time.monotonic()
    result = retriever.retrieve("second", research_submission_index=1, **kwargs)

    assert time.monotonic() - started >= 0.04
    assert [item for item in result if getattr(item, "origin", None) == "web"] == []


def test_finished_leader_releases_waiters_with_empty_selection(monkeypatch):
    rows, locals_by_query, kwargs = _ordered_selection_fixture()
    _patch_ordered_selection_rows(monkeypatch, rows)

    class SerialBase:
        def retrieve(self, query, **_kwargs):
            return (locals_by_query[query],)

    class WaitingRetriever(WebEvidenceRetriever):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            self.second_waiting = threading.Event()

        def _selected_rows_for(self, *args, research_submission_index=None, **kw):
            if research_submission_index == 1:
                self.second_waiting.set()
            return super()._selected_rows_for(
                *args, research_submission_index=research_submission_index, **kw)

    retriever = WaitingRetriever(
        SerialBase(),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        second = pool.submit(
            retriever.retrieve, "second", research_submission_index=1,
            research_deadline_monotonic=time.monotonic() + 5, **kwargs)
        assert retriever.second_waiting.wait(timeout=5)
        started = time.monotonic()
        retriever.finish_research_submission(0)
        result = second.result(timeout=1)

    assert time.monotonic() - started < 1
    assert [item for item in result if getattr(item, "origin", None) == "web"] == []
