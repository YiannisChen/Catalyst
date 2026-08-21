"""Benchmark-accession SEC parse metric report (DATA-01; M3-8).

Final Migration TSD §5.3 / execution-lock §E: the denominator is the unique
predeclared benchmark-relevant accession set bound before the rebuild and is
never redefined post hoc. An accession counts in the numerator only when its
primary document has non-empty, hash-bound successful extraction AND
``eligible_at`` derives from the authoritative accepted time or the declared
approved conservative rule. ``section_parse_degraded`` is separately reported
and never folds into the 90% gate.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from catalyst_data.canonical.ids import asset_id


@dataclass(frozen=True)
class SecParseReport:
    denominator: int
    numerator: int
    excluded_accessions: tuple[str, ...]
    accepted_time_recovered_count: int
    fail_closed_eligibility_count: int
    section_degraded_count: int
    gate_passed: bool


def _primary_document_hash_bound(
    conn: sqlite3.Connection, filing_id: str
) -> bool:
    """True when the primary document has non-empty, hash-bound extraction."""
    doc = conn.execute(
        "SELECT document_id, extraction_status, text FROM filing_documents "
        "WHERE filing_id=? AND document_type='primary_doc' "
        "ORDER BY document_url LIMIT 1",
        (filing_id,),
    ).fetchone()
    if doc is None:
        return False
    if doc["extraction_status"] != "success":
        return False
    if not doc["document_id"]:
        return False
    if not (doc["text"] or "").strip():
        return False
    bound = conn.execute(
        "SELECT COUNT(*) FROM canonical_subtype_assoc s "
        "JOIN canonical_content_versions v "
        "ON v.canonical_content_version_id = s.canonical_content_version_id "
        "WHERE s.subtype_table='filing_documents' AND s.subtype_pk_value=? "
        "AND v.content_hash IS NOT NULL AND length(v.content_hash)=64",
        (doc["document_id"],),
    ).fetchone()[0]
    return bound >= 1


def _filing_degraded(conn: sqlite3.Connection, filing_id: str) -> bool:
    row = conn.execute(
        "SELECT parse_quality FROM canonical_assets WHERE asset_id=?",
        (asset_id(asset_type="FILING", source_table="filings", source_pk=filing_id),),
    ).fetchone()
    return row is not None and row["parse_quality"] == "degraded"


def benchmark_sec_parse_report(
    conn: sqlite3.Connection,
    benchmark_accession_ids: Sequence[str],
    *,
    excluded_accessions: Sequence[str] = (),
    approved_latest_plausible: bool = False,
) -> SecParseReport:
    """Build the DATA-01 report over the predeclared accession set ``A``.

    ``excluded_accessions`` is report metadata only and never subtracts from
    numerator/denominator; a selection exclusion that is also a member of ``A``
    is a hard validation error. Fail-closed rows count in the numerator only
    when ``approved_latest_plausible`` is True (Q-005 approval).
    """
    accessions = list(benchmark_accession_ids)
    excluded = tuple(excluded_accessions)
    if len(set(accessions)) != len(accessions):
        raise ValueError("benchmark_accession_ids must be unique")
    overlap = set(accessions) & set(excluded)
    if overlap:
        raise ValueError(
            f"selection exclusions must not be members of A: {sorted(overlap)}"
        )

    denominator = len(accessions)
    numerator = 0
    accepted_time_recovered_count = 0
    fail_closed_eligibility_count = 0
    section_degraded_count = 0

    for accession in accessions:
        filing = conn.execute(
            "SELECT * FROM filings WHERE accession_number=?", (accession,)
        ).fetchone()
        if filing is None:
            continue  # missing/unrecoverable stays a denominator failure

        primary_ok = _primary_document_hash_bound(conn, filing["filing_id"])

        eligible_ok = False
        if filing["eligible_at"] is not None:
            if filing["accepted_time_recovered"] == 1:
                eligible_ok = True
                accepted_time_recovered_count += 1
            elif (
                filing["temporal_precision"] == "date_only_latest_plausible"
                and approved_latest_plausible
            ):
                eligible_ok = True
        if filing["eligible_at"] is None or (
            filing["temporal_precision"] == "date_only_latest_plausible"
        ):
            fail_closed_eligibility_count += 1

        if primary_ok and eligible_ok:
            numerator += 1
        if _filing_degraded(conn, filing["filing_id"]):
            section_degraded_count += 1

    return SecParseReport(
        denominator=denominator,
        numerator=numerator,
        excluded_accessions=excluded,
        accepted_time_recovered_count=accepted_time_recovered_count,
        fail_closed_eligibility_count=fail_closed_eligibility_count,
        section_degraded_count=section_degraded_count,
        gate_passed=(10 * numerator >= 9 * denominator),
    )


__all__ = ["SecParseReport", "benchmark_sec_parse_report"]
