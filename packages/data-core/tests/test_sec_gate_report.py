"""M3-8: predeclared benchmark-accession SEC parse gate report (DATA-01).

Execution-lock §E: denominator = len(A) permanently; numerator requires
non-empty hash-bound successful primary-document extraction AND eligible_at from
authoritative accepted time or the approved conservative rule; the gate is the
exact integer form ``10 * numerator >= 9 * denominator``; section degradation is
separately reported and never folds into the gate.
"""
from __future__ import annotations

import sqlite3

import pytest

from catalyst_data.canonical.backfill import backfill_from_subtypes
from catalyst_data.sec.gate_report import SecParseReport, benchmark_sec_parse_report

ACC = "0000320193-26-000001"
ACC2 = "0000320193-26-000002"
ACC3 = "0000320193-26-000003"
ACC4 = "0000320193-26-000004"
ACC5 = "0000320193-26-000005"
ACC6 = "0000320193-26-000006"


def _seed_raw(conn, asset_id: str) -> None:
    conn.execute(
        """INSERT INTO raw_assets
           (asset_id, ticker, source_type, reference_date, fetched_at,
            data_version, content_raw, metadata_json)
           VALUES (?, 'AAPL', 'sec_filings', '2026-01-05', '2026-01-05T10:00:00Z',
                   'v1', ?, '{}')""",
        (asset_id, b"raw"),
    )


def _add_filing(
    conn,
    *,
    accession: str,
    eligible_at,
    temporal_precision: str,
    accepted_time_recovered: int,
    eligibility_fail_closed: int,
    extraction_status: str = "success",
    text: str = "",
    degraded: bool = False,
) -> str:
    filing_id = f"filing-{accession}"
    conn.execute(
        """INSERT INTO filings (
               filing_id, cik, ticker, form_type, filed_at, accession_number,
               primary_document, url, raw_asset_id, created_at,
               eligible_at, eligible_at_reason, temporal_precision,
               accepted_time_recovered, eligibility_fail_closed
           ) VALUES (?, '0000320193', 'AAPL', '8-K', '2026-01-05', ?, 'a.htm',
                     'https://example.com/a.htm', 'raw:sec', '2026-01-05T20:00:00Z',
                     ?, ?, ?, ?, ?)""",
        (
            filing_id,
            accession,
            eligible_at,
            "accepted_time_recovered"
            if accepted_time_recovered
            else "latest_plausible_approved"
            if temporal_precision == "date_only_latest_plausible"
            else "fail_closed_no_accepted_time",
            temporal_precision,
            accepted_time_recovered,
            eligibility_fail_closed,
        ),
    )
    if text or extraction_status != "success":
        conn.execute(
            """INSERT INTO filing_documents (
                   filing_id, document_url, document_type, text, char_len,
                   content_type, byte_size, extraction_status, extracted_at, document_id
               ) VALUES (?, ?, 'primary_doc', ?, ?, 'text/html', ?, ?, ?, ?)""",
            (
                filing_id,
                f"https://example.com/{accession}.htm",
                text,
                len(text),
                len(text),
                extraction_status,
                "2026-01-05T20:00:00Z",
                None if extraction_status != "success" else ("d" + "0" * 61 + accession[-2:]),
            ),
        )
    return filing_id


def _gate_conn() -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_raw(conn, "raw:sec")

    body = (
        "Item 1.01 Entry into a Material Definitive Agreement.\n"
        "On January 5, 2026, the registrant entered into a material definitive "
        "agreement. " + ("Substantive disclosure follows. " * 12)
    )
    # A1: accepted-time recovered + successful extraction -> numerator.
    _add_filing(
        conn, accession=ACC, eligible_at="2026-01-05T21:05:00Z",
        temporal_precision="accepted_time", accepted_time_recovered=1,
        eligibility_fail_closed=0, text=body,
    )
    # A2: successful extraction but fail-closed eligibility -> not numerator.
    _add_filing(
        conn, accession=ACC2, eligible_at=None,
        temporal_precision="unknown_time_of_day", accepted_time_recovered=0,
        eligibility_fail_closed=1, text=body,
    )
    # A3: date-only latest-plausible -> numerator only with approval.
    _add_filing(
        conn, accession=ACC3, eligible_at="2026-01-06T05:00:00Z",
        temporal_precision="date_only_latest_plausible", accepted_time_recovered=0,
        eligibility_fail_closed=0, text=body,
    )
    # A4: accession with no filing row -> denominator failure.
    # A5: extraction empty -> not numerator.
    _add_filing(
        conn, accession=ACC5, eligible_at="2026-01-05T21:05:00Z",
        temporal_precision="accepted_time", accepted_time_recovered=1,
        eligibility_fail_closed=0, extraction_status="empty", text="",
    )
    # A6: accepted + successful but degraded sections -> numerator still counts.
    _add_filing(
        conn, accession=ACC6, eligible_at="2026-01-05T21:05:00Z",
        temporal_precision="accepted_time", accepted_time_recovered=1,
        eligibility_fail_closed=0, text=body, degraded=True,
    )
    conn.commit()
    backfill_from_subtypes(conn)
    # Mark A6's canonical asset parse_quality degraded (reparse degradation).
    from catalyst_data.canonical.ids import asset_id as build_asset_id

    aid = build_asset_id(
        asset_type="FILING", source_table="filings", source_pk=f"filing-{ACC6}"
    )
    conn.execute(
        "UPDATE canonical_assets SET parse_quality='degraded' WHERE asset_id=?",
        (aid,),
    )
    conn.commit()
    return conn


