"""Append source-side dedup events and project verified snapshots to a catalog.

This module does not connect to collectors and never writes to source snapshots.
Producers may append an event beside a body version in the caller's transaction.
Projection reads a manifest-verified immutable SQLite snapshot, closes that
read-only connection, and then writes all candidate rows and projection receipts
in one independent catalog transaction. There is deliberately no source ack:
replaying the same pinned snapshot is idempotent and cannot change its SHA.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_data.dedup import ingestion, links
from catalyst_data.dedup.links import EvidenceRecord
from catalyst_data.ingestion import input_manifest
from catalyst_data.ingestion.input_manifest import PreflightError
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.snapshots import (
    PinnedStore,
    VerifiedStores,
    readonly_snapshot,
    validate_verified_stores,
)
from catalyst_data.storage.time_corrections import (
    CorrectionTable,
    load_corrections,
)


class OutboxError(ValueError):
    """An outbox event or verified snapshot does not satisfy the contract."""


_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS dedup_relation_outbox (
    event_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    provider_record_id_kind TEXT,
    corrected_at TEXT NOT NULL,
    time_evidence_json TEXT NOT NULL,
    UNIQUE (source_id, source_version_id)
)
"""
_PROJECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS dedup_projection_receipts (
    store_id TEXT NOT NULL,
    source_snapshot_sha256 TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (store_id, source_snapshot_sha256, event_id)
)
"""
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_TIME_FIELDS = {"published_at", "provider_updated_at", "observed_at"}


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str):
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create an append-only producer outbox without committing caller work."""
    with _savepoint(conn, "dedup_outbox_schema"):
        conn.execute(_OUTBOX_SCHEMA)
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dedup_relation_outbox_no_update "
            "BEFORE UPDATE ON dedup_relation_outbox BEGIN "
            "SELECT RAISE(ABORT, 'dedup relation outbox is append-only'); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dedup_relation_outbox_no_delete "
            "BEFORE DELETE ON dedup_relation_outbox BEGIN "
            "SELECT RAISE(ABORT, 'dedup relation outbox is append-only'); END"
        )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OutboxError(f"{field} must be a nonempty trimmed string")
    return value


def _stamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutboxError(f"{field} must be a timezone-aware timestamp")
    try:
        return timefields.stamp(timefields.stored(value))
    except (TypeError, ValueError) as exc:
        raise OutboxError(f"{field} must be a timezone-aware timestamp") from exc


def _instant(value: Any, field: str):
    if not isinstance(value, str) or not value.strip():
        raise OutboxError(f"{field} must be a timezone-aware timestamp")
    try:
        return timefields.stored(value)
    except (TypeError, ValueError) as exc:
        raise OutboxError(f"{field} must be a timezone-aware timestamp") from exc


def _canonical_time_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(evidence)
    if normalized.get("kind") == "correction_table":
        normalized["current_at"] = _stamp(normalized.get("current_at"), "correction current_at")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _version_row(
    conn: sqlite3.Connection, source_id: str, source_version_id: str
) -> tuple[str, str, str, str, str, str | None, str | None, str] | None:
    return conn.execute(
        "SELECT document_id,title,body,body_sha256,observed_at,published_at,"
        "provider_updated_at,provenance FROM body_versions "
        "WHERE source_id=? AND version_id=?",
        (source_id, source_version_id),
    ).fetchone()


def _reject_finnhub_url_publisher_correction(time_evidence: Mapping[str, Any]) -> None:
    if (
        time_evidence.get("kind") != "correction_table"
        or "accession" in time_evidence
    ):
        return
    raise OutboxError(
        "Finnhub URL/publisher correction is disabled until source-specific provenance is verified"
    )


def _validate_record_body(
    record: EvidenceRecord,
    version: tuple[str, str, str, str, str, str | None, str | None, str],
) -> None:
    document_id, title, body, stored_hash, *_ = version
    actual_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if not body.strip() or stored_hash != actual_hash:
        raise OutboxError("source body version is incomplete or its body hash is invalid")
    if record.record_id != document_id or record.title != title:
        raise OutboxError("evidence identity/title does not match the immutable body version")
    if record.body_fingerprint_confirmed:
        if (
            record.fingerprint_version != "sha256-full-body-v1"
            or record.body_fingerprint != actual_hash
        ):
            raise OutboxError("confirmed full-body fingerprint differs from stored body hash")


