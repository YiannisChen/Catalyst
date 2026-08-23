"""M3-8: predeclared benchmark-accession SEC parse gate report (DATA-01).

Execution-lock §E: denominator = len(A) permanently; numerator requires
non-empty hash-bound successful primary-document extraction AND eligible_at from
authoritative accepted time or the approved conservative rule; the gate is the
exact integer form ``10 * numerator >= 9 * denominator``; section degradation is
separately reported and never folds into the gate.
"""
from __future__ import annotations

import json as _json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.canonical.backfill import backfill_from_subtypes
from catalyst_data.canonical.ids import sha256_identity
import catalyst_data.sec.gate_report as gate_report_module
from catalyst_data.sec.gate_report import (
    SecParseReport,
    benchmark_sec_parse_report,
    benchmark_sec_parse_report_from_operator_inputs,
)

ACC = "0000320193-26-000001"
ACC2 = "0000320193-26-000002"
ACC3 = "0000320193-26-000003"
ACC4 = "0000320193-26-000004"
ACC5 = "0000320193-26-000005"
ACC6 = "0000320193-26-000006"

_BODY = (
    "Item 1.01 Entry into a Material Definitive Agreement.\n"
    "On January 5, 2026, the registrant entered into a material definitive "
    "agreement. " + ("Substantive disclosure follows. " * 12)
)


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
    document_type: str = "primary_doc",
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
               ) VALUES (?, ?, ?, ?, ?, 'text/html', ?, ?, ?, ?)""",
            (
                filing_id,
                f"https://example.com/{accession}.htm",
                document_type,
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

    body = _BODY
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


# ---------------------------------------------------------------------------
# Corrective Batch C — DATA-01 fail-closed boundary (C1–C7)
# ---------------------------------------------------------------------------


def _bound_version(conn, accession: str):
    """Return the canonical content version bound to the primary document."""
    filing_id = f"filing-{accession}"
    doc = conn.execute(
        "SELECT document_id FROM filing_documents "
        "WHERE filing_id=? AND document_type='primary_doc'",
        (filing_id,),
    ).fetchone()
    if doc is None:
        return None
    return conn.execute(
        "SELECT s.subtype_pk_value, v.* FROM canonical_subtype_assoc s "
        "JOIN canonical_content_versions v "
        "ON v.canonical_content_version_id = s.canonical_content_version_id "
        "WHERE s.subtype_table='filing_documents' AND s.subtype_pk='document_id' "
        "AND s.subtype_pk_value=?",
        (doc["document_id"],),
    ).fetchone()


def _operator_record_dicts(
    *,
    decision: str,
    accessions: list[str],
    expected_git_revision: str,
    selection_authority: str = "Frozen §10 + Final TSD §5.3",
    raw_payload_inventory=None,
):
    """Return (benchmark, q005) operator record dicts (never baseline files)."""
    case_hash = sha256_identity(
        {
            "schema_version": "benchmark_accessions_v1",
            "selection_authority": selection_authority,
            "ordered_unique_accession_ids": list(accessions),
        }
    )
    bench = {
        "schema_version": "benchmark_accessions_v1",
        "selection_authority": selection_authority,
        "frozen_at": "2026-08-22T00:00:00Z",
        "git_revision": expected_git_revision,
        "ordered_unique_accession_ids": list(accessions),
        "excluded_accessions": [],
        "selection_exclusions": [],
        "case_list_sha256": case_hash,
        "denominator": len(accessions),
    }
    q005 = {
        "schema_version": "q005_sec_time_approval_v1",
        "benchmark_case_list_sha256": case_hash,
        "selection_authority": selection_authority,
        "decision": decision,
        "raw_payload_inventory": (
            raw_payload_inventory
            if raw_payload_inventory is not None
            else [
                {"accession": a, "acceptance_datetime_retained": True}
                for a in accessions
            ]
        ),
        "frozen_at": "2026-08-22T00:00:00Z",
        "git_revision": expected_git_revision,
    }
    return bench, q005


def _write_operator_records(
    tmp_path,
    *,
    decision: str,
    accessions: list[str],
    expected_git_revision: str,
    selection_authority: str = "Frozen §10 + Final TSD §5.3",
):
    """Write operator Q-005/benchmark records under tmp_path (never baseline)."""
    bench, q005 = _operator_record_dicts(
        decision=decision,
        accessions=accessions,
        expected_git_revision=expected_git_revision,
        selection_authority=selection_authority,
    )
    bench_path = tmp_path / "benchmark_accessions_v1.json"
    q005_path = tmp_path / "q005_sec_time_approval_v1.json"
    bench_path.write_text(_json.dumps(bench, sort_keys=True))
    q005_path.write_text(_json.dumps(q005, sort_keys=True))
    return bench_path, q005_path


def _write_operator_dicts(tmp_path, bench, q005, *, prefix="hostile"):
    """Write mutated operator records under tmp_path (never baseline)."""
    bench_path = tmp_path / f"{prefix}-benchmark.json"
    q005_path = tmp_path / f"{prefix}-q005.json"
    bench_path.write_text(_json.dumps(bench, sort_keys=True))
    q005_path.write_text(_json.dumps(q005, sort_keys=True))
    return bench_path, q005_path


def test_empty_accession_set_fails_closed():
    conn = _gate_conn()
    with pytest.raises(ValueError, match="empty"):
        benchmark_sec_parse_report(conn, [])
    conn.close()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "0000320193-26-000001 ",
        " 0000320193-26-000001",
        "000032019326000001",
        "0000320193-26-00000X",
        "0000320193-26-000001-extra",
        "not-an-accession",
        None,
        123,
    ],
)
def test_malformed_or_non_str_accession_fails_closed(bad):
    conn = _gate_conn()
    with pytest.raises(ValueError, match="accession"):
        benchmark_sec_parse_report(conn, [ACC, bad])
    conn.close()


def test_duplicate_accessions_fail_closed():
    conn = _gate_conn()
    with pytest.raises(ValueError, match="unique"):
        benchmark_sec_parse_report(conn, [ACC, ACC])
    conn.close()


def test_duplicate_filing_rows_for_one_accession_fail_closed():
    conn = _gate_conn()
    conn.execute(
        """INSERT INTO filings (
               filing_id, cik, ticker, form_type, filed_at, accession_number,
               primary_document, url, raw_asset_id, created_at,
               eligible_at, eligible_at_reason, temporal_precision,
               accepted_time_recovered, eligibility_fail_closed
           ) VALUES (?, '0000320193', 'AAPL', '8-K', '2026-01-05', ?,
                     'b.htm', 'https://example.com/b.htm', 'raw:sec',
                     '2026-01-05T20:00:00Z',
                     '2026-01-05T21:05:00Z', 'accepted_time_recovered',
                     'accepted_time', 1, 0)""",
        ("filing-dup-1", ACC),
    )
    conn.commit()
    with pytest.raises(ValueError, match="more than one filing"):
        benchmark_sec_parse_report(conn, [ACC])
    conn.close()


def test_arbitrary_64_hex_content_hash_not_numerator():
    conn = _gate_conn()
    version = _bound_version(conn, ACC)
    assert version is not None
    conn.execute(
        "UPDATE canonical_content_versions SET content_hash=? "
        "WHERE canonical_content_version_id=?",
        ("f" * 64, version["canonical_content_version_id"]),
    )
    conn.commit()
    report = benchmark_sec_parse_report(conn, [ACC])
    assert report.numerator == 0
    conn.close()


def test_foreign_asset_content_version_binding_not_numerator():
    conn = _gate_conn()
    acc_version = _bound_version(conn, ACC)
    acc2_version = _bound_version(conn, ACC2)
    assert acc_version is not None and acc2_version is not None
    assert acc_version["asset_id"] != acc2_version["asset_id"]
    conn.execute(
        "UPDATE canonical_subtype_assoc SET asset_id=?, canonical_content_version_id=? "
        "WHERE subtype_table='filing_documents' AND subtype_pk='document_id' "
        "AND subtype_pk_value=?",
        (
            acc2_version["asset_id"],
            acc2_version["canonical_content_version_id"],
            acc_version["subtype_pk_value"],
        ),
    )
    conn.commit()
    report = benchmark_sec_parse_report(conn, [ACC])
    assert report.numerator == 0
    conn.close()


def test_unbound_primary_document_id_not_numerator():
    conn = _gate_conn()
    doc = conn.execute(
        "SELECT document_id FROM filing_documents "
        "WHERE filing_id=? AND document_type='primary_doc'",
        (f"filing-{ACC}",),
    ).fetchone()
    conn.execute(
        "DELETE FROM canonical_subtype_assoc "
        "WHERE subtype_table='filing_documents' AND subtype_pk='document_id' "
        "AND subtype_pk_value=?",
        (doc["document_id"],),
    )
    conn.commit()
    report = benchmark_sec_parse_report(conn, [ACC])
    assert report.numerator == 0
    conn.close()


def test_normalizer_version_mismatch_not_numerator():
    conn = _gate_conn()
    version = _bound_version(conn, ACC)
    assert version is not None
    conn.execute(
        "UPDATE canonical_content_versions SET normalizer_version=? "
        "WHERE canonical_content_version_id=?",
        ("some_other_v2", version["canonical_content_version_id"]),
    )
    conn.commit()
    report = benchmark_sec_parse_report(conn, [ACC])
    assert report.numerator == 0
    conn.close()


def test_materiality_version_mismatch_not_numerator():
    conn = _gate_conn()
    version = _bound_version(conn, ACC)
    assert version is not None
    conn.execute(
        "UPDATE canonical_content_versions SET materiality_version=? "
        "WHERE canonical_content_version_id=?",
        ("materiality_v9", version["canonical_content_version_id"]),
    )
    conn.commit()
    report = benchmark_sec_parse_report(conn, [ACC])
    assert report.numerator == 0
    conn.close()


def test_stored_text_hash_mismatch_not_numerator():
    conn = _gate_conn()
    conn.execute(
        "UPDATE filing_documents SET text=? "
        "WHERE filing_id=? AND document_type='primary_doc'",
        (_BODY + " changed after binding", f"filing-{ACC}"),
    )
    conn.commit()
    report = benchmark_sec_parse_report(conn, [ACC])
    assert report.numerator == 0
    conn.close()


def test_production_path_fail_closed_only_never_counts_fail_closed_rows(tmp_path):
    conn = _gate_conn()
    bench_path, q005_path = _write_operator_records(
        tmp_path,
        decision="fail_closed_only",
        accessions=[ACC, ACC2, ACC3],
        expected_git_revision="abc123",
    )
    report = benchmark_sec_parse_report_from_operator_inputs(
        conn, bench_path, q005_path, expected_git_revision="abc123"
    )
    assert report.numerator == 1  # A1 accepted; A2/A3 fail-closed never count
    conn.close()


def test_production_path_approve_latest_plausible_instant_counts_approved_row(tmp_path):
    conn = _gate_conn()
    bench_path, q005_path = _write_operator_records(
        tmp_path,
        decision="approve_latest_plausible_instant",
        accessions=[ACC, ACC2, ACC3],
        expected_git_revision="abc123",
    )
    report = benchmark_sec_parse_report_from_operator_inputs(
        conn, bench_path, q005_path, expected_git_revision="abc123"
    )
    assert report.numerator == 2  # A1 + A3
    conn.close()


def test_production_path_rejects_bad_decision(tmp_path):
    conn = _gate_conn()
    bench_path, q005_path = _write_operator_records(
        tmp_path,
        decision="approve_everything",
        accessions=[ACC],
        expected_git_revision="abc123",
    )
    with pytest.raises(ValueError, match="decision"):
        benchmark_sec_parse_report_from_operator_inputs(
            conn, bench_path, q005_path, expected_git_revision="abc123"
        )
    conn.close()


def test_production_path_rejects_git_revision_mismatch(tmp_path):
    conn = _gate_conn()
    bench_path, q005_path = _write_operator_records(
        tmp_path,
        decision="fail_closed_only",
        accessions=[ACC],
        expected_git_revision="abc123",
    )
    with pytest.raises(ValueError, match="git_revision"):
        benchmark_sec_parse_report_from_operator_inputs(
            conn, bench_path, q005_path, expected_git_revision="def456"
        )
    conn.close()


# ---------------------------------------------------------------------------
# Corrective Batch C C8 — production boundary must authenticate via validator
# ---------------------------------------------------------------------------

def _spy_report_builder(monkeypatch):
    """Replace the report builder with a spy that fails loudly if invoked."""
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError(
            "benchmark_sec_parse_report must not be invoked on hostile input"
        )

    monkeypatch.setattr(gate_report_module, "benchmark_sec_parse_report", spy)
    return calls


def test_production_path_forged_case_hash_fails_before_report_builder(
    tmp_path, monkeypatch
):
    conn = _gate_conn()
    bench, q005 = _operator_record_dicts(
        decision="fail_closed_only",
        accessions=[ACC],
        expected_git_revision="abc123",
    )
    bench["case_list_sha256"] = "f" * 64
    q005["benchmark_case_list_sha256"] = "f" * 64
    bench_path, q005_path = _write_operator_dicts(
        tmp_path, bench, q005, prefix="forged-hash"
    )
    calls = _spy_report_builder(monkeypatch)
    with pytest.raises(ValueError, match="case_list_sha256"):
        benchmark_sec_parse_report_from_operator_inputs(
            conn, bench_path, q005_path, expected_git_revision="abc123"
        )
    assert not calls
    conn.close()


def test_production_path_denominator_mismatch_fails_before_report_builder(
    tmp_path, monkeypatch
):
    conn = _gate_conn()
    bench, q005 = _operator_record_dicts(
        decision="fail_closed_only",
        accessions=[ACC, ACC2],
        expected_git_revision="abc123",
    )
    bench["denominator"] = 99
    bench_path, q005_path = _write_operator_dicts(
        tmp_path, bench, q005, prefix="denom"
    )
    calls = _spy_report_builder(monkeypatch)
    with pytest.raises(ValueError, match="denominator"):
        benchmark_sec_parse_report_from_operator_inputs(
            conn, bench_path, q005_path, expected_git_revision="abc123"
        )
    assert not calls
    conn.close()


@pytest.mark.parametrize("blank", ["", "   "])
def test_production_path_empty_selection_authority_fails_before_report_builder(
    tmp_path, monkeypatch, blank
):
    conn = _gate_conn()
    bench, q005 = _operator_record_dicts(
        decision="fail_closed_only",
        accessions=[ACC],
        expected_git_revision="abc123",
        selection_authority=blank,
    )
    bench_path, q005_path = _write_operator_dicts(
        tmp_path, bench, q005, prefix="authority"
    )
    calls = _spy_report_builder(monkeypatch)
    with pytest.raises(ValueError, match="selection_authority"):
        benchmark_sec_parse_report_from_operator_inputs(
            conn, bench_path, q005_path, expected_git_revision="abc123"
        )
    assert not calls
    conn.close()


def test_production_path_incomplete_inventory_fails_before_report_builder(
    tmp_path, monkeypatch
):
    conn = _gate_conn()
    bench, q005 = _operator_record_dicts(
        decision="fail_closed_only",
        accessions=[ACC, ACC2],
        expected_git_revision="abc123",
        raw_payload_inventory=[
            {"accession": ACC, "acceptance_datetime_retained": True},
        ],
    )
    bench_path, q005_path = _write_operator_dicts(
        tmp_path, bench, q005, prefix="incomplete-inventory"
    )
    calls = _spy_report_builder(monkeypatch)
    with pytest.raises(ValueError, match="raw_payload_inventory"):
        benchmark_sec_parse_report_from_operator_inputs(
            conn, bench_path, q005_path, expected_git_revision="abc123"
        )
    assert not calls
    conn.close()


@pytest.mark.parametrize(
    "inventory",
    [
        # acceptance_datetime_retained key missing
        [
            {"accession": ACC},
            {"accession": ACC2, "acceptance_datetime_retained": True},
        ],
        # explicit null
        [
            {"accession": ACC, "acceptance_datetime_retained": None},
            {"accession": ACC2, "acceptance_datetime_retained": True},
        ],
        # string literal
        [
            {"accession": ACC, "acceptance_datetime_retained": "true"},
            {"accession": ACC2, "acceptance_datetime_retained": True},
        ],
        # Python int 1 (type(1) is bool is False)
        [
            {"accession": ACC, "acceptance_datetime_retained": 1},
            {"accession": ACC2, "acceptance_datetime_retained": True},
        ],
        # Python int 0 (type(0) is bool is False)
        [
            {"accession": ACC, "acceptance_datetime_retained": 0},
            {"accession": ACC2, "acceptance_datetime_retained": True},
        ],
        # "false" string
        [
            {"accession": ACC, "acceptance_datetime_retained": "false"},
            {"accession": ACC2, "acceptance_datetime_retained": True},
        ],
    ],
)
def test_production_path_invalid_inventory_boolean_fails_before_report_builder(
    tmp_path, monkeypatch, inventory
):
    conn = _gate_conn()
    bench, q005 = _operator_record_dicts(
        decision="fail_closed_only",
        accessions=[ACC, ACC2],
        expected_git_revision="abc123",
        raw_payload_inventory=inventory,
    )
    bench_path, q005_path = _write_operator_dicts(
        tmp_path, bench, q005, prefix="inventory-bool"
    )
    calls = _spy_report_builder(monkeypatch)
    with pytest.raises(ValueError, match="acceptance_datetime_retained"):
        benchmark_sec_parse_report_from_operator_inputs(
            conn, bench_path, q005_path, expected_git_revision="abc123"
        )
    assert not calls
    conn.close()


def test_production_path_wrong_schema_version_fails_before_report_builder(
    tmp_path, monkeypatch
):
    conn = _gate_conn()
    bench, q005 = _operator_record_dicts(
        decision="fail_closed_only",
        accessions=[ACC],
        expected_git_revision="abc123",
    )
    bench["schema_version"] = "benchmark_accessions_v2"
    bench_path, q005_path = _write_operator_dicts(
        tmp_path, bench, q005, prefix="schema"
    )
    calls = _spy_report_builder(monkeypatch)
    with pytest.raises(ValueError, match="schema_version"):
        benchmark_sec_parse_report_from_operator_inputs(
            conn, bench_path, q005_path, expected_git_revision="abc123"
        )
    assert not calls
    conn.close()


def test_production_path_git_revision_mismatch_fails_before_report_builder(
    tmp_path, monkeypatch
):
    conn = _gate_conn()
    bench, q005 = _operator_record_dicts(
        decision="fail_closed_only",
        accessions=[ACC],
        expected_git_revision="abc123",
    )
    bench_path, q005_path = _write_operator_dicts(
        tmp_path, bench, q005, prefix="gitrev"
    )
    calls = _spy_report_builder(monkeypatch)
    with pytest.raises(ValueError, match="git_revision"):
        benchmark_sec_parse_report_from_operator_inputs(
            conn, bench_path, q005_path, expected_git_revision="def456"
        )
    assert not calls
    conn.close()


def test_production_path_invokes_repository_validator(tmp_path, monkeypatch):
    """The production boundary must call the repository-owned validator."""
    conn = _gate_conn()
    bench_path, q005_path = _write_operator_records(
        tmp_path,
        decision="fail_closed_only",
        accessions=[ACC, ACC2],
        expected_git_revision="abc123",
    )
    calls = []

    def validator_spy(*args, **kwargs):
        calls.append((args, kwargs))

    # raising=False keeps this RED under the old boundary and GREEN once the
    # production function imports/calls the repository validator.
    monkeypatch.setattr(
        gate_report_module,
        "validate_m3_8b_operator_inputs",
        validator_spy,
        raising=False,
    )
    report = benchmark_sec_parse_report_from_operator_inputs(
        conn, bench_path, q005_path, expected_git_revision="abc123"
    )
    assert calls, (
        "benchmark_sec_parse_report_from_operator_inputs must call "
        "validate_m3_8b_operator_inputs"
    )
    assert report.numerator == 1  # A1 accepted; A2 fail-closed never counts
    conn.close()


def test_production_path_errors_are_path_safe(tmp_path, monkeypatch):
    conn = _gate_conn()
    bench, q005 = _operator_record_dicts(
        decision="fail_closed_only",
        accessions=[ACC],
        expected_git_revision="abc123",
    )
    bench["denominator"] = 99
    bench_path, q005_path = _write_operator_dicts(
        tmp_path, bench, q005, prefix="safe-err"
    )
    calls = _spy_report_builder(monkeypatch)
    with pytest.raises(ValueError) as exc:
        benchmark_sec_parse_report_from_operator_inputs(
            conn, bench_path, q005_path, expected_git_revision="abc123"
        )
    assert not calls
    msg = str(exc.value)
    assert str(tmp_path) not in msg
    assert str(Path.home()) not in msg
    conn.close()


def _primary_type_conn() -> sqlite3.Connection:
    """Two accepted-time filings: one stored 'primary_doc', one stored 'primary'."""
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_raw(conn, "raw:sec")
    _add_filing(
        conn, accession=ACC, eligible_at="2026-01-05T21:05:00Z",
        temporal_precision="accepted_time", accepted_time_recovered=1,
        eligibility_fail_closed=0, text=_BODY,
    )
    _add_filing(
        conn, accession="0000320193-26-000007", eligible_at="2026-01-05T21:05:00Z",
        temporal_precision="accepted_time", accepted_time_recovered=1,
        eligibility_fail_closed=0, text=_BODY, document_type="primary",
    )
    conn.commit()
    backfill_from_subtypes(conn)
    return conn


def test_primary_document_type_counts_in_numerator():
    """Frozen stored type 'primary' is the primary document (IN-set, 0A).

    A filing whose only document is ``document_type='primary'`` with otherwise
    valid hash-bound evidence must count in the DATA-01 numerator just like
    ``document_type='primary_doc'``.
    """
    conn = _primary_type_conn()
    report = benchmark_sec_parse_report(conn, [ACC, "0000320193-26-000007"])
    assert report.numerator == 2
    assert report.denominator == 2
    assert report.accepted_time_recovered_count == 2
    conn.close()
