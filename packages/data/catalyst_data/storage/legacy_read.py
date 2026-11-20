"""Validate one legacy result against its pinned cohort without reclassifying it."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping, Sequence

from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.legacy_cohorts import LegacyCohort

_CHUNK_METADATA_COLUMNS = (
    "version_id", "cohort_id", "cohort_manifest_sha256", "membership_sha256",
    "source_snapshot_sha256", "source_store_id", "source_id", "document_id",
    "asset_id", "chunk_id", "ordinal", "representation", "original_body_sha256",
    "eligibility_rule_id", "eligibility_rule_version", "correction_bindings_json",
)
_CHUNK_IDENTITY_FIELDS = ("asset_id", "chunk_id", "ordinal", "original_body_sha256")


def _required(record: Mapping, field: str):
    if field not in record:
        raise ValueError(f"legacy result is missing {field}")
    return record[field]


def _stamp(value, field: str) -> str:
    try:
        return timefields.stored(value).isoformat(timespec="microseconds")
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"legacy result has invalid {field}") from None


def _hash_text(value, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"legacy result has invalid {field}")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _metadata_row(conn: sqlite3.Connection, version_id: str) -> tuple | None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_version_metadata'"
    ).fetchone()
    if exists is None:
        return None
    try:
        return conn.execute(
            f"SELECT {','.join(_CHUNK_METADATA_COLUMNS)} FROM legacy_version_metadata WHERE version_id=?",
            (version_id,),
        ).fetchone()
    except sqlite3.Error:
        raise ValueError("legacy chunk metadata is unavailable or invalid") from None


def validate_legacy_version(
    conn: sqlite3.Connection,
    record: dict,
    cohort: LegacyCohort,
    correction_bindings: Sequence[Mapping[str, str]],
) -> dict:
    """Validate a returned legacy row against its pinned cohort and stored version.

    This validates identity and representation only. It does not apply date
    corrections, filter temporal windows, or approve the catalog's old role.
    Raw full bodies are supported only when explicitly catalogued and have no
    persisted import binding; this does not add raw-body import or approve an
    old eligibility policy.
    """
    if not isinstance(record, Mapping) or not isinstance(cohort, LegacyCohort):
        raise ValueError("a result mapping and validated legacy cohort are required")

    version_id = _required(record, "version_id")
    source_id = _required(record, "source_id")
    document_id = _required(record, "document_id")
    member = cohort.record(source_id, document_id)
    representation = member.representation
    if "representation" in record and record["representation"] != representation:
        raise ValueError("legacy result representation differs from cohort membership")

    if _required(record, "migration_cohort_id") != cohort.cohort_id:
        raise ValueError("legacy result is outside the pinned cohort")
    if (_required(record, "evidence_id") != member.evidence_id
            or _required(record, "kind") != member.kind
            or _required(record, "evidence_role") != member.evidence_role):
        raise ValueError("legacy result identity or role differs from cohort membership")
    ticker = _required(record, "ticker")
    if not isinstance(ticker, str) or ticker != ticker.upper() or ticker not in {
        item.upper() for item in member.tickers
    }:
        raise ValueError("legacy result ticker differs from cohort membership")
    eligible_at = _stamp(_required(record, "legacy_eligible_at"), "legacy_eligible_at")
    if eligible_at != member.eligible_at:
        raise ValueError("legacy eligibility time differs from cohort membership")

    title = _required(record, "title")
    text = _required(record, "text")
    if _hash_text(title, "title") != member.title_sha256:
        raise ValueError("legacy title hash differs from cohort membership")
    if _hash_text(text, "text") != member.text_sha256:
        raise ValueError("legacy text hash differs from cohort membership")

    if _required(record, "provenance") != "legacy":
        raise ValueError("legacy result has non-legacy provenance")
    if "body_sha256" in record and record["body_sha256"] != _hash_text(text, "text"):
        raise ValueError("legacy body hash differs from stored text")
    correction_bindings = tuple(correction_bindings) if correction_bindings is not None else None
    cohort.require_corrections(correction_bindings)

    try:
        stored = conn.execute(
            "SELECT source_id,document_id,title,body,body_sha256,provenance,legacy_cohort,legacy_eligible_at "
            "FROM body_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if stored is None:
            raise ValueError("legacy result version is missing from storage")
        if (stored[0] != source_id or stored[1] != document_id or stored[2] != title
                or stored[3] != text or stored[4] != _hash_text(text, "text")
                or stored[5] != "legacy" or stored[6] != cohort.cohort_id
                or _stamp(stored[7], "stored legacy_eligible_at") != member.eligible_at):
            raise ValueError("legacy result differs from its immutable stored version")
        association = conn.execute(
            "SELECT evidence_id,kind,evidence_role,eligible_at FROM temporal_evidence_map "
            "WHERE version_id=? AND ticker=? AND kind=?",
            (version_id, ticker, member.kind),
        ).fetchone()
    except sqlite3.Error:
        raise ValueError("legacy result storage is unavailable or invalid") from None
    if association is None or (
        association[0] != member.evidence_id or association[1] != member.kind
        or association[2] != member.evidence_role
        or _stamp(association[3], "stored eligible_at") != member.eligible_at
    ):
        raise ValueError("legacy result association differs from cohort membership")

    if representation == "legacy_chunk":
        metadata_row = _metadata_row(conn, version_id)
        if metadata_row is None:
            raise ValueError("legacy chunk metadata is missing")
        metadata = dict(zip(_CHUNK_METADATA_COLUMNS, metadata_row))
        correction_json = json.dumps(
            [list(binding) for binding in cohort.correction_bindings],
            ensure_ascii=True, separators=(",", ":"),
        )
        expected = {
            "version_id": version_id,
            "cohort_id": cohort.cohort_id,
            "cohort_manifest_sha256": cohort.manifest_sha256,
            "membership_sha256": cohort.membership_sha256,
            "source_snapshot_sha256": cohort.source_snapshot_sha256,
            "source_store_id": cohort.source_store_id,
            "source_id": member.source_id,
            "document_id": member.document_id,
            "asset_id": member.asset_id,
            "chunk_id": member.chunk_id,
            "ordinal": member.ordinal,
            "representation": member.representation,
            "original_body_sha256": member.original_body_sha256,
            "eligibility_rule_id": cohort.eligibility_rule_id,
            "eligibility_rule_version": cohort.eligibility_rule_version,
            "correction_bindings_json": correction_json,
        }
        if metadata != expected:
            raise ValueError("legacy chunk metadata differs from the pinned cohort")
        if any(record.get(field) not in (None, member.__dict__.get(field))
               for field in _CHUNK_IDENTITY_FIELDS):
            raise ValueError("legacy chunk identity differs from cohort membership")
    elif representation == "raw_full_body":
        if any(member.__dict__.get(field) is not None for field in _CHUNK_IDENTITY_FIELDS):
            raise ValueError("raw legacy body cannot carry chunk identity")
        if any(record.get(field) is not None for field in _CHUNK_IDENTITY_FIELDS):
            raise ValueError("raw legacy body result cannot carry chunk metadata")
        if _metadata_row(conn, version_id) is not None:
            raise ValueError("raw legacy body cannot carry chunk metadata")
    else:
        raise ValueError("unsupported legacy representation")

    return {
        "representation": representation,
        "binding_scope": (
            "persistent_import_metadata" if representation == "legacy_chunk" else "catalog_only"
        ),
        "asset_id": member.asset_id,
        "chunk_id": member.chunk_id,
        "ordinal": member.ordinal,
        "original_body_sha256": member.original_body_sha256,
        "cohort_manifest_sha256": cohort.manifest_sha256,
        "membership_sha256": cohort.membership_sha256,
        "source_snapshot_sha256": cohort.source_snapshot_sha256,
        "eligibility_rule_id": cohort.eligibility_rule_id,
        "eligibility_rule_version": cohort.eligibility_rule_version,
    }


__all__ = ["validate_legacy_version"]
