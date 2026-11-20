"""Inside a run, SEC lookups read the local cache only and never fetch."""
from __future__ import annotations

import json

import pytest
from catalyst_data.ingestion.collectors import sec, sec_filings


def _submissions(accessions: list[str], archives: list[str] = ()) -> dict:
    n = len(accessions)
    return {
        "filings": {
            "recent": {
                "form": ["8-K"] * n,
                "accessionNumber": accessions,
                "acceptanceDateTime": ["2026-04-10T16:05:00.000Z"] * n,
                "items": ["2.02,9.01"] * n,
            },
            "files": [{"name": name} for name in archives],
        }
    }


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path)
    monkeypatch.setattr(sec_filings, "_cik_for", lambda _ticker: "0000000001")
    monkeypatch.delenv("CATALYST_SEC_OFFLINE", raising=False)
    monkeypatch.setenv("SEC_USER_AGENT", "test agent")

    def no_network(*_args, **_kwargs):
        raise AssertionError("a cached-only lookup must not call SEC")

    monkeypatch.setattr(sec, "index", no_network)
    import requests

    monkeypatch.setattr(requests, "get", no_network)
    return tmp_path


def test_missing_index_page_keeps_submissions_time_without_fetching(cache):
    (cache / "0000000001.json").write_text(json.dumps(_submissions(["0000000001-26-000001"])))
    with sec_filings.cached_only():
        rows = sec_filings.filings_8k("ABC")
    assert [row["accession"] for row in rows] == ["0000000001-26-000001"]
    assert rows[0]["time_flag"] == "index_missing"


def test_cached_index_page_is_still_used(cache):
    (cache / "0000000001.json").write_text(json.dumps(_submissions(["0000000001-26-000001"])))
    (cache / "CIK0000000001-0000000001-26-000001-index.json").write_text(
        json.dumps({"accession": "0000000001-26-000001", "accepted_at": "2026-04-10T16:04:30Z"})
    )
    with sec_filings.cached_only():
        rows = sec_filings.filings_8k("ABC")
    assert rows[0]["time_flag"] != "index_missing"


def test_missing_submissions_and_archive_mean_no_filings_not_a_fetch(cache):
    with sec_filings.cached_only():
        assert sec_filings.filings_8k("ABC") == []
    (cache / "0000000001.json").write_text(
        json.dumps(_submissions(["0000000001-26-000001"], ["CIK0000000001-submissions-001.json"]))
    )
    with sec_filings.cached_only():
        assert len(sec_filings.filings_8k("ABC")) == 1


def test_scope_ends_with_the_block(cache):
    with sec_filings.cached_only():
        assert sec_filings._CACHED_ONLY.get() is True
    assert sec_filings._CACHED_ONLY.get() is False
