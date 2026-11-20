import hashlib
import sqlite3
from datetime import date, timedelta

import pytest
from catalyst_data import read_api, trading_calendar
from catalyst_data.ingestion import gaps
from catalyst_data.ingestion.collectors import headlines

CORRECTIONS = (
    b"accession,cik,form,items,json_acceptance_utc,edgar_accepted_et,edgar_accepted_utc,"
    b"json_minus_edgar_h,in_stores\n"
    b"0000000001-26-000001,1,8-K,2.02,2026-10-30T20:00:00Z,2026-10-30 12:00:00,"
    b"2026-10-30T19:00:00Z,1,test-store\n"
    b"0000000001-26-000004,1,8-K,2.02,2026-10-28T01:05:00Z,2026-10-27 16:05:00,"
    b"2026-10-27T20:05:00Z,5,test-store\n"
    b"0000000001-26-000005,1,8-K,2.02,2026-11-01T20:00:00Z,2026-11-01 12:00:00,"
    b"2026-11-01T19:00:00Z,1,other-store\n"
)


def _add_coverage(db_path, ticker, requested_start, requested_end, *, status="OK",
                  covered_dates=None, include_window=True):
    with sqlite3.connect(db_path) as conn:
        ledger = gaps.GapLedger(conn)
        stop = (date.fromisoformat(requested_end) - timedelta(days=1)).isoformat()
        for session_date in trading_calendar.calendar_trading_days(requested_start, stop):
            if covered_dates is not None and session_date not in covered_dates:
                continue
            window_from, window_until = headlines.session_window(session_date)
            ledger.record(
                session_date=session_date, ticker=ticker, source_id="daily:sec",
                scope_kind="target", scope_id=ticker, status=status,
                reason="ERROR" if status == "ERROR" else None,
                requested_from=window_from, requested_until=window_until,
                coverage_from=window_from if include_window else None,
                coverage_until=window_until if include_window else None,
                pagination_complete=status in {"OK", "EMPTY"},
            )


def _add_gap_row(db_path, *, ticker, session_date, scope_id, status,
                 coverage_from, coverage_until, updated_at):
    with sqlite3.connect(db_path) as conn:
        ledger = gaps.GapLedger(conn)
        row = ledger.record(
            session_date=session_date, ticker=ticker, source_id="daily:sec",
            scope_kind="target", scope_id=scope_id, status=status,
            reason="ERROR" if status == "ERROR" else None,
            requested_from=coverage_from, requested_until=coverage_until,
            coverage_from=coverage_from if status in {"OK", "EMPTY"} else None,
            coverage_until=coverage_until if status in {"OK", "EMPTY"} else None,
            pagination_complete=status in {"OK", "EMPTY"},
        )
        conn.execute("UPDATE category_gaps SET updated_at=? WHERE id=?", (updated_at, row.id))


