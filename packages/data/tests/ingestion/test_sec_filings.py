from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from catalyst_data.ingestion.collectors import sec_filings
from catalyst_data.ingestion.time import timefields

CIK = "0000001234"


def _recent():
    return {
        "accessionNumber": ["0000001234-26-000003"],
        "acceptanceDateTime": ["2026-09-14T19:00:00.000Z"],
        "form": ["8-K"],
        "items": ["2.02"],
    }


def _history():
    return {
        "accessionNumber": ["0000001234-25-000001", "0000001234-25-000002"],
        "acceptanceDateTime": ["2025-01-02T15:00:00.000Z", "2025-01-03T15:00:00.000Z"],
        "form": ["8-K", "10-K"],
        "items": ["2.02", ""],
    }


def test_filings_8k_reads_archived_submissions_listed_in_files(monkeypatch, tmp_path):
    monkeypatch.setattr(sec_filings, "_load", lambda _ticker: {
        "filings": {
            "recent": _recent(),
            "files": [{"name": "CIK0000001234-submissions-001.json"}],
        }
    })
    monkeypatch.setattr(sec_filings.sec, "cik_for", lambda _ticker: CIK)
    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path)
    monkeypatch.setattr(
        sec_filings,
        "_index_accepted_at",
        lambda _cik, accession: timefields.stored(
            "2025-01-02T15:00:00Z" if accession.startswith("0000001234-25")
            else "2026-09-14T15:00:00Z"
        ),
    )
    monkeypatch.delenv("CATALYST_SEC_OFFLINE", raising=False)
    monkeypatch.setenv("SEC_USER_AGENT", "fixture-agent")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"filings": _history()}

    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        return Response()

    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=get))
    result = sec_filings.filings_8k("EXM")

    assert [row["accession"] for row in result] == [
        "0000001234-25-000001", "0000001234-26-000003",
    ]
    assert result[-1]["accepted_at"] == timefields.stored("2026-09-14T15:00:00Z")
    assert result[-1]["json_accepted_at"] == timefields.stored("2026-09-14T19:00:00Z")
    assert result[-1]["time_flag"] == "api_offset"
    assert requests[0][0] == (
        "https://data.sec.gov/submissions/CIK0000001234-submissions-001.json"
    )
    assert json.loads((tmp_path / "CIK0000001234-submissions-001.json").read_text()) == {
        "filings": _history(),
    }


@pytest.mark.parametrize(
    "field", ["accessionNumber", "acceptanceDateTime", "form", "items"]
)
def test_filing_rows_rejects_misaligned_core_arrays(field):
    recent = _history()
    recent[field].pop()

    with pytest.raises(ValueError, match="parallel filing arrays"):
        sec_filings._filing_rows(recent)


@pytest.mark.parametrize("malformed_value", ["missing", "not-an-array"])
def test_filing_rows_rejects_missing_or_non_list_core_arrays(malformed_value):
    recent = _history()
    if malformed_value == "missing":
        del recent["items"]
    else:
        recent["items"] = malformed_value

    with pytest.raises(ValueError, match="parallel filing arrays"):
        sec_filings._filing_rows(recent)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("accessionNumber", None),
        ("form", None),
        ("acceptanceDateTime", "2025-01-02"),
        ("items", None),
    ],
)
def test_filing_rows_rejects_invalid_core_values_on_8k_rows(field, bad_value):
    recent = _history()
    recent[field][0] = bad_value

    with pytest.raises(ValueError):
        sec_filings._filing_rows(recent)


def test_filing_rows_does_not_require_8k_values_from_non_8k_rows():
    recent = _history()
    recent["form"][0] = "10-K"
    recent["accessionNumber"][0] = None
    recent["acceptanceDateTime"][0] = None
    recent["items"][0] = None

    assert sec_filings._filing_rows(recent) == []