def test_report_counts_numerator_and_denominator():
    conn = _gate_conn()
    report = benchmark_sec_parse_report(
        conn, [ACC, ACC2, ACC3, ACC4, ACC5, ACC6]
    )
    assert isinstance(report, SecParseReport)
    assert report.denominator == 6
    # A1 (accepted), A6 (accepted, degraded sections still count).
    assert report.numerator == 2
    # A1/A5/A6 recovered the accepted time; A5 still fails extraction and is
    # reported separately from the numerator.
    assert report.accepted_time_recovered_count == 3
    assert report.fail_closed_eligibility_count == 2  # A2 + A3 (without approval)
    assert report.section_degraded_count == 1  # A6
    conn.close()


def test_denominator_is_fixed_len_of_predeclared_set():
    conn = _gate_conn()
    report = benchmark_sec_parse_report(conn, [ACC, ACC2, ACC3])
    assert report.denominator == 3
    assert report.gate_passed is False  # 10*1 < 9*3
    # A re-run with a smaller set redefines the denominator only via a new
    # predeclared manifest; the report always reports len(A) as given.
    report2 = benchmark_sec_parse_report(conn, [ACC])
    assert report2.denominator == 1
    assert report2.gate_passed is True  # 10*1 >= 9*1
    conn.close()


def test_excluded_accessions_are_metadata_only_never_subtract():
    conn = _gate_conn()
    # The excluded accession is NOT a member of A (selection-stage metadata).
    excluded = "0000320193-26-099999"
    report = benchmark_sec_parse_report(
        conn, [ACC, ACC2, ACC3, ACC4, ACC5, ACC6],
        excluded_accessions=[excluded],
    )
    assert report.denominator == 6
    assert report.numerator == 2  # exclusions never subtract from either count
    assert excluded in report.excluded_accessions
    conn.close()


def test_selection_exclusions_never_members_of_a():
    conn = _gate_conn()
    with pytest.raises(ValueError, match="members of A"):
        benchmark_sec_parse_report(
            conn, [ACC, ACC2], excluded_accessions=[ACC2]
        )
    conn.close()


def test_fail_closed_counts_only_with_approval():
    conn = _gate_conn()
    without = benchmark_sec_parse_report(conn, [ACC, ACC2, ACC3])
    assert without.numerator == 1  # only A1
    with_approval = benchmark_sec_parse_report(
        conn, [ACC, ACC2, ACC3], approved_latest_plausible=True
    )
    assert with_approval.numerator == 2  # A1 + A3
    assert with_approval.gate_passed is False  # 10*2 < 9*3
    conn.close()


def test_section_degradation_does_not_change_numerator_or_denominator():
    conn = _gate_conn()
    base = benchmark_sec_parse_report(conn, [ACC, ACC6])
    assert base.denominator == 2
    assert base.numerator == 2
    assert base.section_degraded_count == 1
    conn.close()


def test_gate_passed_exact_integer_form():
    conn = _gate_conn()
    report = benchmark_sec_parse_report(conn, [ACC, ACC2, ACC3, ACC4, ACC5, ACC6])
    # 10 * 2 >= 9 * 6 is False.
    assert report.gate_passed is False
    assert (10 * report.numerator >= 9 * report.denominator) == report.gate_passed
    conn.close()
