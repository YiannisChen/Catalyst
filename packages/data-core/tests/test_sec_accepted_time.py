"""M3-3: SEC accepted-time recovery tests (execution-lock §D).

``recover_accepted_time`` parses the authoritative accepted time for exactly the
requested accession from EDGAR submissions JSON ``recent`` parallel arrays.
Every ambiguous/misaligned case fails closed (returns None).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalyst_data.sec.accepted_time import (
    parse_edgar_acceptance_datetime,
    recover_accepted_time,
)


def _payload(*, accessions, acceptance_times, filing_dates=None, report_dates=None):
    recent = {
        "accessionNumber": list(accessions),
        "acceptanceDateTime": list(acceptance_times),
    }
    if filing_dates is not None:
        recent["filingDate"] = list(filing_dates)
    if report_dates is not None:
        recent["reportDate"] = list(report_dates)
    return {"recent": recent, "cik": "0000320193", "name": "Apple Inc."}


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_recover_target_accession_not_first():
    payload = _payload(
        accessions=["0000320193-26-000001", "0000320193-26-000002", "0000320193-26-000003"],
        acceptance_times=[
            "2026-01-05T20:30:00Z",
            "2026-01-05T21:05:00Z",
            "2026-01-05T22:10:00Z",
        ],
    )
    assert recover_accepted_time(payload, accession="0000320193-26-000002") == _utc(
        "2026-01-05T21:05:00Z"
    )


def test_recover_missing_target_accession():
    payload = _payload(
        accessions=["0000320193-26-000001"],
        acceptance_times=["2026-01-05T20:30:00Z"],
    )
    assert recover_accepted_time(payload, accession="0000320193-26-999999") is None


def test_recover_duplicate_target_accession():
    payload = _payload(
        accessions=["0000320193-26-000001", "0000320193-26-000001"],
        acceptance_times=["2026-01-05T20:30:00Z", "2026-01-05T21:05:00Z"],
    )
    assert recover_accepted_time(payload, accession="0000320193-26-000001") is None


def test_recover_unequal_parallel_array_lengths():
    payload = _payload(
        accessions=["0000320193-26-000001", "0000320193-26-000002"],
        acceptance_times=["2026-01-05T20:30:00Z"],
    )
    assert recover_accepted_time(payload, accession="0000320193-26-000001") is None


def test_recover_malformed_time_at_target_index():
    payload = _payload(
        accessions=["0000320193-26-000001"],
        acceptance_times=["not-a-time"],
    )
    assert recover_accepted_time(payload, accession="0000320193-26-000001") is None


def test_recover_missing_recent_block():
    assert recover_accepted_time({}, accession="0000320193-26-000001") is None
    assert recover_accepted_time(
        {"cik": "0000320193"}, accession="0000320193-26-000001"
    ) is None


def test_recover_missing_acceptance_datetime_column():
    payload = _payload(
        accessions=["0000320193-26-000001"], acceptance_times=[]
    )
    del payload["recent"]["acceptanceDateTime"]
    assert recover_accepted_time(payload, accession="0000320193-26-000001") is None


def test_parse_z_suffixed_iso():
    assert parse_edgar_acceptance_datetime("2026-01-05T21:05:00Z") == _utc(
        "2026-01-05T21:05:00Z"
    )


def test_parse_explicit_offset_iso_converted_to_utc():
    assert parse_edgar_acceptance_datetime("2026-01-05T16:05:00-05:00") == _utc(
        "2026-01-05T21:05:00Z"
    )


def test_parse_edgar_compact_est():
    # 2026-01-05 21:05:00 America/New_York (EST, UTC-5) -> 02:05Z next day.
    assert parse_edgar_acceptance_datetime("20260105210500") == _utc(
        "2026-01-06T02:05:00Z"
    )


def test_parse_edgar_compact_edt():
    # 2026-05-01 16:00:00 America/New_York (EDT, UTC-4) -> 20:00Z.
    assert parse_edgar_acceptance_datetime("20260501160000") == _utc(
        "2026-05-01T20:00:00Z"
    )


def test_parse_naive_iso_rejected():
    assert parse_edgar_acceptance_datetime("2026-01-05T21:05:00") is None


def test_parse_malformed_rejected():
    assert parse_edgar_acceptance_datetime("garbage") is None
    assert parse_edgar_acceptance_datetime("") is None
    assert parse_edgar_acceptance_datetime("2026-01-05T25:99:00Z") is None


def test_parse_dst_ambiguous_instant_rejected():
    # 2026-11-01 01:30:00 occurs twice in America/New_York (fall-back).
    assert parse_edgar_acceptance_datetime("20261101013000") is None


def test_parse_date_only_never_becomes_midnight_utc():
    assert parse_edgar_acceptance_datetime("2026-01-05") is None
    assert parse_edgar_acceptance_datetime("20260105") is None


def test_parse_outputs_are_always_utc_aware():
    for value in (
        "2026-01-05T21:05:00Z",
        "2026-01-05T16:05:00-05:00",
        "20260105210500",
    ):
        parsed = parse_edgar_acceptance_datetime(value)
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0
