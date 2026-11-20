"""Append-only complete body versions, independent of serving projections.

Call ``ensure_schema`` first. Callers own their SQLite connection and transaction:
helpers neither open files nor commit. Observed times are supplied by the intake
or explicit migration, never inferred from provider publication/modification time.
This storage module does not implement eligibility or as-of selection.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


class BodyVersionError(ValueError):
    """Invalid body provenance or conflicting historical observation."""


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS body_documents (
        source_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        PRIMARY KEY (source_id, document_id)
    )""",
    """CREATE TABLE IF NOT EXISTS body_versions (
        version_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        published_at TEXT,
        provider_updated_at TEXT,
        provenance TEXT NOT NULL CHECK (provenance IN ('versioned', 'legacy')),
        legacy_cohort TEXT,
        legacy_eligible_at TEXT,
        FOREIGN KEY (source_id, document_id) REFERENCES body_documents(source_id, document_id),
        CHECK ((provenance = 'versioned' AND legacy_cohort IS NULL AND legacy_eligible_at IS NULL)
            OR (provenance = 'legacy' AND legacy_cohort IS NOT NULL AND legacy_eligible_at IS NOT NULL))
    )""",
    """CREATE INDEX IF NOT EXISTS body_versions_document_observed
       ON body_versions(source_id, document_id, observed_at)""",
    """CREATE TRIGGER IF NOT EXISTS body_versions_no_update
       BEFORE UPDATE ON body_versions BEGIN
       SELECT RAISE(ABORT, 'body versions are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS body_versions_no_delete
       BEFORE DELETE ON body_versions BEGIN
       SELECT RAISE(ABORT, 'body versions are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS body_versions_no_replace
       BEFORE INSERT ON body_versions
       WHEN EXISTS (SELECT 1 FROM body_versions WHERE version_id=NEW.version_id)
       BEGIN SELECT RAISE(ABORT, 'existing body versions cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS body_documents_no_update
       BEFORE UPDATE ON body_documents BEGIN
       SELECT RAISE(ABORT, 'document first observation is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS body_documents_no_delete
       BEFORE DELETE ON body_documents BEGIN
       SELECT RAISE(ABORT, 'document first observation is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS body_documents_no_replace
       BEFORE INSERT ON body_documents
       WHEN EXISTS (SELECT 1 FROM body_documents
                    WHERE source_id=NEW.source_id AND document_id=NEW.document_id)
       BEGIN SELECT RAISE(ABORT, 'existing document observation cannot be replaced'); END""",
)

_COLUMNS = (
    "version_id", "source_id", "document_id", "title", "body", "body_sha256",
    "observed_at", "published_at", "provider_updated_at", "provenance",
    "legacy_cohort", "legacy_eligible_at",
)


