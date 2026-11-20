from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from catalyst_data import read_api, trading_calendar


def accession(number: int) -> str:
    return f"0000000001-26-{number:06d}"


ROWS = (
    (1, "0000000001", "2026-11-02T21:10:00Z"),
    (2, "0000000001", "2026-11-02T13:00:00Z"),
    (3, "0000000001", "2026-11-02T14:30:00Z"),
    (4, "0000000001", "2026-11-02T21:00:00Z"),
    (5, "0000000001", "2026-11-28T17:00:00Z"),
    (6, "0000000001", "2026-11-26T15:00:00Z"),
    (7, "0000000001", "2026-11-27T18:05:00Z"),
    (8, "0000000001", "2026-11-25T21:01:00Z"),
    (9, "0000000001", "2026-03-09T14:00:00Z"),
    (10, "0000000001", "2026-11-02T14:00:00Z"),
    (11, "0000000001", "2027-12-31T22:00:00Z"),
    (12, "0000000002", "2026-11-02T15:00:00Z"),
    (13, "0000000001", "2026-11-02T15:00:00Z"),
    (14, "0000000001", "2026-11-02T19:00:00Z"),
)

CORRECTIONS = (
    b"accession,cik,form,items,json_acceptance_utc,edgar_accepted_et,edgar_accepted_utc,"
    b"json_minus_edgar_h,in_stores\n"
    b"0000000001-26-000001,1,8-K,2.02,2026-11-02T21:10:00Z,2026-11-02 11:10:00,"
    b"2026-11-02T16:10:00Z,5,test-store\n"
    b"0000000001-26-000014,1,8-K,2.02,2026-11-02T19:00:00Z,2026-11-02 14:00:00,"
    b"2026-11-02T19:00:00Z,0,other-store\n"
)