@pytest.fixture
def sec_db(tmp_path):
    path = tmp_path / "news_bodies_v2.sqlite3"
    with sqlite3.connect(path) as conn:
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
            """
        )
        rows = (
            ("v1", "0000000001-26-000001", "2026-10-30T20:00:00.000000+00:00",
             "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/aex991.htm",
             "AAA Form 8-K EX-99.1: 8-K"),
            ("v2", "0000000001-26-000002", "2026-10-30T21:00:00.000000+00:00",
             "https://www.sec.gov/Archives/edgar/data/1/000000000126000002/bex991.htm",
             "AAA Form 8-K EX-99.1: 8-K"),
            ("v3", "0000000001-26-000003", None,
             "https://www.sec.gov/Archives/edgar/data/1/000000000126000003/cex991.htm",
             "BBB Form 8-K EX-99.1: 8-K"),
            ("v4", "0000000001-26-000004", "2026-10-28T01:05:00.000000+00:00",
             "https://www.sec.gov/Archives/edgar/data/1/000000000126000004/dex991.htm",
             "AAA Form 8-K EX-99.1: 8-K"),
            ("v5", "0000000001-26-000005", "2026-11-01T20:00:00.000000+00:00",
             "https://www.sec.gov/Archives/edgar/data/1/000000000126000005/eex991.htm",
             "AAA Form 8-K EX-99.1: 8-K"),
            ("v6", "0000000001-26-000006", "2026-10-30T19:00:00.000000+00:00",
             "https://www.sec.gov/Archives/edgar/data/1/000000000126000006/fex991.htm",
             "BBB Form 8-K EX-99.1: 8-K"),
        )
        for version, accession, accepted_at, url, title in rows:
            conn.execute(
                "INSERT INTO body_versions VALUES (?, 'daily:sec', ?, ?, ?)",
                (version, accession, title, accepted_at),
            )
            conn.execute(
                "INSERT INTO temporal_evidence_versions VALUES (?, 'sec_index')", (version,)
            )
            ticker = "BBB" if version in {"v3", "v6"} else "AAA"
            conn.execute(
                "INSERT INTO temporal_evidence_map VALUES (?, ?, 'filing', ?, ?, ?)",
                (version, ticker, accepted_at, url, accession),
            )
    for ticker in ("AAA", "BBB"):
        _add_coverage(
            path, ticker, "2026-10-26", "2026-11-04",
        )
    correction_path = tmp_path / "sec_acceptance_v1.csv"
    correction_path.write_bytes(CORRECTIONS)
    return path, correction_path


def test_sec_acceptance_table_applies_scoped_correction(sec_db):
    db_path, correction_path = sec_db

    rows = read_api.sec_8k_acceptance_table(
        db_path, accessions=["0000000001-26-000001"], store_id="test-store",
        correction_table_path=correction_path,
    )

    assert rows == [{
        "accession": "0000000001-26-000001",
        "cik": "0000000001",
        "ticker": "AAA",
        "form": None,
        "filed_date": None,
        "acceptance_at": "2026-10-30T20:00:00Z",
        "corrected_at": "2026-10-30T19:00:00Z",
        "correction_ref": f"sec_acceptance_v1:{hashlib.sha256(CORRECTIONS).hexdigest()}#0000000001-26-000001",
        "exhibits": [
            "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/aex991.htm"
        ],
        "missing_fields": ["filed_date", "form"],
        "correction_status": "applied",
    }]


def test_sec_acceptance_table_marks_no_correction(sec_db):
    db_path, correction_path = sec_db

    rows = read_api.sec_8k_acceptance_table(
        db_path, accessions=["0000000001-26-000002"], store_id="test-store",
        correction_table_path=correction_path,
    )

    assert rows[0]["corrected_at"] == rows[0]["acceptance_at"] == "2026-10-30T21:00:00Z"
    assert rows[0]["correction_ref"] == "none"
    assert rows[0]["correction_status"] == "none"


def test_sec_acceptance_table_rejects_missing_acceptance_time(sec_db):
    db_path, correction_path = sec_db

    with pytest.raises(read_api.SecFilingDataError, match="acceptance time"):
        read_api.sec_8k_acceptance_table(
            db_path, accessions=["0000000001-26-000003"], store_id="test-store",
            correction_table_path=correction_path,
        )


def test_sec_acceptance_table_opens_database_read_only(sec_db, monkeypatch):
    db_path, correction_path = sec_db
    original_connect = sqlite3.connect
    opened = []

    def tracked_connect(database, *args, **kwargs):
        opened.append(str(database))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(read_api.sqlite3, "connect", tracked_connect)

    read_api.sec_8k_acceptance_table(
        db_path, accessions=["0000000001-26-000002"], store_id="test-store",
        correction_table_path=correction_path,
    )

    assert opened and "mode=ro" in opened[0]


def test_sec_acceptance_table_does_not_treat_empty_result_as_no_filings(sec_db):
    db_path, correction_path = sec_db

    with pytest.raises(read_api.SecFilingDataError, match="no SEC filing rows"):
        read_api.sec_8k_acceptance_table(
            db_path, accessions=["0000000001-26-000099"], store_id="test-store",
            correction_table_path=correction_path,
        )


def test_sec_acceptance_table_filters_tickers_by_inclusive_utc_dates(sec_db):
    db_path, correction_path = sec_db

    rows = read_api.sec_8k_acceptance_table(
        db_path, tickers=["aaa"], start="2026-10-30", end="2026-10-30",
        store_id="test-store", correction_table_path=correction_path,
    )

    assert [row["accession"] for row in rows] == [
        "0000000001-26-000001", "0000000001-26-000002",
    ]


def test_ticker_date_filter_uses_corrected_utc_day_across_midnight(sec_db):
    db_path, correction_path = sec_db

    rows = read_api.sec_8k_acceptance_table(
        db_path, tickers=["AAA"], start="2026-10-27", end="2026-10-27",
        store_id="test-store", correction_table_path=correction_path,
    )
    assert [row["accession"] for row in rows] == ["0000000001-26-000004"]
    assert rows[0]["acceptance_at"] == "2026-10-28T01:05:00Z"
    assert rows[0]["corrected_at"] == "2026-10-27T20:05:00Z"

    assert read_api.sec_8k_acceptance_table(
        db_path, tickers=["AAA"], start="2026-10-28", end="2026-10-28",
        store_id="test-store", correction_table_path=correction_path,
    ) == []


def test_correction_for_another_store_is_explicitly_not_applicable(sec_db):
    db_path, correction_path = sec_db

    row = read_api.sec_8k_acceptance_table(
        db_path, accessions=["0000000001-26-000005"], store_id="test-store",
        correction_table_path=correction_path,
    )[0]

    assert row["correction_status"] == "not_applicable_store"
    assert row["corrected_at"] is None
    assert row["correction_ref"] != ""
    assert row["correction_ref"].endswith("#0000000001-26-000005")
    assert "corrected_at" in row["missing_fields"]


def test_ticker_date_query_returns_unverified_corrected_time(sec_db):
    db_path, correction_path = sec_db

    result = read_api.sec_8k_acceptance_result(
        db_path, tickers=["AAA"], start="2026-11-01", end="2026-11-01",
        store_id="test-store", correction_table_path=correction_path,
    )

    assert [row["accession"] for row in result["rows"]] == ["0000000001-26-000005"]
    assert result["rows"][0]["corrected_at"] is None
    assert result["unverified_time"] == [{
        "accession": "0000000001-26-000005",
        "acceptance_at": "2026-11-01T20:00:00Z",
        "correction_ref": result["rows"][0]["correction_ref"],
    }]


def test_ticker_date_query_keeps_unverified_time_and_reports_it(sec_db):
    db_path, correction_path = sec_db
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE temporal_evidence_map SET eligible_at=? WHERE sec_accession=?",
            ("2026-10-31T21:00:00Z", "0000000001-26-000002"),
        )

    result = read_api.sec_8k_acceptance_result(
        db_path, tickers=["AAA"], start="2026-10-31", end="2026-10-31",
        store_id="test-store", correction_table_path=correction_path,
    )

    assert [row["accession"] for row in result["rows"]] == [
        "0000000001-26-000002", "0000000001-26-000005",
    ]
    unverified = result["rows"][1]
    assert unverified["correction_status"] == "not_applicable_store"
    assert unverified["corrected_at"] is None
    assert "corrected_at" in unverified["missing_fields"]
    assert result["unverified_time"] == [{
        "accession": "0000000001-26-000005",
        "acceptance_at": "2026-11-01T20:00:00Z",
        "correction_ref": unverified["correction_ref"],
    }]


def test_ticker_date_query_errors_on_missing_time_even_with_other_rows(sec_db):
    db_path, correction_path = sec_db

    with pytest.raises(read_api.SecFilingDataError, match="missing acceptance time"):
        read_api.sec_8k_acceptance_table(
            db_path, tickers=["BBB"], start="2026-10-30", end="2026-10-30",
            store_id="test-store", correction_table_path=correction_path,
        )


def test_covered_ticker_date_with_no_filings_returns_empty_with_coverage(sec_db):
    db_path, correction_path = sec_db

    result = read_api.sec_8k_acceptance_result(
        db_path, tickers=["AAA"], start="2026-10-29", end="2026-10-29",
        store_id="test-store", correction_table_path=correction_path,
    )

    assert result["rows"] == []
    assert result["uncovered"] == []
    coverage_row = next(
        row for row in result["coverage"]["AAA"] if row["session_date"] == "2026-10-29"
    )
    expected_window = headlines.session_window("2026-10-29")
    assert coverage_row["from"] == expected_window[0]
    assert coverage_row["until"] == expected_window[1]
    assert coverage_row["source"] == "category_gaps"
    assert coverage_row["policy"] is None
    assert read_api.sec_8k_acceptance_table(
        db_path, tickers=["AAA"], start="2026-10-29", end="2026-10-29",
        store_id="test-store", correction_table_path=correction_path,
    ) == []


def test_partially_covered_ticker_range_reports_missing_utc_day(sec_db):
    db_path, correction_path = sec_db
    _add_coverage(db_path, "CCC", "2026-11-04", "2026-11-06",
                  covered_dates={"2026-11-04"})

    with pytest.raises(read_api.SecCoverageError, match=r"CCC.*2026-11-04T21:00:00Z.*2026-11-06T00:00:00Z"):
        read_api.sec_8k_acceptance_result(
            db_path, tickers=["CCC"], start="2026-11-04", end="2026-11-05",
            store_id="test-store", correction_table_path=correction_path,
        )


def test_uncovered_ticker_range_reports_full_gap(sec_db):
    db_path, correction_path = sec_db

    with pytest.raises(read_api.SecCoverageError, match=r"CCC.*2026-11-04T00:00:00Z.*2026-11-06T00:00:00Z"):
        read_api.sec_8k_acceptance_table(
            db_path, tickers=["CCC"], start="2026-11-04", end="2026-11-05",
            store_id="test-store", correction_table_path=correction_path,
        )


@pytest.mark.parametrize("status,actual_bounds", [("ERROR", True), ("OK", False)])
def test_error_or_missing_window_does_not_cover(sec_db, status, actual_bounds):
    db_path, correction_path = sec_db
    _add_coverage(db_path, "CCC", "2026-11-04", "2026-11-05", status=status,
                  include_window=actual_bounds)

    with pytest.raises(read_api.SecCoverageError, match="CCC"):
        read_api.sec_8k_acceptance_table(
            db_path, tickers=["CCC"], start="2026-11-04", end="2026-11-04",
            store_id="test-store", correction_table_path=correction_path,
        )


def test_mixed_ticker_coverage_error_lists_only_uncovered_ticker(sec_db):
    db_path, correction_path = sec_db

    with pytest.raises(read_api.SecCoverageError) as exc_info:
        read_api.sec_8k_acceptance_result(
            db_path, tickers=["AAA", "CCC"], start="2026-10-29", end="2026-10-29",
            store_id="test-store", correction_table_path=correction_path,
        )
    assert "CCC" in str(exc_info.value)
    assert "AAA" not in str(exc_info.value)


def test_coverage_table_missing_is_an_explicit_error(sec_db):
    db_path, correction_path = sec_db
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE category_gaps")

    with pytest.raises(read_api.SecFilingDataError, match="coverage.*missing"):
        read_api.sec_8k_acceptance_result(
            db_path, tickers=["AAA"], start="2026-10-29", end="2026-10-29",
            store_id="test-store", correction_table_path=correction_path,
        )


def test_coverage_uses_daily_sec_session_windows_and_unions_intervals(sec_db):
    db_path, correction_path = sec_db
    _add_coverage(db_path, "CCC", "2026-11-04", "2026-11-07")

    result = read_api.sec_8k_acceptance_result(
        db_path, tickers=["CCC"], start="2026-11-04", end="2026-11-05",
        store_id="test-store", correction_table_path=correction_path,
    )
    assert result["rows"] == []
    assert len(result["coverage"]["CCC"]) == 3


def test_ok_category_gap_with_actual_window_counts_as_coverage(sec_db):
    db_path, correction_path = sec_db
    _add_gap_row(
        db_path, ticker="CCC", session_date="2026-11-04", scope_id="CCC",
        status="OK", coverage_from="2026-11-04T00:00:00Z",
        coverage_until="2026-11-05T00:00:00Z", updated_at="2026-11-05T00:00:00Z",
    )

    result = read_api.sec_8k_acceptance_result(
        db_path, tickers=["CCC"], start="2026-11-04", end="2026-11-04",
        store_id="test-store", correction_table_path=correction_path,
    )
    assert result["rows"] == []
    assert result["coverage"]["CCC"][0]["source"] == "category_gaps"


def test_same_ticker_session_uses_latest_gap_row(sec_db):
    db_path, correction_path = sec_db
    window_from, window_until = headlines.session_window("2026-11-04")
    _add_gap_row(
        db_path, ticker="CCC", session_date="2026-11-04", scope_id="CCC-old",
        status="OK", coverage_from=window_from,
        coverage_until=window_until, updated_at="2026-11-05T00:00:00Z",
    )
    _add_gap_row(
        db_path, ticker="CCC", session_date="2026-11-04", scope_id="CCC-new",
        status="ERROR", coverage_from=window_from,
        coverage_until=window_until, updated_at="2026-11-06T00:00:00Z",
    )

    with pytest.raises(read_api.SecCoverageError, match="CCC"):
        read_api.sec_8k_acceptance_result(
            db_path, tickers=["CCC"], start="2026-11-04", end="2026-11-04",
            store_id="test-store", correction_table_path=correction_path,
        )


def test_next_session_window_does_not_cover_prior_utc_day_prefix(sec_db):
    db_path, correction_path = sec_db
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM category_gaps WHERE ticker='AAA'")
    _add_gap_row(
        db_path, ticker="AAA", session_date="2026-10-28", scope_id="AAA",
        status="OK", coverage_from="2026-10-27T20:00:00Z",
        coverage_until="2026-10-28T20:00:00Z", updated_at="2026-10-29T00:00:00Z",
    )

    with pytest.raises(read_api.SecCoverageError, match=r"2026-10-27T00:00:00Z.*20:00:00Z"):
        read_api.sec_8k_acceptance_result(
            db_path, tickers=["AAA"], start="2026-10-27", end="2026-10-27",
            store_id="test-store", correction_table_path=correction_path,
        )


def test_prior_and_next_session_windows_cover_corrected_utc_day_filing(sec_db):
    db_path, correction_path = sec_db
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM category_gaps WHERE ticker='AAA'")
    _add_gap_row(
        db_path, ticker="AAA", session_date="2026-10-27", scope_id="AAA",
        status="OK", coverage_from="2026-10-26T20:00:00Z",
        coverage_until="2026-10-27T20:00:00Z", updated_at="2026-10-28T00:00:00Z",
    )
    _add_gap_row(
        db_path, ticker="AAA", session_date="2026-10-28", scope_id="AAA-next",
        status="OK", coverage_from="2026-10-27T20:00:00Z",
        coverage_until="2026-10-28T20:00:00Z", updated_at="2026-10-29T00:00:00Z",
    )

    result = read_api.sec_8k_acceptance_result(
        db_path, tickers=["AAA"], start="2026-10-27", end="2026-10-27",
        store_id="test-store", correction_table_path=correction_path,
    )
    assert [row["accession"] for row in result["rows"]] == ["0000000001-26-000004"]
    assert result["uncovered"] == []


def test_saturday_utc_range_is_covered_by_monday_session_window(sec_db):
    db_path, correction_path = sec_db
    _add_gap_row(
        db_path, ticker="CCC", session_date="2026-11-02", scope_id="CCC",
        status="OK", coverage_from="2026-10-30T20:00:00Z",
        coverage_until="2026-11-02T21:00:00Z", updated_at="2026-11-03T00:00:00Z",
    )

    result = read_api.sec_8k_acceptance_result(
        db_path, tickers=["CCC"], start="2026-10-31", end="2026-10-31",
        store_id="test-store", correction_table_path=correction_path,
    )
    assert result["rows"] == []
    assert result["uncovered"] == []


def test_uncovered_saturday_range_is_not_assumed_complete(sec_db):
    db_path, correction_path = sec_db

    with pytest.raises(read_api.SecCoverageError, match=r"2026-10-31T00:00:00Z.*2026-11-01T00:00:00Z"):
        read_api.sec_8k_acceptance_result(
            db_path, tickers=["CCC"], start="2026-10-31", end="2026-10-31",
            store_id="test-store", correction_table_path=correction_path,
        )


def test_early_close_coverage_uses_stored_window_boundaries(sec_db):
    db_path, correction_path = sec_db
    _add_gap_row(
        db_path, ticker="CCC", session_date="2026-11-27", scope_id="CCC",
        status="OK", coverage_from="2026-11-25T21:00:00Z",
        coverage_until="2026-11-27T18:00:00Z", updated_at="2026-11-28T00:00:00Z",
    )
    _add_gap_row(
        db_path, ticker="CCC", session_date="2026-11-30", scope_id="CCC-next",
        status="EMPTY", coverage_from="2026-11-27T18:00:00Z",
        coverage_until="2026-11-30T21:00:00Z", updated_at="2026-12-01T00:00:00Z",
    )

    result = read_api.sec_8k_acceptance_result(
        db_path, tickers=["CCC"], start="2026-11-27", end="2026-11-27",
        store_id="test-store", correction_table_path=correction_path,
    )
    assert result["rows"] == []
    assert result["uncovered"] == []


def test_coverage_window_can_prove_dates_outside_calendar_years(sec_db):
    db_path, correction_path = sec_db
    _add_gap_row(
        db_path, ticker="CCC", session_date="2030-05-06", scope_id="CCC",
        status="OK", coverage_from="2030-05-05T20:00:00Z",
        coverage_until="2030-05-06T20:00:00Z", updated_at="2030-05-07T00:00:00Z",
    )
    _add_gap_row(
        db_path, ticker="CCC", session_date="2030-05-07", scope_id="CCC-next",
        status="EMPTY", coverage_from="2030-05-06T20:00:00Z",
        coverage_until="2030-05-07T20:00:00Z", updated_at="2030-05-08T00:00:00Z",
    )

    result = read_api.sec_8k_acceptance_result(
        db_path, tickers=["CCC"], start="2030-05-06", end="2030-05-06",
        store_id="test-store", correction_table_path=correction_path,
    )
    assert result["rows"] == []
    assert result["uncovered"] == []


def test_gap_between_category_gap_windows_is_reported(sec_db):
    db_path, correction_path = sec_db
    _add_gap_row(
        db_path, ticker="CCC", session_date="2026-10-27", scope_id="CCC-a",
        status="OK", coverage_from="2026-10-27T00:00:00Z",
        coverage_until="2026-10-27T12:00:00Z", updated_at="2026-10-28T00:00:00Z",
    )
    _add_gap_row(
        db_path, ticker="CCC", session_date="2026-10-28", scope_id="CCC-b",
        status="OK", coverage_from="2026-10-27T13:00:00Z",
        coverage_until="2026-10-28T00:00:00Z", updated_at="2026-10-29T00:00:00Z",
    )

    with pytest.raises(read_api.SecCoverageError, match=r"2026-10-27T12:00:00Z.*13:00:00Z"):
        read_api.sec_8k_acceptance_result(
            db_path, tickers=["CCC"], start="2026-10-27", end="2026-10-27",
            store_id="test-store", correction_table_path=correction_path,
        )


def test_earnings_timing_returns_empty_when_sec_range_is_covered(sec_db):
    db_path, correction_path = sec_db

    assert read_api.earnings_release_timing(
        db_path, tickers=["AAA"], start="2026-10-29", end="2026-10-29",
        store_id="test-store", correction_table_path=correction_path,
        ticker_mapping_path=db_path.with_name("missing-mapping.json"),
        submissions_dir=db_path.parent / "missing-submissions",
    ) == []


def test_sec_acceptance_table_fails_explicitly_for_missing_projection_tables(tmp_path):
    db_path = tmp_path / "missing-projection.sqlite3"
    sqlite3.connect(db_path).close()
    correction_path = tmp_path / "sec_acceptance_v1.csv"
    correction_path.write_bytes(CORRECTIONS)

    with pytest.raises(read_api.SecFilingDataError, match="missing tables"):
        read_api.sec_8k_acceptance_table(
            db_path,
            accessions=["0000000001-26-000001"],
            store_id="test-store",
            correction_table_path=correction_path,
        )
