"""M3-8B operator-input validator (Corrective Batch C C8).

Validates the operator-supplied DATA-01 benchmark manifest and Q-005 SEC time
approval record before any seal/execution. Fail closed on any violation; error
strings are secret/path-safe: they never include file paths, home directories,
tokens, or absolute secret paths.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from catalyst_data.canonical.ids import sha256_identity

_EDGAR_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_BENCHMARK_SCHEMA_VERSION = "benchmark_accessions_v1"
_Q005_SCHEMA_VERSION = "q005_sec_time_approval_v1"
_Q005_DECISIONS = ("fail_closed_only", "approve_latest_plausible_instant")


def _read_json(path: str | os.PathLike[str]) -> dict:
    """Read a JSON object record; errors stay path-safe (no absolute paths)."""
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError("operator record unreadable or malformed") from exc
    if not isinstance(value, dict):
        raise ValueError("operator record must be a JSON object")
    return value


def _valid_utc_iso(value: object) -> bool:
    """True only for a timezone-aware timestamp normalized to UTC (offset 0)."""
    if not isinstance(value, str) or not value:
        return False
    raw = value.strip()
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return False
    if dt.tzinfo is None or dt.utcoffset() is None:
        return False
    return dt.utcoffset().total_seconds() == 0


def validate_m3_8b_operator_inputs(
    benchmark_path: str | os.PathLike[str],
    q005_path: str | os.PathLike[str],
    *,
    expected_git_revision: str,
) -> None:
    """Validate the operator-supplied DATA-01/Q-005 records; raise on violation.

    Exact schema versions, matching pre-seal ``git_revision``, non-empty
    ordered unique EDGAR accessions, ``denominator == len(A)``, exclusions
    disjoint from ``A``, locked ``case_list_sha256`` recompute, Q-005 decision
    literal, exact raw-payload inventory coverage, matching benchmark hash,
    non-empty selection authorities, and timezone-aware UTC ``frozen_at``.
    """
    benchmark = _read_json(benchmark_path)
    q005 = _read_json(q005_path)

    # 1. Exact schema versions.
    if benchmark.get("schema_version") != _BENCHMARK_SCHEMA_VERSION:
        raise ValueError(
            "benchmark record schema_version must be benchmark_accessions_v1"
        )
    if q005.get("schema_version") != _Q005_SCHEMA_VERSION:
        raise ValueError(
            "Q-005 record schema_version must be q005_sec_time_approval_v1"
        )

    # 2. Both git_revision values equal the expected pre-seal HEAD.
    if benchmark.get("git_revision") != expected_git_revision:
        raise ValueError(
            "benchmark record git_revision must equal the expected git_revision"
        )
    if q005.get("git_revision") != expected_git_revision:
        raise ValueError(
            "Q-005 record git_revision must equal the expected git_revision"
        )

    # 3/4. Non-empty ordered unique accession list with EDGAR grammar.
    accessions = benchmark.get("ordered_unique_accession_ids")
    if not isinstance(accessions, list) or not accessions:
        raise ValueError(
            "benchmark ordered_unique_accession_ids must be a non-empty list"
        )
    if len(accessions) != len(set(accessions)):
        raise ValueError(
            "benchmark ordered_unique_accession_ids must be unique"
        )
    for accession in accessions:
        if (
            not isinstance(accession, str)
            or not _EDGAR_ACCESSION_RE.fullmatch(accession)
        ):
            raise ValueError(
                "benchmark accession must be a string matching "
                r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$"
            )
    a = list(accessions)

    # 5. denominator == len(A), permanently.
    if benchmark.get("denominator") != len(a):
        raise ValueError(
            "benchmark denominator must equal len(ordered_unique_accession_ids)"
        )

    # 6. selection_exclusions disjoint from A; excluded_accessions never subtract.
    excluded_accessions = benchmark.get("excluded_accessions")
    if excluded_accessions is None:
        excluded_accessions = []
    if not isinstance(excluded_accessions, list):
        raise ValueError("benchmark excluded_accessions must be a list")
    selection_exclusions = benchmark.get("selection_exclusions")
    if selection_exclusions is None:
        selection_exclusions = []
    if not isinstance(selection_exclusions, list):
        raise ValueError("benchmark selection_exclusions must be a list")
    excluded_members: set[str] = {
        e for e in excluded_accessions if isinstance(e, str)
    }
    for entry in selection_exclusions:
        if not isinstance(entry, dict) or not isinstance(
            entry.get("accession"), str
        ):
            raise ValueError(
                "benchmark selection_exclusions entries must carry an accession"
            )
        excluded_members.add(entry["accession"])
    if excluded_members & set(a):
        raise ValueError(
            "benchmark selection exclusions must not intersect A"
        )

    # 7. case_list_sha256 recomputes over the locked payload.
    case_hash = benchmark.get("case_list_sha256")
    if not isinstance(case_hash, str):
        raise ValueError("benchmark case_list_sha256 must be a string")
    expected_hash = sha256_identity(
        {
            "schema_version": _BENCHMARK_SCHEMA_VERSION,
            "selection_authority": benchmark.get("selection_authority"),
            "ordered_unique_accession_ids": a,
        }
    )
    if case_hash != expected_hash:
        raise ValueError(
            "benchmark case_list_sha256 does not recompute under the locked "
            "serializer"
        )

    # 8. Q-005 decision is exactly one of the two literals.
    decision = q005.get("decision")
    if decision not in _Q005_DECISIONS:
        raise ValueError(
            "Q-005 decision must be exactly fail_closed_only or "
            "approve_latest_plausible_instant"
        )

    # 9. raw_payload_inventory exactly covers A (one per accession, no extras).
    inventory = q005.get("raw_payload_inventory")
    if not isinstance(inventory, list):
        raise ValueError("Q-005 raw_payload_inventory must be a list")
    inventory_accessions: list[str] = []
    for entry in inventory:
        if not isinstance(entry, dict) or not isinstance(
            entry.get("accession"), str
        ):
            raise ValueError(
                "Q-005 raw_payload_inventory entries must carry an accession"
            )
        inventory_accessions.append(entry["accession"])
    if len(inventory_accessions) != len(set(inventory_accessions)):
        raise ValueError(
            "Q-005 raw_payload_inventory must not duplicate accessions"
        )
    if set(inventory_accessions) != set(a):
        raise ValueError(
            "Q-005 raw_payload_inventory must exactly cover the benchmark "
            "accession set"
        )

    # 10. benchmark_case_list_sha256 matches the benchmark hash.
    if q005.get("benchmark_case_list_sha256") != case_hash:
        raise ValueError(
            "Q-005 benchmark_case_list_sha256 must match the benchmark "
            "case_list_sha256"
        )

    # 11. Non-empty selection_authority on both records.
    for label, record in (
        ("benchmark record", benchmark),
        ("Q-005 record", q005),
    ):
        authority = record.get("selection_authority")
        if not isinstance(authority, str) or not authority.strip():
            raise ValueError(
                f"{label} selection_authority must be non-empty"
            )

    # 12. Valid frozen_at: timezone-aware UTC ISO.
    for label, record in (
        ("benchmark record", benchmark),
        ("Q-005 record", q005),
    ):
        if not _valid_utc_iso(record.get("frozen_at")):
            raise ValueError(
                f"{label} frozen_at must be a timezone-aware UTC ISO timestamp"
            )


__all__ = ["validate_m3_8b_operator_inputs"]