@pytest.fixture
def timing_inputs(tmp_path):
    db_path = tmp_path / "news_bodies_v2.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE body_versions (
                version_id TEXT PRIMARY KEY, source_id TEXT, document_id TEXT,
                title TEXT, published_at TEXT
            );
            CREATE TABLE temporal_evidence_versions (
                version_id TEXT PRIMARY KEY, published_at_source TEXT
            );
            CREATE TABLE temporal_evidence_map (
                version_id TEXT, ticker TEXT, kind TEXT, eligible_at TEXT,
                url TEXT, sec_accession TEXT
            );
            CREATE TABLE sec_exhibit_units (
                url TEXT PRIMARY KEY, accession TEXT, units_json TEXT NOT NULL, parsed_at TEXT
            );
            """
        )
        for number, filing_cik, accepted_at in ROWS:
            acc = accession(number)
            digits = acc.replace("-", "")
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(filing_cik)}/{digits}/"
                   f"main{number}.htm")
            conn.execute(
                "INSERT INTO body_versions VALUES (?, 'daily:sec', ?, ?, ?)",
                (f"v{number}", acc, "AAA (AAA) Form 8-K EX-99.1: 8-K", accepted_at),
            )
            conn.execute(
                "INSERT INTO temporal_evidence_versions VALUES (?, 'sec_index')", (f"v{number}",)
            )
            conn.execute(
                "INSERT INTO temporal_evidence_map VALUES (?, 'AAA', 'filing', ?, ?, ?)",
                (f"v{number}", accepted_at, url, acc),
            )

    corrections_path = tmp_path / "sec_acceptance_v1.csv"
    corrections_path.write_bytes(CORRECTIONS)
    submissions_dir = tmp_path / "sec_submissions"
    submissions_dir.mkdir()
    ticker_mapping_path = tmp_path / "sec_company_tickers.json"
    ticker_mapping_path.write_text(json.dumps({
        "1": {"ticker": "AAA", "cik_str": 1},
        "2": {"ticker": "BBB", "cik_str": 2},
    }))
    return {
        "db_path": db_path,
        "correction_table_path": corrections_path,
        "submissions_dir": submissions_dir,
        "ticker_mapping_path": ticker_mapping_path,
    }


def _read(inputs, number: int, **kwargs):
    return read_api.earnings_release_timing(
        inputs["db_path"], accessions=[accession(number)], store_id="test-store",
        correction_table_path=inputs["correction_table_path"],
        submissions_dir=inputs["submissions_dir"],
        ticker_mapping_path=inputs["ticker_mapping_path"], **kwargs,
    )[0]


def _write_submissions(directory, cik: str, items_by_number: dict[int, str], *, archive=False):
    path = directory / (
        f"CIK{cik}-submissions-001.json" if archive else f"{cik}.json"
    )
    path.write_text(json.dumps({"filings": {"recent": {
        "accessionNumber": [accession(number) for number in items_by_number],
        "items": list(items_by_number.values()),
    }}}))


def _write_main_unit(inputs, number: int, text: str, *, unit_kind="paragraph"):
    acc = accession(number)
    with sqlite3.connect(inputs["db_path"]) as conn:
        conn.execute("UPDATE body_versions SET title=? WHERE document_id=?",
                     ("AAA (AAA) Form 8-K 8-K: Items", acc))
        url = conn.execute(
            "SELECT url FROM temporal_evidence_map WHERE sec_accession=?", (acc,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO sec_exhibit_units VALUES (?, ?, ?, '2026-11-05T00:00:00Z')",
            (url, acc, json.dumps({"title": None, "units": [{"kind": unit_kind, "text": text}]})),
        )


@pytest.mark.parametrize(
    ("number", "session", "local_time", "attributed"),
    [
        (2, "pre_market", "2026-11-02T08:00:00-05:00", "2026-11-02"),
        (3, "intraday", "2026-11-02T09:30:00-05:00", "2026-11-02"),
        (4, "after_close", "2026-11-02T16:00:00-05:00", "2026-11-03"),
        (5, "non_trading_day", "2026-11-28T12:00:00-05:00", "2026-11-30"),
        (6, "non_trading_day", "2026-11-26T10:00:00-05:00", "2026-11-27"),
        (7, "after_close", "2026-11-27T13:05:00-05:00", "2026-11-30"),
        (8, "after_close", "2026-11-25T16:01:00-05:00", "2026-11-27"),
        (9, "intraday", "2026-03-09T10:00:00-04:00", "2026-03-09"),
        (10, "pre_market", "2026-11-02T09:00:00-05:00", "2026-11-02"),
    ],
)
def test_release_time_classification_uses_corrected_utc_and_market_calendar(
    timing_inputs, number, session, local_time, attributed,
):
    row = _read(timing_inputs, number)

    assert row["release_session"] == session
    assert row["release_local_time"] == local_time
    assert row["attributed_session_date"] == attributed


def test_correction_can_move_acceptance_across_close(timing_inputs):
    row = _read(timing_inputs, 1)

    assert row["acceptance_at"] == "2026-11-02T21:10:00Z"
    assert row["corrected_at"] == "2026-11-02T16:10:00Z"
    assert row["correction_status"] == "applied"
    assert row["release_session"] == "intraday"
    assert row["release_local_time"] == "2026-11-02T11:10:00-05:00"
    assert row["attributed_session_date"] == "2026-11-02"


def test_not_applicable_store_does_not_guess_release_or_attributed_session(timing_inputs):
    row = _read(timing_inputs, 14)

    assert row["correction_status"] == "not_applicable_store"
    assert row["corrected_at"] is None
    assert row["release_session"] is None
    assert row["release_local_time"] is None
    assert row["attributed_session_date"] is None
    assert {"corrected_at", "release_session", "release_local_time", "attributed_session_date"} <= set(
        row["missing_fields"]
    )


def test_same_utc_time_observes_edt_and_est(timing_inputs):
    edt = _read(timing_inputs, 9)
    est = _read(timing_inputs, 10)

    assert edt["corrected_at"][-1] == est["corrected_at"][-1] == "Z"
    assert edt["release_session"] == "intraday"
    assert est["release_session"] == "pre_market"


def test_early_close_boundary_is_after_close(timing_inputs):
    with sqlite3.connect(timing_inputs["db_path"]) as conn:
        conn.execute("UPDATE body_versions SET published_at=? WHERE document_id=?",
                     ("2026-11-27T18:00:00Z", accession(7)))
        conn.execute("UPDATE temporal_evidence_map SET eligible_at=? WHERE sec_accession=?",
                     ("2026-11-27T18:00:00Z", accession(7)))

    row = _read(timing_inputs, 7)

    assert row["release_local_time"] == "2026-11-27T13:00:00-05:00"
    assert row["release_session"] == "after_close"


def test_item_202_uses_local_submissions_before_exhibit_units(timing_inputs):
    _write_submissions(timing_inputs["submissions_dir"], "0000000001", {2: "1.01, 2.02"})

    row = _read(timing_inputs, 2)

    assert row["issuer_cik_match"] is True
    assert row["is_issuer_earnings"] is True
    assert row["earnings_basis"] == "submissions_items"


def test_submissions_root_archive_names_are_read_and_indexpage_is_ignored(timing_inputs):
    directory = timing_inputs["submissions_dir"]
    _write_submissions(directory, "0000000001", {2: "2.02"})
    _write_submissions(directory, "0000000001", {13: "2.02"}, archive=True)
    (directory / f"CIK0000000001-{accession(13)}-indexpage.json").write_text("not json")

    current = _read(timing_inputs, 2)
    archived = _read(timing_inputs, 13)

    assert current["is_issuer_earnings"] is True
    assert current["earnings_basis"] == "submissions_items"
    assert archived["is_issuer_earnings"] is True
    assert archived["earnings_basis"] == "submissions_items"


def test_mismatched_filing_cik_is_not_issuer_earnings(timing_inputs):
    _write_submissions(timing_inputs["submissions_dir"], "0000000002", {12: "2.02"})

    row = _read(timing_inputs, 12)

    assert row["issuer_cik_match"] is False
    assert row["is_issuer_earnings"] is False
    assert row["earnings_basis"] == "submissions_items"


def test_main_document_units_are_the_fallback_for_missing_submissions(timing_inputs):
    _write_main_unit(timing_inputs, 13, "Item 2.02 Results of Operations and Financial Condition")

    row = _read(timing_inputs, 13)

    assert row["issuer_cik_match"] is True
    assert row["is_issuer_earnings"] is True
    assert row["earnings_basis"] == "exhibit_units_text"


@pytest.mark.parametrize("reference", [
    "Previously furnished under Item 2.02 of the Form 8-K filed on October 1.",
    "Pursuant to Item 2.02, the company incorporates the prior release by reference.",
    "This information was incorporated by reference from Item 2.02 of the Form 8-K.",
])
def test_reference_to_item_202_is_not_a_filing_item_heading(timing_inputs, reference):
    _write_main_unit(timing_inputs, 13, reference)

    row = _read(timing_inputs, 13)

    assert row["is_issuer_earnings"] is None
    assert row["earnings_basis"] == "unknown"


@pytest.mark.parametrize("heading", [
    "ITEM 2.02 — Results of Operations and Financial Condition",
    "Item 2.02: Results of Operations and Financial Condition",
    "Item 2.02. Results of Operations and Financial Condition",
    "iTeM   2.02 -   results\t of Operations and Financial Condition",
])
def test_item_202_heading_requires_results_of_operations_heading_text(timing_inputs, heading):
    _write_main_unit(
        timing_inputs, 13, heading, unit_kind="title",
    )

    row = _read(timing_inputs, 13)

    assert row["is_issuer_earnings"] is True
    assert row["earnings_basis"] == "exhibit_units_text"


def test_unexpected_item_202_heading_remains_unknown(timing_inputs):
    _write_main_unit(timing_inputs, 13, "Item 2.02 — Other Information")

    row = _read(timing_inputs, 13)

    assert row["is_issuer_earnings"] is None


def test_other_recognized_item_heading_without_202_is_not_earnings(timing_inputs):
    _write_main_unit(timing_inputs, 13, "Item 7.01 — Regulation FD Disclosure")

    row = _read(timing_inputs, 13)

    assert row["is_issuer_earnings"] is False
    assert row["earnings_basis"] == "exhibit_units_text"


def test_main_document_without_recognized_item_heading_is_unknown(timing_inputs):
    _write_main_unit(timing_inputs, 13, "The company announced quarterly financial results today.")

    row = _read(timing_inputs, 13)

    assert row["is_issuer_earnings"] is None
    assert row["earnings_basis"] == "unknown"


def test_exhibit_units_do_not_substitute_for_missing_main_document_units(timing_inputs):
    acc = accession(13)
    with sqlite3.connect(timing_inputs["db_path"]) as conn:
        url = conn.execute(
            "SELECT url FROM temporal_evidence_map WHERE sec_accession=?", (acc,)
        ).fetchone()[0]
        conn.execute("INSERT INTO sec_exhibit_units VALUES (?, ?, ?, '2026-11-05T00:00:00Z')",
                     (url, acc, json.dumps({"units": [{"kind": "paragraph", "text": "Item 2.02"}]})))

    row = _read(timing_inputs, 13)

    assert row["issuer_cik_match"] is True
    assert row["is_issuer_earnings"] is None
    assert row["earnings_basis"] == "unknown"
    assert "is_issuer_earnings" in row["missing_fields"]


def test_main_document_item_check_errors_when_units_table_is_missing(timing_inputs):
    with sqlite3.connect(timing_inputs["db_path"]) as conn:
        conn.execute("DROP TABLE sec_exhibit_units")

    with pytest.raises(read_api.SecFilingDataError, match="missing sec_exhibit_units table"):
        _read(timing_inputs, 13)


def test_earnings_is_unknown_when_submissions_and_main_units_are_absent(timing_inputs):
    row = _read(timing_inputs, 13)

    assert row["issuer_cik_match"] is True
    assert row["is_issuer_earnings"] is None
    assert row["earnings_basis"] == "unknown"


def test_submissions_negative_result_takes_precedence_over_body_units(timing_inputs):
    _write_submissions(timing_inputs["submissions_dir"], "0000000001", {13: "1.01"})
    _write_main_unit(timing_inputs, 13, "Item 2.02 Results of Operations and Financial Condition")

    row = _read(timing_inputs, 13)

    assert row["is_issuer_earnings"] is False
    assert row["earnings_basis"] == "submissions_items"


def test_calendar_coverage_fails_closed_outside_supported_years(timing_inputs):
    with pytest.raises(trading_calendar.CalendarCoverageError):
        _read(timing_inputs, 11)


def test_all_sqlite_reads_are_read_only_and_leave_database_unchanged(timing_inputs, monkeypatch):
    db_path = timing_inputs["db_path"]
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    original_connect = sqlite3.connect
    opened = []

    def tracked_connect(database, *args, **kwargs):
        opened.append(str(database))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(read_api.sqlite3, "connect", tracked_connect)
    _read(timing_inputs, 2, immutable=True)

    assert len(opened) >= 2
    assert all("mode=ro" in uri and "immutable=1" in uri for uri in opened)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_ticker_date_query_without_coverage_ledger_is_explicit_error(timing_inputs):
    with pytest.raises(read_api.SecFilingDataError, match="category_gaps"):
        read_api.earnings_release_timing(
            timing_inputs["db_path"], tickers=["ZZZ"], start="2026-11-02", end="2026-11-02",
            correction_table_path=timing_inputs["correction_table_path"],
            submissions_dir=timing_inputs["submissions_dir"],
            ticker_mapping_path=timing_inputs["ticker_mapping_path"],
        )

def test_earnings_timing_uses_the_configured_submissions_directory_by_default(
    timing_inputs, monkeypatch, tmp_path,
):
    from catalyst_data.ingestion.collectors import sec, sec_filings

    submissions_dir = tmp_path / "configured-submissions"
    submissions_dir.mkdir()
    observed = []
    monkeypatch.setattr(sec, "_tickers_path", lambda: timing_inputs["ticker_mapping_path"])
    monkeypatch.setattr(sec_filings, "_dir", lambda: submissions_dir)
    monkeypatch.setattr(
        read_api, "_submission_items_for_accession",
        lambda path, _cik, _accession: observed.append(path) or None,
    )
    monkeypatch.setattr(read_api, "_main_document_item_202", lambda *_args, **_kwargs: None)

    read_api.earnings_release_timing(
        timing_inputs["db_path"], accessions=[accession(1)],
        correction_table_path=timing_inputs["correction_table_path"],
    )

    assert observed == [submissions_dir]
