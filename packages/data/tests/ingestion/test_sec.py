from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from catalyst_data.ingestion.collectors import sec, sec_structure

CIK = "0000001234"
ACCESSION = "0000001234-26-000001"
SUBMISSIONS = {
    "name": "Example Corp",
    "filings": {
        "recent": {
            "accessionNumber": [ACCESSION, "0000001234-26-000002", "0000001234-26-000003"],
            "filingDate": ["2026-09-14", "2026-09-13", "2026-09-14"],
            "acceptanceDateTime": [
                "2026-09-14T19:00:00.000Z",
                "2026-09-11T19:00:00.000Z",
                "2026-09-14T19:00:00.000Z",
            ],
            "form": ["8-K", "8-K", "8-K"],
            "items": ["2.02,9.01", "2.02", "3.01"],
            "primaryDocument": ["form8k.htm", "outside.htm", "irrelevant.htm"],
            "primaryDocDescription": ["Results", "Outside", "Irrelevant"],
        }
    },
}
ACCEPTED = '<div class="formGrouping"><div class="infoHead">Accepted</div>\n<div class="info">{}</div></div>'
INDEX_HTML = ACCEPTED.format("2026-09-14 15:00:00") + """<html><table>
<tr><td>1</td><td>Annual report</td><td><a href="/Archives/edgar/data/1234/000000123426000001/earningsrelease.htm">earningsrelease.htm</a></td><td>EX-99.1</td></tr>
<tr><td>2</td><td>Graphic</td><td><a href="/Archives/edgar/data/1234/000000123426000001/chart.jpg">chart.jpg</a></td><td>GRAPHIC</td></tr>
<tr><td>3</td><td>Current report</td><td><a href="/ix?doc=/Archives/edgar/data/1234/000000123426000001/form8k.htm">form8k.htm</a></td><td>8-K</td></tr>
</table></html>"""
PROSE = "The company reported quarterly revenue growth and described business operations across its major segments. " * 5


class Response:
    status_code = 200

    def __init__(self, *, payload=None, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError("http failure")

    def json(self):
        return self._payload


def test_list_8k_filters_by_acceptance_window_and_relevant_items():
    result = sec.list_8k(
        CIK, "2026-09-14T14:00:00Z", "2026-09-14T20:00:00Z",
        http_get=lambda *_args, **_kwargs: Response(payload=SUBMISSIONS), user_agent="fixture-agent",
    )
    assert result == [{
        "accession": ACCESSION,
        "accepted_at": "2026-09-14T19:00:00Z",
        "items": ["2.02", "9.01"],
        "primary_document": "form8k.htm",
        "description": "Results",
        "company": "Example Corp",
    }]


@pytest.mark.parametrize(
    "field", ["accessionNumber", "acceptanceDateTime", "form", "items"]
)
def test_list_8k_rejects_misaligned_core_filing_arrays(field):
    payload = json.loads(json.dumps(SUBMISSIONS))
    payload["filings"]["recent"][field].pop()

    with pytest.raises(ValueError, match="parallel filing arrays"):
        sec.list_8k(
            CIK,
            "2026-09-14T14:00:00Z",
            "2026-09-14T20:00:00Z",
            http_get=lambda *_args, **_kwargs: Response(payload=payload),
            user_agent="fixture-agent",
        )


@pytest.mark.parametrize("malformed_value", ["missing", "not-an-array"])
def test_list_8k_rejects_missing_or_non_list_core_arrays(malformed_value):
    payload = json.loads(json.dumps(SUBMISSIONS))
    if malformed_value == "missing":
        del payload["filings"]["recent"]["items"]
    else:
        payload["filings"]["recent"]["items"] = malformed_value

    with pytest.raises(ValueError, match="parallel filing arrays"):
        sec.list_8k(
            CIK,
            "2026-09-14T14:00:00Z",
            "2026-09-14T20:00:00Z",
            http_get=lambda *_args, **_kwargs: Response(payload=payload),
            user_agent="fixture-agent",
        )


def test_list_8k_keeps_optional_metadata_arrays_optional():
    payload = json.loads(json.dumps(SUBMISSIONS))
    recent = payload["filings"]["recent"]
    recent.pop("primaryDocument")
    recent.pop("primaryDocDescription")

    result = sec.list_8k(
        CIK,
        "2026-09-14T14:00:00Z",
        "2026-09-14T20:00:00Z",
        http_get=lambda *_args, **_kwargs: Response(payload=payload),
        user_agent="fixture-agent",
    )

    assert len(result) == 1
    assert result[0]["primary_document"] is None
    assert result[0]["description"] is None


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("accessionNumber", None),
        ("form", None),
        ("acceptanceDateTime", "2026-09-14"),
        ("items", None),
    ],
)
def test_list_8k_rejects_invalid_core_values_on_8k_rows(field, bad_value):
    payload = json.loads(json.dumps(SUBMISSIONS))
    payload["filings"]["recent"][field][0] = bad_value

    with pytest.raises(ValueError):
        sec.list_8k(
            CIK,
            "2026-09-14T14:00:00Z",
            "2026-09-14T20:00:00Z",
            http_get=lambda *_args, **_kwargs: Response(payload=payload),
            user_agent="fixture-agent",
        )


