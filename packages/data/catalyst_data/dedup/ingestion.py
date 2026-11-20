"""Incrementally index immutable record versions and persist deterministic links.

Callers pass T1-corrected timestamps and their audit references. This module
checks their shape and stores them; it does not interpret raw provider times,
verify the external correction evidence, fetch data, or infer identity from
titles and SEC accessions.

Candidate records must be appended through :func:`ingest_record`; its transaction
keeps the record and all derived keys atomic. Schema setup performs one atomic
backfill for historical rows. Direct SQL writes or manual key deletion after
that migration are corruption and are not detected by scanning the full catalog
on every ingestion.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace

from catalyst_data.dedup import links
from catalyst_data.dedup.links import EvidenceRecord
from catalyst_data.ingestion.time import timefields

_RECORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS dedup_candidate_records (
    record_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_record_id_kind TEXT,
    corrected_at TEXT NOT NULL,
    correction_ref TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE (store_id, source_id, document_id, version_id)
)
"""

_KEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS dedup_candidate_keys (
    key_kind TEXT NOT NULL,
    key_namespace TEXT NOT NULL,
    key_value TEXT NOT NULL,
    record_id TEXT NOT NULL REFERENCES dedup_candidate_records(record_id),
    PRIMARY KEY (key_kind, key_namespace, key_value, record_id)
)
"""

_MIGRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS dedup_ingestion_migrations (
    migration_id TEXT PRIMARY KEY
)
"""
_BACKFILL_MIGRATION_ID = "candidate_keys_v1"


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT dedup_ingestion")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO dedup_ingestion")
        conn.execute("RELEASE dedup_ingestion")
        raise
    else:
        conn.execute("RELEASE dedup_ingestion")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the append-only candidate index without committing caller work."""
    with _atomic(conn):
        links.ensure_schema(conn)
        conn.execute(_RECORD_SCHEMA)
        record_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(dedup_candidate_records)")
        }
        if "provider_record_id_kind" not in record_columns:
            conn.execute(
                "ALTER TABLE dedup_candidate_records "
                "ADD COLUMN provider_record_id_kind TEXT"
            )
        conn.execute(_KEY_SCHEMA)
        conn.execute(_MIGRATION_SCHEMA)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dedup_candidate_key_lookup "
            "ON dedup_candidate_keys (key_kind, key_namespace, key_value, record_id)"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dedup_candidate_records_no_update "
            "BEFORE UPDATE ON dedup_candidate_records BEGIN "
            "SELECT RAISE(ABORT, 'dedup candidate records are append-only'); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dedup_candidate_records_no_delete "
            "BEFORE DELETE ON dedup_candidate_records BEGIN "
            "SELECT RAISE(ABORT, 'dedup candidate records are append-only'); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dedup_candidate_keys_no_update "
            "BEFORE UPDATE ON dedup_candidate_keys BEGIN "
            "SELECT RAISE(ABORT, 'dedup candidate keys are append-only'); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS dedup_candidate_keys_no_delete "
            "BEFORE DELETE ON dedup_candidate_keys BEGIN "
            "SELECT RAISE(ABORT, 'dedup candidate keys are append-only'); END"
        )
        _backfill_candidate_keys_once(conn)


def _backfill_candidate_keys_once(conn: sqlite3.Connection) -> None:
    migrated = conn.execute(
        "SELECT 1 FROM dedup_ingestion_migrations WHERE migration_id = ?",
        (_BACKFILL_MIGRATION_ID,),
    ).fetchone()
    if migrated is not None:
        return

    historical_rows = conn.execute(
        "SELECT evidence_json, provider_record_id_kind, record_id "
        "FROM dedup_candidate_records ORDER BY record_id"
    ).fetchall()
    for evidence_json, provider_record_id_kind, record_id in historical_rows:
        record = EvidenceRecord(**json.loads(evidence_json))
        keys = _candidate_keys(record, provider_record_id_kind)
        conn.executemany(
            "INSERT OR IGNORE INTO dedup_candidate_keys "
            "(key_kind, key_namespace, key_value, record_id) VALUES (?, ?, ?, ?)",
            [(kind, namespace, value, record_id) for kind, namespace, value in keys],
        )
    conn.execute(
        "INSERT INTO dedup_ingestion_migrations (migration_id) VALUES (?)",
        (_BACKFILL_MIGRATION_ID,),
    )


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a nonempty trimmed value")
    return value


def _record_key(store_id: str, source_id: str, document_id: str, version_id: str) -> str:
    # JSON tuple encoding is unambiguous even when identifiers contain separators.
    components = [store_id, source_id, document_id, version_id]
    return "record:v1:" + json.dumps(
        components, ensure_ascii=False, separators=(",", ":")
    )


def record_key_for(store_id: str, source_id: str, document_id: str, version_id: str) -> str:
    """Return the stable D1 key shared by ingestion and temporal read rows."""
    return _record_key(
        _text(store_id, "store_id"), _text(source_id, "source_id"),
        _text(document_id, "document_id"), _text(version_id, "version_id"),
    )


def _candidate_keys(
    record: EvidenceRecord, provider_record_id_kind: str | None
) -> tuple[tuple[str, str, str], ...]:
    keys: set[tuple[str, str, str]] = set()
    provider_record_id = record.provider_record_id
    if (
        provider_record_id_kind == "article"
        and isinstance(provider_record_id, str)
        and provider_record_id.strip()
    ):
        keys.add((
            "provider_record_id",
            record.provider.strip().casefold(),
            provider_record_id.strip(),
        ))

    canonical_url = links.normalize_identity_url(record.url)
    if canonical_url is not None:
        keys.add(("canonical_url", "", canonical_url))

    if record.release_id_confirmed:
        # EvidenceRecord validates the two required values at construction.
        keys.add((
            "confirmed_release_id",
            record.release_namespace.strip().casefold(),  # type: ignore[union-attr]
            record.release_id.strip(),  # type: ignore[union-attr]
        ))

    if record.body_fingerprint_confirmed:
        keys.add((
            "full_body_fingerprint",
            record.fingerprint_version.strip(),  # type: ignore[union-attr]
            record.body_fingerprint,  # type: ignore[arg-type]
        ))
    return tuple(sorted(keys))


def _validate_corrected_time(corrected_at: str, correction_ref: str) -> tuple[str, str]:
    corrected_at = _text(corrected_at, "corrected_at")
    correction_ref = _text(correction_ref, "correction_ref")
    try:
        # Parse only the caller-supplied corrected value, never an upstream raw time.
        normalized = timefields.stamp(timefields.stored(corrected_at))
    except (TypeError, ValueError) as exc:
        raise ValueError("corrected_at must be a timezone-aware corrected timestamp") from exc
    return normalized, correction_ref


def ingest_record(
    conn: sqlite3.Connection,
    record: EvidenceRecord,
    *,
    store_id: str,
    source_id: str,
    version_id: str,
    provider_record_id_kind: str | None = None,
    corrected_at: str,
    correction_ref: str,
) -> int:
    """Append one immutable record version and links found through indexed keys.

    The return value is the number of new deterministic link rows. Exact replay
    of the same full identity and content is idempotent. Reusing that identity
    with changed record/time data fails closed.
    """
    if not isinstance(record, EvidenceRecord):
        raise TypeError("record must be an EvidenceRecord")
    store_id = _text(store_id, "store_id")
    source_id = _text(source_id, "source_id")
    version_id = _text(version_id, "version_id")
    if provider_record_id_kind not in (None, "article", "filing_accession"):
        raise ValueError("provider_record_id_kind must be 'article' or 'filing_accession'")
    has_provider_record_id = (
        isinstance(record.provider_record_id, str)
        and bool(record.provider_record_id.strip())
    )
    if has_provider_record_id and provider_record_id_kind is None:
        raise ValueError("provider_record_id_kind is required when provider_record_id is present")
    corrected_at, correction_ref = _validate_corrected_time(corrected_at, correction_ref)
    document_id = _text(record.record_id, "record_id")
    record_key = record_key_for(store_id, source_id, document_id, version_id)
    evidence_json = json.dumps(
        asdict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    candidate_keys = _candidate_keys(record, provider_record_id_kind)

    with _atomic(conn):
        ensure_schema(conn)
        prior = conn.execute(
            "SELECT store_id, source_id, document_id, version_id, provider, corrected_at, "
            "provider_record_id_kind, "
            "correction_ref, evidence_json FROM dedup_candidate_records WHERE record_id = ?",
            (record_key,),
        ).fetchone()
        expected = (
            store_id, source_id, document_id, version_id, record.provider,
            corrected_at, provider_record_id_kind, correction_ref, evidence_json,
        )
        if prior is not None:
            if tuple(prior) != expected:
                raise ValueError("dedup identity collision: same version has different content")
            return 0

        candidate_ids: set[str] = set()
        for key_kind, key_namespace, key_value in candidate_keys:
            candidate_ids.update(
                row[0]
                for row in conn.execute(
                "SELECT k.record_id FROM dedup_candidate_keys AS k "
                "JOIN dedup_candidate_records AS r ON r.record_id = k.record_id "
                "WHERE k.key_kind = ? AND k.key_namespace = ? AND k.key_value = ? "
                "AND (k.key_kind != 'provider_record_id' "
                "OR r.provider_record_id_kind = 'article')",
                    (key_kind, key_namespace, key_value),
                )
            )

        conn.execute(
            "INSERT INTO dedup_candidate_records "
            "(record_id, store_id, source_id, document_id, version_id, provider, "
            "provider_record_id_kind, corrected_at, correction_ref, evidence_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record_key, store_id, source_id, document_id, version_id, record.provider,
             provider_record_id_kind, corrected_at, correction_ref, evidence_json),
        )
        conn.executemany(
            "INSERT INTO dedup_candidate_keys "
            "(key_kind, key_namespace, key_value, record_id) VALUES (?, ?, ?, ?)",
            [(kind, namespace, value, record_key) for kind, namespace, value in candidate_keys],
        )

        scoped_record = _record_for_classification(
            record, provider_record_id_kind, record_key
        )
        new_links = 0
        for candidate_id in sorted(candidate_ids):
            row = conn.execute(
                "SELECT evidence_json, provider_record_id_kind "
                "FROM dedup_candidate_records WHERE record_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("candidate index points to a missing record")
            candidate = EvidenceRecord(**json.loads(row[0]))
            candidate = _record_for_classification(candidate, row[1], candidate_id)
            if links.classify_pair(candidate, scoped_record):
                new_links += links.persist_pair_links(conn, candidate, scoped_record)
    return new_links


def _record_for_classification(
    record: EvidenceRecord, provider_record_id_kind: str | None, record_id: str
) -> EvidenceRecord:
    """Remove IDs that are explicitly not article identities before matching."""
    if provider_record_id_kind != "article":
        return replace(record, record_id=record_id, provider_record_id=None)
    return replace(record, record_id=record_id)


__all__ = ["ensure_schema", "ingest_record"]