def _validate_time_evidence_shape(
    time_evidence: Mapping[str, Any],
    *,
    source_version_id: str,
    corrected_at: str,
) -> None:
    kind = time_evidence.get("kind")
    if kind == "source_field":
        if (
            time_evidence.get("version_id") != source_version_id
            or time_evidence.get("field") not in _SOURCE_TIME_FIELDS
        ):
            raise OutboxError("source time evidence must identify a field on this exact version")
    elif kind == "correction_table":
        required = {"table_id", "sha256", "current_at"}
        if not required.issubset(time_evidence):
            raise OutboxError("correction evidence is missing its pinned table identity")
        if not isinstance(time_evidence.get("table_id"), str) or not isinstance(
            time_evidence.get("sha256"), str
        ) or not _SHA256.fullmatch(time_evidence["sha256"]):
            raise OutboxError("correction evidence has an invalid table identity")
        if not (
            isinstance(time_evidence.get("accession"), str)
            or (
                isinstance(time_evidence.get("url"), str)
                and isinstance(time_evidence.get("publisher"), str)
            )
        ):
            raise OutboxError("correction evidence must identify an exact source row")
        _stamp(time_evidence.get("current_at"), "correction current_at")
    else:
        raise OutboxError("time evidence must identify a source field or correction table row")
    _stamp(corrected_at, "corrected_at")


def _validate_correction_identity(
    record: EvidenceRecord,
    provider_record_id_kind: str | None,
    time_evidence: Mapping[str, Any],
) -> None:
    """Bind a correction row to this record without inferring publisher identity."""
    if time_evidence.get("kind") != "correction_table":
        return
    if "accession" in time_evidence:
        accession = time_evidence["accession"]
        if provider_record_id_kind != "filing_accession":
            raise OutboxError("SEC correction requires a typed filing_accession record ID")
        if record.provider.strip().casefold() != "sec":
            raise OutboxError("SEC correction requires an SEC provider record")
        if not isinstance(accession, str) or record.provider_record_id != accession:
            raise OutboxError("SEC correction accession differs from the record identity")
        if "url" in time_evidence or "publisher" in time_evidence:
            raise OutboxError("SEC correction evidence must identify the accession row only")
        return

    evidence_url = links.normalize_identity_url(time_evidence.get("url"))
    record_url = links.normalize_identity_url(record.url)
    if evidence_url is None or record_url is None or evidence_url != record_url:
        raise OutboxError("Finnhub correction URL differs from the record URL identity")
    # URL equality alone does not bind the publisher; the evidence-type guard
    # rejects this correction class regardless of the record's provider label.


def _event_payload(
    record: EvidenceRecord,
    *,
    source_id: str,
    source_version_id: str,
    provider_record_id_kind: str | None,
    corrected_at: str,
    time_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_version_id": source_version_id,
        "record_json": _canonical_json(asdict(record)),
        "provider_record_id_kind": provider_record_id_kind,
        "corrected_at": corrected_at,
        "time_evidence_json": _canonical_json(dict(time_evidence)),
    }