def test_intake_records_invalid_8k_core_values_as_error_not_empty():
    payload = json.loads(json.dumps(SUBMISSIONS))
    payload["filings"]["recent"]["acceptanceDateTime"][0] = "2026-09-14"

    rows, result = sec.intake(
        "ABC",
        "2026-09-14",
        http_get=lambda *_args, **_kwargs: Response(payload=payload),
        sleep=lambda _seconds: None,
        user_agent="fixture-agent",
        tickers_json={"0": {"ticker": "ABC", "title": "Example Corp", "cik_str": 1234}},
    )

    assert rows == []
    assert result == "ERROR"


def test_list_8k_does_not_validate_8k_fields_on_non_8k_rows():
    payload = json.loads(json.dumps(SUBMISSIONS))
    recent = payload["filings"]["recent"]
    recent["form"][0] = "10-K"
    recent["accessionNumber"][0] = None
    recent["acceptanceDateTime"][0] = None
    recent["items"][0] = None

    assert sec.list_8k(
        CIK,
        "2026-09-14T14:00:00Z",
        "2026-09-14T20:00:00Z",
        http_get=lambda *_args, **_kwargs: Response(payload=payload),
        user_agent="fixture-agent",
    ) == []


def test_exhibits_keeps_ex99_html_and_uses_primary_doc_when_no_exhibits():
    urls = []

    def get(url, **_kwargs):
        urls.append(url)
        return Response(text=INDEX_HTML)

    found = sec.exhibits(1234, ACCESSION, http_get=get, user_agent="fixture-agent")
    assert found == [{
        "type": "EX-99.1", "filename": "earningsrelease.htm",
        "url": "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/earningsrelease.htm",
    }]
    assert urls == [f"https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/{ACCESSION}-index.htm"]

    fallback = sec.exhibits(
        1234, "0000001234-26-000004",
        http_get=lambda *_args, **_kwargs: Response(text=INDEX_HTML.replace("EX-99.1", "GRAPHIC")),
        user_agent="fixture-agent",
    )
    assert fallback == [{
        "type": "8-K", "filename": "form8k.htm",
        "url": "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/form8k.htm",
    }]


def test_sec_index_without_supported_document_is_retryable(monkeypatch):
    from catalyst_data.ingestion.storage import page_versions

    monkeypatch.setattr(sec, "index", lambda *_a, **_k: ([], "2026-03-03T20:30:00Z"))
    monkeypatch.setattr(page_versions, "record_times", lambda *_a, **_k: None)
    filing = {"accession": "a", "accepted_at": "2026-03-03T20:30:00Z",
              "items": ["2.02"], "company": "A", "description": ""}
    rows, failed = sec.rows_for_filing(
        "1", "AAA", filing, http_get=lambda *_a, **_k: None, user_agent="fixture",
        window=("2026-03-02T21:00:00Z", "2026-03-03T21:00:00Z"),
    )
    assert rows == []
    assert failed is True


def test_unextractable_sec_exhibit_is_retryable(monkeypatch):
    from catalyst_data.ingestion.storage import page_versions

    monkeypatch.setattr(sec, "index", lambda *_a, **_k: ([{
        "url": "https://www.sec.gov/Archives/a.htm", "type": "EX-99.1",
        "filename": "a.htm",
    }], "2026-03-03T20:30:00Z"))
    monkeypatch.setattr(page_versions, "record_times", lambda *_a, **_k: None)
    monkeypatch.setattr(sec, "exhibit_text", lambda _html: None)
    filing = {"accession": "a", "accepted_at": "2026-03-03T20:30:00Z",
              "items": ["2.02"], "company": "A", "description": ""}
    rows, failed = sec.rows_for_filing(
        "1", "AAA", filing, http_get=lambda *_a, **_k: Response(text="<html></html>"),
        user_agent="fixture", window=("2026-03-02T21:00:00Z", "2026-03-03T21:00:00Z"),
    )
    assert rows == []
    assert failed is True


