"""Benchmark-accession SEC parse metric report (DATA-01; M3-8).

Final Migration TSD §5.3 / execution-lock §E: the denominator is the unique
predeclared benchmark-relevant accession set bound before the rebuild and is
never redefined post hoc (``denominator = len(A)`` permanently). An accession
counts in the numerator only when its primary document has non-empty,
hash-bound successful extraction AND ``eligible_at`` derives from the
authoritative accepted time or the declared approved conservative rule.
``section_parse_degraded`` is separately reported and never folds into the
90% gate.

Corrective Batch C: the report fails closed (never a ``SecParseReport`` with
``gate_passed=True``) on an empty/malformed/duplicate accession set, exclusions
intersecting ``A``, or more than one filing row per accession; hash-bound
evidence is the full locked chain (64-hex document_id, document binding,
foreign-asset rejection, FULL_TEXT content-hash recompute, parser and
materiality agreement). The production path reads the operator Q-005 record
instead of accepting an unauthenticated free boolean.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from catalyst_data.canonical.backfill import MATERIALITY_VERSION
from catalyst_data.canonical.ids import asset_id, sha256_identity
from catalyst_data.corpus.news_v2 import _normalize_text
from catalyst_data.sec.extract import SEC_EXTRACT_PARSER_VERSION
from catalyst_data.sec.m3_8b_validator import validate_m3_8b_operator_inputs

_EDGAR_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

@dataclass(frozen=True)
class SecParseReport:
    denominator: int
    numerator: int
    excluded_accessions: tuple[str, ...]
    accepted_time_recovered_count: int
    fail_closed_eligibility_count: int
    section_degraded_count: int
    gate_passed: bool


def _read_json(path: str | os.PathLike[str]) -> dict:
    """Read a JSON object record; errors stay path-safe (no absolute paths)."""
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError("unable to read operator record") from exc
    if not isinstance(value, dict):
        raise ValueError("operator record must be a JSON object")
    return value


def _primary_document_hash_bound(
    conn: sqlite3.Connection, filing_id: str
) -> bool:
    """True only when the primary document satisfies every locked evidence rule.

    Batch C: successful non-empty extraction, 64-hex ``document_id``, a
    ``canonical_subtype_assoc`` binding to a content version whose ``asset_id``
    is the FILING asset for this filing, a content hash that recomputes from
    the normalized stored text under §A.2 FULL_TEXT, and matching parser and
    materiality versions. Any miss is not a numerator.
    """
    doc = conn.execute(
        "SELECT document_id, extraction_status, text, parser_version "
        "FROM filing_documents "
        "WHERE filing_id=? AND document_type='primary_doc' "
        "ORDER BY document_url LIMIT 1",
        (filing_id,),
    ).fetchone()
    if doc is None:
        return False
    if doc["extraction_status"] != "success":
        return False
    text = (doc["text"] or "").strip()
    if not text:
        return False
    document_id = doc["document_id"]
    if not isinstance(document_id, str) or not _HEX64_RE.fullmatch(document_id):
        return False

    bound = conn.execute(
        """SELECT v.asset_id, v.content_hash, v.normalizer_version,
                  v.materiality_version
           FROM canonical_subtype_assoc s
           JOIN canonical_content_versions v
             ON v.canonical_content_version_id = s.canonical_content_version_id
           WHERE s.subtype_table='filing_documents'
             AND s.subtype_pk='document_id'
             AND s.subtype_pk_value=?
             AND s.canonical_content_version_id IS NOT NULL
           LIMIT 1""",
        (document_id,),
    ).fetchone()
    if bound is None:
        return False

    filing_asset = asset_id(
        asset_type="FILING", source_table="filings", source_pk=filing_id
    )
    if bound["asset_id"] != filing_asset:
        return False

    content_hash = bound["content_hash"]
    if not isinstance(content_hash, str) or not _HEX64_RE.fullmatch(content_hash):
        return False
    expected_hash = sha256_identity(
        {
            "content_state": "FULL_TEXT",
            "normalized_body": _normalize_text(text),
        }
    )
    if content_hash != expected_hash:
        return False

    expected_normalizer = doc["parser_version"] or SEC_EXTRACT_PARSER_VERSION
    if bound["normalizer_version"] != expected_normalizer:
        return False
    if bound["materiality_version"] != MATERIALITY_VERSION:
        return False
    return True


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
    is a hard validation error. ``approved_latest_plausible`` is the
    fixture-level Q-005 approval flag only; the production path
    (``benchmark_sec_parse_report_from_operator_inputs``) reads the operator
    Q-005 record and never accepts an unauthenticated free boolean.

    Fails closed (ValueError) when ``A`` is empty, contains a malformed or
    duplicate accession, exclusions intersect ``A``, or a filing row is
    duplicated for an accession in ``A``.
    """
    accessions = list(benchmark_accession_ids)
    excluded = tuple(excluded_accessions)
    if not accessions:
        raise ValueError("benchmark_accession_ids must not be empty (denominator would be 0)")
    for accession in accessions:
        if not isinstance(accession, str) or not _EDGAR_ACCESSION_RE.fullmatch(accession):
            raise ValueError(
                "benchmark accession must be a string matching "
                r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$: " + repr(accession)
            )
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
        row_count = conn.execute(
            "SELECT COUNT(*) FROM filings WHERE accession_number=?", (accession,)
        ).fetchone()[0]
        if row_count > 1:
            raise ValueError(
                f"accession {accession!r} maps to more than one filing row "
                f"({row_count}); fail closed"
            )
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


def benchmark_sec_parse_report_from_operator_inputs(
    conn: sqlite3.Connection,
    benchmark_path: str | os.PathLike[str],
    q005_path: str | os.PathLike[str],
    *,
    expected_git_revision: str,
) -> SecParseReport:
    """Production Q-005-gated DATA-01 report over operator records.

    Invokes ``catalyst_data.sec.m3_8b_validator.validate_m3_8b_operator_inputs``
    at this boundary before constructing any report, then reads the now-
    authenticated records to map the Q-005 decision onto
    ``approved_latest_plausible`` and pass ``A`` / exclusions into
    ``benchmark_sec_parse_report``. ``fail_closed_only`` never lets a
    fail-closed row into the numerator; only
    ``approve_latest_plausible_instant`` enables the approved conservative
    rule.
    """
    validate_m3_8b_operator_inputs(
        benchmark_path,
        q005_path,
        expected_git_revision=expected_git_revision,
    )
    benchmark = _read_json(benchmark_path)
    q005 = _read_json(q005_path)
    decision = q005["decision"]
    accessions = benchmark["ordered_unique_accession_ids"]

    excluded: list[str] = []
    raw_excluded = benchmark.get("excluded_accessions")
    if isinstance(raw_excluded, list):
        excluded.extend(a for a in raw_excluded if isinstance(a, str))
    raw_selection = benchmark.get("selection_exclusions")
    if isinstance(raw_selection, list):
        for entry in raw_selection:
            if isinstance(entry, dict) and isinstance(entry.get("accession"), str):
                excluded.append(entry["accession"])

    return benchmark_sec_parse_report(
        conn,
        accessions,
        excluded_accessions=excluded,
        approved_latest_plausible=(
            decision == "approve_latest_plausible_instant"
        ),
    )


__all__ = [
    "SecParseReport",
    "benchmark_sec_parse_report",
    "benchmark_sec_parse_report_from_operator_inputs",
]
