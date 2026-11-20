"""Stored titles for the subject gate and abstain leads: legacy cohort first, the old store must exist otherwise."""
from __future__ import annotations

import sqlite3

import pytest
from catalyst_agents.attribution import abstain_context, ceilings
from catalyst_data.storage.errors import StoreReadError


class _Cohort:
    def __init__(self):
        self.asked = []

    def titles_for_chunks(self, ids):
        self.asked.append(list(ids))
        return {i: "Acme raises guidance" for i in ids}


def _env(monkeypatch, tmp_path, *, store: bool):
    store_path = tmp_path / "news_bodies_v2.sqlite3"
    (tmp_path / "sec_company_tickers.json").write_text("{}")
    if store:
        with sqlite3.connect(store_path) as conn:
            conn.execute("CREATE TABLE web_body_chunks (chunk_id TEXT, title TEXT)")
            conn.execute("INSERT INTO web_body_chunks VALUES ('a:web_body:1', 'Old store title')")
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(store_path))
    return store_path


def test_cohort_is_read_first_even_when_the_old_store_is_absent(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path, store=False)
    cohort = _Cohort()
    monkeypatch.setattr("catalyst_data.retrieval.legacy_read.configured_reader", lambda: cohort)
    titles = ceilings._title_source()(["a:web_body:1", "fact:2"])
    assert titles == {"a:web_body:1": "Acme raises guidance"} and cohort.asked == [["a:web_body:1"]]


def test_old_store_is_read_without_a_cohort(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path, store=True)
    monkeypatch.setattr("catalyst_data.retrieval.legacy_read.configured_reader", lambda: None)
    assert ceilings._title_source()(["a:web_body:1"]) == {"a:web_body:1": "Old store title"}


def test_a_named_but_missing_old_store_without_a_cohort_raises(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path, store=False)
    monkeypatch.setattr("catalyst_data.retrieval.legacy_read.configured_reader", lambda: None)
    with pytest.raises(StoreReadError, match="does not exist"):
        ceilings._title_source()


def test_no_body_store_configured_keeps_the_title_rule_off(monkeypatch):
    monkeypatch.delenv("CATALYST_NEWS_BODIES_DB", raising=False)
    monkeypatch.setattr("catalyst_data.retrieval.legacy_read.configured_reader", lambda: None)
    assert ceilings._title_source() is None


def test_an_unreadable_old_store_raises_instead_of_returning_no_titles(tmp_path, monkeypatch):
    store = _env(monkeypatch, tmp_path, store=False)
    store.write_bytes(b"")  # exists, but has no web_body_chunks table
    monkeypatch.setattr("catalyst_data.retrieval.legacy_read.configured_reader", lambda: None)
    with pytest.raises(StoreReadError, match="web_body_chunks"):
        ceilings._title_source()(["a:web_body:1"])


def test_abstain_leads_use_the_same_source(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ceilings, "_title_source", lambda: calls.append(1))  # returns None: no store configured
    assert abstain_context.related_news_items(object(), "ACME") == [] and calls == [1]


# ---- through the protected callers: the subject gate and the abstain leads give the same results whether the
# titles come from a cohort (no old store on disk) or from the old store

_EID = "web:abc:web_body:0001"


def _gate(target, quote):
    from types import SimpleNamespace as NS

    from catalyst_agents.attribution.analyst import HypothesisRole
    from catalyst_agents.attribution.assessment import _subject_ceiling

    hypothesis = NS(normalized_role=HypothesisRole.PRIMARY, cause_type=NS(value="EARNINGS"),
                    statement=f"“{quote}” — Benzinga, 2026-03-03 15:05 UTC, https://example.com/a",
                    supporting_evidence_ids=(_EID,))
    item = NS(evidence_id=_EID, source_class=NS(value="reported_news"), canonical_url="https://example.com/a",
              excerpt_text=f"Lead. {quote} More.")
    pack = NS(observation=NS(ticker=target, sector_ticker=None, commodity_ticker=None), evidence_inventory=(item,))
    return _subject_ceiling((hypothesis,), pack)


def _leads(ticker):
    from datetime import datetime, timezone
    from types import SimpleNamespace as NS

    start, end = datetime(2026, 3, 2, tzinfo=timezone.utc), datetime(2026, 3, 4, tzinfo=timezone.utc)
    temporal = NS(publication_window=lambda: (start, end), evidence_window_start_at=None)
    item = NS(evidence_id=_EID, eligible_at=datetime(2026, 3, 3, 15, tzinfo=timezone.utc),
              canonical_url="https://example.com/a", publisher="Benzinga")
    return abstain_context.related_news_items(NS(temporal_identity=temporal, evidence_inventory=(item,)), ticker)


def _titles_from(source, monkeypatch, tmp_path, title, *, name_map=True):
    import json

    if name_map:
        (tmp_path / "sec_company_tickers.json").write_text(json.dumps({"0": {"ticker": "DE", "title": "DEERE & CO"}}))
    store = tmp_path / "bodies.sqlite3"
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(store))
    if source == "cohort":
        cohort = _Cohort()
        cohort.titles_for_chunks = lambda ids: {i: title for i in ids}
        monkeypatch.setattr("catalyst_data.retrieval.legacy_read.configured_reader", lambda: cohort)
    else:
        with sqlite3.connect(store) as conn:
            conn.execute("CREATE TABLE web_body_chunks (chunk_id TEXT, title TEXT)")
            conn.execute("INSERT INTO web_body_chunks VALUES (?, ?)", (_EID, title))
        monkeypatch.setattr("catalyst_data.retrieval.legacy_read.configured_reader", lambda: None)


_QUOTE = "The U.S. large equipment maker reported adjusted earnings of $1.58, beating estimates."


@pytest.mark.parametrize("source", ["cohort", "old_store"])
@pytest.mark.parametrize("title, expected", [
    ("Deere Beats Q1 Estimates", (None, "")),
    ("Caterpillar Beats Q1 Estimates", ("ABSTAIN", "primary_off_subject:EARNINGS")),
])
def test_subject_gate_same_outcome_from_cohort_or_old_store(source, title, expected, tmp_path, monkeypatch):
    _titles_from(source, monkeypatch, tmp_path, title)
    status, reason = _gate("DE", _QUOTE)
    assert (getattr(status, "value", status), reason) == expected


@pytest.mark.parametrize("source", ["cohort", "old_store"])
def test_abstain_leads_same_from_cohort_or_old_store(source, tmp_path, monkeypatch):
    _titles_from(source, monkeypatch, tmp_path, "Deere raises its outlook")
    assert _leads("DE") == [{"title": "Deere raises its outlook", "publisher": "Benzinga",
                             "published_at": "2026-03-03 15:00 UTC", "url": "https://example.com/a"}]


def test_missing_name_map_keeps_the_gate_off_when_the_store_is_present(tmp_path, monkeypatch):
    _titles_from("old_store", monkeypatch, tmp_path, "Caterpillar Beats Q1 Estimates", name_map=False)
    assert _gate("DE", _QUOTE) == (None, "")


def test_missing_store_fails_the_gate_even_without_a_name_map(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(tmp_path / "absent.sqlite3"))
    monkeypatch.setattr("catalyst_data.retrieval.legacy_read.configured_reader", lambda: None)
    with pytest.raises(StoreReadError, match="does not exist"):
        _gate("DE", _QUOTE)