def test_exhibit_text_keeps_prose_cells_and_drops_numeric_table_cells():
    # Releases laid out with tables (Visa) keep their prose cells; financial
    # figure cells fail the prose filter and are never flattened into text.
    numeric_row = "<tr><td>Net revenue</td><td>$11,195</td><td>$9,594</td><td>17%</td></tr>"
    parsed = sec.exhibit_text(
        f"<html><body><table><tr><td>{PROSE}</td></tr>{numeric_row}</table></body></html>"
    )
    assert parsed == PROSE.strip()
    assert "11,195" not in parsed and "Net revenue" not in parsed
    numbers_only = sec.exhibit_text(f"<table>{numeric_row * 20}</table>")
    assert numbers_only is None


def test_intake_is_disabled_without_user_agent(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    rows, result = sec.intake("ABC", "2026-09-14", now=datetime(2026, 9, 14, 22, tzinfo=timezone.utc))
    assert rows == []
    assert result == "DISABLED"


def test_sec_exhibit_units_count_is_read_only_and_reports_missing_or_empty_table(tmp_path):
    from catalyst_data.storage.errors import StoreReadError

    missing_table = tmp_path / "missing-table.sqlite3"
    sqlite3.connect(missing_table).close()
    with pytest.raises(StoreReadError):
        sec_structure.units_count(missing_table)

    empty_table = tmp_path / "empty-table.sqlite3"
    with sqlite3.connect(empty_table) as conn:
        conn.execute(sec_structure.TABLE)
    assert sec_structure.units_count(empty_table) == 0


def test_sec_exhibit_units_count_reads_existing_rows_without_mutation(tmp_path):
    path = tmp_path / "units.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(sec_structure.TABLE)
        conn.execute(
            "INSERT INTO sec_exhibit_units VALUES (?,?,?,?)",
            ("https://sec.example/exhibit", "0001", "{}", "2026-11-04T00:00:00Z"),
        )

    before = path.stat().st_size
    assert sec_structure.units_count(path) == 1
    assert path.stat().st_size == before


def test_intake_creates_body_rows_at_acceptance_time():
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if "submissions" in url:
            return Response(payload=SUBMISSIONS)
        if url.endswith("-index.htm"):
            return Response(text=INDEX_HTML)
        return Response(text=f"<html><body><p>{PROSE}</p></body></html>")

    now = datetime(2026, 9, 14, 22, tzinfo=timezone.utc)
    rows, result = sec.intake(
        "ABC", "2026-09-14", http_get=get, sleep=lambda _seconds: None,
        user_agent="fixture-agent", tickers_json={"0": {"ticker": "ABC", "title": "Example Corp", "cik_str": 1234}},
        now=now,
    )
    assert result == "OK"
    assert len(rows) == 1
    structure = rows[0].pop("structure")  # v2.1: the exhibit's units travel with the row
    assert structure["units"] and rows[0].pop("accession")
    assert rows[0] == {
        "asset_id": "web:" + hashlib.sha256(rows[0]["url"].encode()).hexdigest()[:32],
        "text": PROSE.strip(),
        "url": "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/earningsrelease.htm",
        "title": "Example Corp (ABC) Form 8-K EX-99.1: Results",
        "publisher": "SEC filing",
        "source_class": "issuer_disclosure",
        "eligible_at": "2026-09-14T19:00:00Z",
        "fetched_at": "2026-09-14T22:00:00Z",
        "body_sha256": hashlib.sha256(PROSE.strip().encode()).hexdigest(),
        "tickers": ["ABC"],
    }
    assert all(call[1]["headers"]["User-Agent"] == "fixture-agent" for call in calls)


def test_intake_errors_never_expose_user_agent():
    secret_agent = "private-sec-agent-value"

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"failed request with {secret_agent}")

    rows, result = sec.intake(
        "ABC", "2026-09-14", http_get=fail, sleep=lambda _seconds: None,
        user_agent=secret_agent,
        tickers_json={"0": {"ticker": "ABC", "title": "Example Corp", "cik_str": 1234}},
    )
    assert rows == []
    assert result == "ERROR"
    assert secret_agent not in result