def _event_id(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def enqueue_candidate(
    conn: sqlite3.Connection,
    record: EvidenceRecord,
    *,
    source_id: str,
    source_version_id: str,
    provider_record_id_kind: str | None,
    corrected_at: str,
    time_evidence: Mapping[str, Any],
) -> str:
    """Append the relation payload beside its immutable body version.

    The producer owns the enclosing transaction and must call this before
    committing the source version. No store ID or snapshot SHA is recorded here;
    the verified snapshot supplies the store identity and digest at projection.
    """
    if not isinstance(record, EvidenceRecord):
        raise TypeError("record must be an EvidenceRecord")
    source_id = _text(source_id, "source_id")
    source_version_id = _text(source_version_id, "source_version_id")
    if provider_record_id_kind not in (None, "article", "filing_accession"):
        raise OutboxError("provider_record_id_kind must be article, filing_accession, or null")
    has_provider_id = isinstance(record.provider_record_id, str) and bool(
        record.provider_record_id.strip()
    )
    if has_provider_id and provider_record_id_kind is None:
        raise OutboxError("provider_record_id_kind is required for a provider record ID")
    if not isinstance(time_evidence, Mapping):
        raise OutboxError("time_evidence must be a mapping")
    ensure_schema(conn)
    version = _version_row(conn, source_id, source_version_id)
    if version is None:
        raise OutboxError("source version is not present in body_versions")
    _validate_record_body(record, version)
    _validate_time_evidence_shape(
        time_evidence,
        source_version_id=source_version_id,
        corrected_at=corrected_at,
    )
    _validate_correction_identity(record, provider_record_id_kind, time_evidence)
    _reject_finnhub_url_publisher_correction(time_evidence)
    if time_evidence.get("kind") == "source_field":
        field = time_evidence["field"]
        field_index = {
            "observed_at": 4,
            "published_at": 5,
            "provider_updated_at": 6,
        }[field]
        if version[field_index] is None or _instant(version[field_index], field) != _instant(
            corrected_at, "corrected_at"
        ):
            raise OutboxError("corrected_at does not match the referenced immutable source field")
        if version[7] != "versioned":
            raise OutboxError("source-field time evidence requires a versioned body")

    corrected = _stamp(corrected_at, "corrected_at")
    normalized_evidence = _canonical_time_evidence(time_evidence)
    payload = _event_payload(
        record,
        source_id=source_id,
        source_version_id=source_version_id,
        provider_record_id_kind=provider_record_id_kind,
        corrected_at=corrected,
        time_evidence=normalized_evidence,
    )
    event_id = _event_id(payload)
    with _savepoint(conn, "dedup_outbox_enqueue"):
        ensure_schema(conn)
        prior = conn.execute(
            "SELECT event_id,source_id,source_version_id,record_json,"
            "provider_record_id_kind,corrected_at,time_evidence_json "
            "FROM dedup_relation_outbox WHERE source_id=? AND source_version_id=?",
            (source_id, source_version_id),
        ).fetchone()
        expected = (
            event_id,
            payload["source_id"],
            payload["source_version_id"],
            payload["record_json"],
            payload["provider_record_id_kind"],
            payload["corrected_at"],
            payload["time_evidence_json"],
        )
        if prior is not None:
            if tuple(prior) != expected:
                raise OutboxError("source version already has a conflicting outbox event")
            return event_id
        conn.execute(
            "INSERT INTO dedup_relation_outbox "
            "(event_id,source_id,source_version_id,record_json,"
            "provider_record_id_kind,corrected_at,time_evidence_json) "
            "VALUES (?,?,?,?,?,?,?)",
            expected,
        )
    return event_id


def _manifest_correction_table(
    verified: VerifiedStores,
    table: CorrectionTable,
) -> None:
    receipt = input_manifest.verify(
        verified.manifest_path,
        invocation=list(verified.invocation),
        required_env=verified.required_env,
        environ=verified.environ_override,
    )
    matches = [
        Path(item["path"])
        for item in receipt["files"]
        if Path(item["path"]).is_file()
        and Path(item["path"]).suffix.lower() == ".csv"
        and Path(item["path"]).stem + ":" + table.sha256 == table.table_id
        and item["sha256"] == table.sha256
    ]
    if len(matches) != 1 or not _SHA256.fullmatch(table.sha256):
        raise OutboxError("correction table is not uniquely pinned by the input manifest")
    path = matches[0]
    parsed = load_corrections(
        path.read_bytes(),
        kind=table.kind,
        filename=path.name,
        expected_sha256=table.sha256,
    )
    if parsed != table:
        raise OutboxError("correction table differs from its manifest-pinned bytes")


def _validated_corrections(
    verified: VerifiedStores,
    correction_tables: Sequence[CorrectionTable],
) -> dict[str, CorrectionTable]:
    result: dict[str, CorrectionTable] = {}
    if not isinstance(correction_tables, (tuple, list)):
        raise OutboxError("correction_tables must be a sequence of CorrectionTable values")
    for table in correction_tables:
        if not isinstance(table, CorrectionTable):
            raise OutboxError("correction_tables must contain validated CorrectionTable values")
        _manifest_correction_table(verified, table)
        if table.table_id in result:
            raise OutboxError("duplicate correction table identity")
        result[table.table_id] = table
    return result


def _validate_version_and_time(
    conn: sqlite3.Connection,
    *,
    pin: PinnedStore,
    event: tuple,
    correction_tables: Mapping[str, CorrectionTable],
) -> tuple[EvidenceRecord, str, str]:
    event_id, source_id, source_version_id, record_json, provider_kind, corrected_at, time_json = event
    payload = {
        "source_id": source_id,
        "source_version_id": source_version_id,
        "record_json": record_json,
        "provider_record_id_kind": provider_kind,
        "corrected_at": corrected_at,
        "time_evidence_json": time_json,
    }
    if _event_id(payload) != event_id:
        raise OutboxError("outbox event digest does not match its immutable payload")
    try:
        record = EvidenceRecord(**json.loads(record_json))
        time_evidence = json.loads(time_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OutboxError("outbox event payload is invalid") from exc
    if not isinstance(time_evidence, dict):
        raise OutboxError("outbox time evidence must be a JSON object")
    if provider_kind not in (None, "article", "filing_accession"):
        raise OutboxError("outbox provider ID type is invalid")
    if record.provider_record_id and provider_kind is None:
        raise OutboxError("outbox provider ID has no explicit type")
    _validate_correction_identity(record, provider_kind, time_evidence)
    _reject_finnhub_url_publisher_correction(time_evidence)

    version = _version_row(conn, source_id, source_version_id)
    if version is None:
        raise OutboxError("outbox references a missing immutable source version")
    _validate_record_body(record, version)
    normalized_corrected = _stamp(corrected_at, "corrected_at")
    kind = time_evidence.get("kind")
    if kind == "source_field":
        _validate_time_evidence_shape(
            time_evidence,
            source_version_id=source_version_id,
            corrected_at=corrected_at,
        )
        field_index = {
            "observed_at": 4,
            "published_at": 5,
            "provider_updated_at": 6,
        }[time_evidence["field"]]
        field_value = version[field_index]
        if field_value is None or _instant(field_value, "source time") != _instant(
            corrected_at, "corrected_at"
        ):
            raise OutboxError("corrected_at differs from its referenced source field")
        if version[7] != "versioned":
            raise OutboxError("source-field time evidence requires a versioned body")
    elif kind == "correction_table":
        _validate_time_evidence_shape(
            time_evidence,
            source_version_id=source_version_id,
            corrected_at=corrected_at,
        )
        table_id = time_evidence["table_id"]
        table = correction_tables.get(table_id)
        if table is None or table.sha256 != time_evidence["sha256"]:
            raise OutboxError("outbox correction reference is not a verified pinned table")
        if "accession" in time_evidence and table.kind != "sec":
            raise OutboxError("filing accession correction requires a SEC correction table")
        if "accession" not in time_evidence and table.kind != "finnhub":
            raise OutboxError("URL/publisher correction requires a Finnhub correction table")
        current_at = _stamp(time_evidence["current_at"], "correction current_at")
        source_time = version[5]
        if source_time is None or _instant(source_time, "source published_at") != _instant(
            time_evidence["current_at"], "correction current_at"
        ):
            raise OutboxError("correction row original time differs from immutable source version")
        if "accession" in time_evidence:
            decision = table.apply(
                current_at,
                store=pin.store_id,
                accession=time_evidence["accession"],
            )
        else:
            decision = table.apply(
                current_at,
                store=pin.store_id,
                url=time_evidence["url"],
                publisher=time_evidence["publisher"],
            )
        if decision.status not in {"corrected", "already_corrected"}:
            raise OutboxError("pinned correction table has no applicable exact row")
        if decision.effective_at is None or _instant(
            decision.effective_at, "correction effective_at"
        ) != _instant(corrected_at, "corrected_at"):
            raise OutboxError("corrected_at differs from the pinned correction-table result")
    else:
        raise OutboxError("outbox event has no supported time evidence")

    audit_evidence = _canonical_time_evidence(time_evidence)
    if kind == "source_field":
        audit_evidence = {
            "kind": "source_assertion",
            "field": time_evidence["field"],
            "version_id": source_version_id,
        }
    return record, normalized_corrected, _canonical_json(audit_evidence)


def project_verified_snapshot(
    verified: VerifiedStores,
    store_id: str,
    catalog_conn: sqlite3.Connection,
    *,
    correction_tables: Sequence[CorrectionTable] = (),
) -> int:
    """Project all events from one pinned source snapshot in a catalog transaction.

    The store identity and file digest come only from ``VerifiedStores``. Source
    reads finish before the catalog transaction begins. The catalog receipt and
    all resulting candidates/links commit or roll back together. Returns how
    many new events were projected; no source table is updated or acknowledged.
    """
    if not isinstance(verified, VerifiedStores):
        raise PreflightError("source projection requires a VerifiedStores value")
    if not isinstance(store_id, str) or not store_id.strip() or store_id != store_id.strip():
        raise OutboxError("store_id must be a nonempty trimmed identity")
    validate_verified_stores(verified)
    pin = verified.stores.get(store_id)
    if not isinstance(pin, PinnedStore):
        raise PreflightError("store is not declared in the verified manifest")
    tables = _validated_corrections(verified, correction_tables)

    prepared = []
    with readonly_snapshot(verified, pin) as source_conn:
        present = source_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dedup_relation_outbox'"
        ).fetchone()
        if present is None:
            raise OutboxError("verified source snapshot has no dedup relation outbox")
        events = source_conn.execute(
            "SELECT event_id,source_id,source_version_id,record_json,"
            "provider_record_id_kind,corrected_at,time_evidence_json "
            "FROM dedup_relation_outbox ORDER BY event_id"
        ).fetchall()
        for event in events:
            record, corrected, correction_ref = _validate_version_and_time(
                source_conn,
                pin=pin,
                event=tuple(event),
                correction_tables=tables,
            )
            source_id = event[1]
            source_version_id = event[2]
            version = _version_row(source_conn, source_id, source_version_id)
            if version is None:
                raise OutboxError("outbox source version disappeared during projection")
            prepared.append((event[0], source_id, source_version_id, record, event[4], corrected, correction_ref))

    if catalog_conn.in_transaction:
        raise OutboxError("catalog connection must not have a caller-owned transaction")
    imported = 0
    try:
        catalog_conn.execute("BEGIN IMMEDIATE")
        ingestion.ensure_schema(catalog_conn)
        catalog_conn.execute(_PROJECTION_SCHEMA)
        catalog_conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dedup_projection_receipts_no_update "
            "BEFORE UPDATE ON dedup_projection_receipts BEGIN "
            "SELECT RAISE(ABORT, 'dedup projection receipts are append-only'); END"
        )
        catalog_conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dedup_projection_receipts_no_delete "
            "BEFORE DELETE ON dedup_projection_receipts BEGIN "
            "SELECT RAISE(ABORT, 'dedup projection receipts are append-only'); END"
        )
        for event_id, source_id, source_version_id, record, provider_kind, corrected, correction_ref in prepared:
            receipt = catalog_conn.execute(
                "SELECT 1 FROM dedup_projection_receipts "
                "WHERE store_id=? AND source_snapshot_sha256=? AND event_id=?",
                (pin.store_id, pin.sha256, event_id),
            ).fetchone()
            if receipt is not None:
                continue
            ingestion.ingest_record(
                catalog_conn,
                record,
                store_id=pin.store_id,
                source_id=source_id,
                version_id=source_version_id,
                provider_record_id_kind=provider_kind,
                corrected_at=corrected,
                correction_ref=correction_ref,
            )
            catalog_conn.execute(
                "INSERT INTO dedup_projection_receipts "
                "(store_id,source_snapshot_sha256,event_id) VALUES (?,?,?)",
                (pin.store_id, pin.sha256, event_id),
            )
            imported += 1
        catalog_conn.commit()
    except Exception:
        if catalog_conn.in_transaction:
            catalog_conn.rollback()
        raise
    return imported


__all__ = [
    "OutboxError",
    "ensure_schema",
    "enqueue_candidate",
    "project_verified_snapshot",
]