@contextmanager
def _atomic(conn: sqlite3.Connection):
    # Releasing an outermost savepoint commits it; keep an outer transaction
    # open so a caller can still roll back a successful standalone operation.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT body_version_write")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO body_version_write")
        conn.execute("RELEASE body_version_write")
        raise
    else:
        conn.execute("RELEASE body_version_write")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create additive tables and guards without committing caller work."""
    with _atomic(conn):
        for statement in _SCHEMA:
            conn.execute(statement)


def _identity(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise BodyVersionError(f"{field} must be a nonempty identifier")
    return value


def _timestamp(value: datetime | None, *, field: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BodyVersionError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def put_version(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    document_id: str,
    title: str,
    body: str,
    observed_at: datetime,
    first_seen_at: datetime | None = None,
    published_at: datetime | None = None,
    provider_updated_at: datetime | None = None,
    provenance: str = "versioned",
    legacy_cohort: str | None = None,
    legacy_eligible_at: datetime | None = None,
) -> str:
    """Append a full content/provenance version and return its stable identity.

    Exact replay at a later observation retains the first persisted observation.
    Version identity includes title, exact body, provider times, and provenance,
    but excludes observed_at. Revisions with unchanged provider timestamps still
    append when the actual content changes. A new document's first_seen_at is its
    initial observed_at unless a separately proven first_seen_at is supplied;
    later writes cannot alter that document observation.

    Legacy migration requires a named cohort and the old eligibility timestamp.
    That timestamp is stored separately and never becomes observed_at. Missing
    provider timestamps remain unknown. Historical migrations are permitted;
    this module does not compare supplied timestamps to the wall clock.
    """
    source_id = _identity(source_id, "source_id")
    document_id = _identity(document_id, "document_id")
    if not isinstance(title, str):
        raise BodyVersionError("title must be a string")
    if not isinstance(body, str) or not body.strip():
        raise BodyVersionError("body must contain complete nonempty text")
    if provenance not in {"versioned", "legacy"}:
        raise BodyVersionError("provenance must be versioned or legacy")
    if provenance == "legacy":
        legacy_cohort = _identity(legacy_cohort, "legacy_cohort")
        eligible = _timestamp(legacy_eligible_at, field="legacy_eligible_at")
    else:
        if legacy_cohort is not None or legacy_eligible_at is not None:
            raise BodyVersionError("versioned content cannot carry legacy fields")
        eligible = None
    observed = _timestamp(observed_at, field="observed_at")
    first_seen = _timestamp(first_seen_at, field="first_seen_at", optional=True)
    if first_seen is not None and first_seen > observed:
        raise BodyVersionError("first observation cannot follow a body observation")
    published = _timestamp(published_at, field="published_at", optional=True)
    updated = _timestamp(provider_updated_at, field="provider_updated_at", optional=True)
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    identity = ("body_version_v1", source_id, document_id, title, body, published,
                updated, provenance, legacy_cohort, eligible)
    version_id = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    values = (version_id, source_id, document_id, title, body, body_hash, observed,
              published, updated, provenance, legacy_cohort, eligible)
    with _atomic(conn):
        document = conn.execute("SELECT first_seen_at FROM body_documents WHERE source_id=? AND document_id=?",
                                (source_id, document_id)).fetchone()
        if document is None:
            conn.execute("INSERT INTO body_documents VALUES (?,?,?)", (source_id, document_id, first_seen or observed))
        elif first_seen is not None and first_seen != document[0]:
            raise BodyVersionError("document first observation cannot be replaced")
        elif observed < document[0]:
            observed = document[0]
            values = (version_id, source_id, document_id, title, body, body_hash, observed,
                      published, updated, provenance, legacy_cohort, eligible)
        existing = conn.execute(f"SELECT {','.join(_COLUMNS)} FROM body_versions WHERE version_id=?", (version_id,)).fetchone()
        if existing is not None:
            # Compare every field except observation; do not trust a hash alone.
            if tuple(existing[:6]) + tuple(existing[7:]) != values[:6] + values[7:]:
                raise BodyVersionError("version identity conflicts with stored content")
            return version_id
        conn.execute(f"INSERT INTO body_versions ({','.join(_COLUMNS)}) VALUES ({','.join('?' for _ in _COLUMNS)})", values)
    return version_id


def list_versions(conn: sqlite3.Connection, *, source_id: str, document_id: str) -> list[dict]:
    """Return complete versions in observation order; no temporal eligibility filter."""
    _identity(source_id, "source_id")
    _identity(document_id, "document_id")
    selection = ",".join("v." + column for column in _COLUMNS)
    rows = conn.execute(
        f"SELECT {selection},d.first_seen_at FROM body_versions v JOIN body_documents d "
        "ON d.source_id=v.source_id AND d.document_id=v.document_id "
        "WHERE v.source_id=? AND v.document_id=? ORDER BY v.observed_at,v.rowid",
        (source_id, document_id),
    )
    return [dict(zip((*_COLUMNS, "first_seen_at"), row)) for row in rows]