def test_filings_8k_uses_archived_file_cache_in_offline_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(sec_filings, "_load", lambda _ticker: {
        "filings": {
            "recent": _recent(),
            "files": [{"name": "CIK0000001234-submissions-001.json"}],
        }
    })
    monkeypatch.setattr(sec_filings.sec, "cik_for", lambda _ticker: CIK)
    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path)
    monkeypatch.setattr(
        sec_filings, "_index_accepted_at",
        lambda _cik, _accession: timefields.stored("2026-09-14T19:00:00Z"),
    )
    monkeypatch.setenv("CATALYST_SEC_OFFLINE", "1")
    (tmp_path / "CIK0000001234-submissions-001.json").write_text(json.dumps({"filings": _history()}))
    monkeypatch.setitem(
        sys.modules,
        "requests",
        SimpleNamespace(get=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network"))),
    )

    result = sec_filings.filings_8k("EXM")

    assert [row["accession"] for row in result] == [
        "0000001234-25-000001", "0000001234-26-000003",
    ]


def test_normalize_ticker_unifies_dot_and_hyphen_class_share_forms():
    assert sec_filings.normalize_ticker(" brk.b ") == "BRK-B"
    assert sec_filings.normalize_ticker("BRK-B") == "BRK-B"


def test_cik_lookup_accepts_sec_dot_class_share_form(monkeypatch):
    asked = []

    def lookup(ticker):
        asked.append(ticker)
        return CIK if ticker == "BRK.B" else None

    monkeypatch.setattr(sec_filings.sec, "cik_for", lookup)

    assert sec_filings._cik_for("brk-b") == CIK
    assert asked == ["BRK-B", "BRK.B"]


def test_index_acceptance_time_is_cached_and_required_for_offline_replay(monkeypatch, tmp_path):
    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path)
    monkeypatch.delenv("CATALYST_SEC_OFFLINE", raising=False)
    monkeypatch.setenv("SEC_USER_AGENT", "fixture-agent")
    monkeypatch.setattr(sec_filings.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        sec_filings.sec, "index",
        lambda _cik, _accession, **_kwargs: ([], "2026-09-14T15:00:00Z"),
    )

    accepted = sec_filings._index_accepted_at(CIK, "0000001234-26-000003")

    assert accepted == timefields.stored("2026-09-14T15:00:00Z")
    monkeypatch.setenv("CATALYST_SEC_OFFLINE", "1")
    monkeypatch.setattr(
        sec_filings.sec, "index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )
    assert sec_filings._index_accepted_at(CIK, "0000001234-26-000003") == accepted

    with pytest.raises(FileNotFoundError, match="not cached"):
        sec_filings._index_accepted_at(CIK, "0000001234-25-000099")


def test_missing_index_time_is_retried_and_positive_result_is_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path)
    monkeypatch.delenv("CATALYST_SEC_OFFLINE", raising=False)
    monkeypatch.setenv("SEC_USER_AGENT", "fixture-agent")
    monkeypatch.setattr(sec_filings.time, "sleep", lambda _seconds: None)
    accession = "0000001234-26-000003"
    cache = tmp_path / f"CIK{CIK}-{accession}-index.json"
    calls = []

    def index(*_args, **_kwargs):
        calls.append(1)
        return [], None if len(calls) == 1 else "2026-09-14T15:00:00Z"

    monkeypatch.setattr(sec_filings.sec, "index", index)

    assert sec_filings._index_accepted_at(CIK, accession) is None
    assert not cache.exists()
    assert sec_filings._index_accepted_at(CIK, accession) == timefields.stored("2026-09-14T15:00:00Z")
    assert len(calls) == 2
    assert json.loads(cache.read_text())["accepted_at"] == "2026-09-14T15:00:00Z"
    assert sec_filings._index_accepted_at(CIK, accession) == timefields.stored("2026-09-14T15:00:00Z")
    assert len(calls) == 2


def test_old_null_index_cache_is_refetched_online_but_stays_closed_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(sec_filings, "_dir", lambda: tmp_path)
    monkeypatch.setenv("CATALYST_SEC_OFFLINE", "1")
    monkeypatch.setenv("SEC_USER_AGENT", "fixture-agent")
    monkeypatch.setattr(sec_filings.time, "sleep", lambda _seconds: None)
    accession = "0000001234-26-000003"
    cache = tmp_path / f"CIK{CIK}-{accession}-index.json"
    cache.write_text(json.dumps({"accession": accession, "accepted_at": None}))
    calls = []

    def index(*_args, **_kwargs):
        calls.append(1)
        return [], "2026-09-14T15:00:00Z"

    monkeypatch.setattr(sec_filings.sec, "index", index)

    assert sec_filings._index_accepted_at(CIK, accession) is None
    assert calls == []
    monkeypatch.delenv("CATALYST_SEC_OFFLINE")
    assert sec_filings._index_accepted_at(CIK, accession) == timefields.stored("2026-09-14T15:00:00Z")
    assert len(calls) == 1
    assert json.loads(cache.read_text())["accepted_at"] == "2026-09-14T15:00:00Z"