def _intake(session, json_time, accepted_et, monkeypatch, tmp_path):
    """One 2.02 8-K whose submissions JSON says ``json_time`` and whose index page says ``accepted_et``."""
    from catalyst_data.ingestion.storage import page_versions
    monkeypatch.setattr(page_versions, "_path", lambda: tmp_path / "page_versions.sqlite3")
    subs = json.loads(json.dumps(SUBMISSIONS))
    subs["filings"]["recent"]["acceptanceDateTime"][0] = json_time
    html = INDEX_HTML.replace("2026-09-14 15:00:00", accepted_et) if accepted_et else INDEX_HTML.split("<html>", 1)[1]

    def get(url, **_):
        if "submissions" in url:
            return Response(payload=subs)
        if url.endswith("-index.htm"):
            return Response(text=html)
        return Response(text=f"<html><body><p>{PROSE}</p></body></html>")

    rows, result = sec.intake("ABC", session, http_get=get, sleep=lambda _s: None, user_agent="fixture-agent",
                              tickers_json={"0": {"ticker": "ABC", "title": "Example Corp", "cik_str": 1234}},
                              now=datetime(2026, 9, 16, 22, tzinfo=timezone.utc))
    import sqlite3
    db = tmp_path / "page_versions.sqlite3"
    flags = [r[0] for r in sqlite3.connect(db).execute("SELECT flag FROM body_times")] if db.exists() else []
    return rows, result, flags


def test_the_index_page_time_replaces_a_json_time_four_hours_late(monkeypatch, tmp_path):
    # EDGAR accepted 15:00 EDT (19:00Z); the JSON says 23:00Z, as for STZ 2026-04-08
    rows, result, flags = _intake("2026-09-14", "2026-09-14T23:00:00.000Z", "2026-09-14 15:00:00", monkeypatch, tmp_path)
    assert result == "OK" and rows[0]["eligible_at"] == "2026-09-14T19:00:00Z" and flags == ["api_offset"]


def test_a_midday_filing_stays_in_its_own_session_and_not_the_next(monkeypatch, tmp_path):
    # accepted 13:00 EDT on 09-14; the JSON's 21:00Z is after that day's 16:00 ET cutoff
    rows, _, _ = _intake("2026-09-14", "2026-09-14T21:00:00.000Z", "2026-09-14 13:00:00", monkeypatch, tmp_path)
    assert [r["eligible_at"] for r in rows] == ["2026-09-14T17:00:00Z"]
    rows, result, _ = _intake("2026-09-15", "2026-09-14T21:00:00.000Z", "2026-09-14 13:00:00", monkeypatch, tmp_path)
    assert rows == [] and result == "EMPTY"  # not a "new event after the previous close" on 09-15


def test_an_index_page_without_an_acceptance_time_fails_the_item(monkeypatch, tmp_path):
    rows, result, flags = _intake("2026-09-14", "2026-09-14T19:00:00.000Z", None, monkeypatch, tmp_path)
    assert rows == [] and result == "ERROR" and flags == ["index_missing"]


def test_acceptance_flags():
    assert sec.acceptance("2026-01-05T02:29:20Z", "2026-01-04T21:29:20Z") == ("2026-01-04T21:29:20Z", "api_offset")
    assert sec.acceptance("2026-01-04T21:30:00Z", "2026-01-04T21:29:20Z")[1] == "agree"
    assert sec.acceptance("2026-01-04T23:00:00Z", "2026-01-04T21:29:20Z")[1] == "disagree"
    assert sec.index_accepted(ACCEPTED.format("2026-01-04 16:29:20")) == "2026-01-04T21:29:20Z"  # EST


def test_an_index_page_is_fetched_once_and_then_read_from_the_cache(tmp_path):
    """A filing's index page never changes: the daily job must not re-fetch every 8-K of an issuer's history."""
    calls = []

    def http_get(url, **_kwargs):
        calls.append(url)
        return Response(text=INDEX_HTML)

    first = sec.index(CIK, ACCESSION, http_get=http_get, user_agent="fixture", cache_dir=tmp_path)
    second = sec.index(CIK, ACCESSION, http_get=http_get, user_agent="fixture", cache_dir=tmp_path)
    assert len(calls) == 1
    assert second == first and first[1] is not None and first[0]


def test_an_index_page_without_an_accepted_time_is_not_cached(tmp_path):
    calls = []

    def http_get(url, **_kwargs):
        calls.append(url)
        return Response(text="<html></html>")

    sec.index(CIK, ACCESSION, http_get=http_get, user_agent="fixture", cache_dir=tmp_path)
    sec.index(CIK, ACCESSION, http_get=http_get, user_agent="fixture", cache_dir=tmp_path)
    assert len(calls) == 2  # retried next time: a missing time is not an immutable fact
