"""Additive full-text projection for explicitly classified evidence versions.

This module does not decide source eligibility. The caller must classify each
row using the data contract and source registration; provider names alone are
never consulted to promote a row into evidence. The projection is independent
of the currently served corpus FTS index.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Literal

EvidenceKind = Literal["body", "filing", "headline", "fact"]
EvidenceClass = Literal["body_evidence", "filing_evidence", "lead"]


class EvidenceTextProjectionError(ValueError):
    """Invalid explicit evidence classification or conflicting projection."""


@dataclass(frozen=True)
class EvidenceText:
    """One explicit evidence/version input row.

    ``evidence_class`` is a decision supplied by the caller, not inferred from
    ``provider``. Body and filing evidence are projected; leads are skipped.
    """

    evidence_id: str
    version_id: str
    source_id: str
    document_id: str
    provider: str
    kind: EvidenceKind
    evidence_class: EvidenceClass
    title: str
    text: str
    origin: str | None
    provenance: Literal["legacy", "versioned"]
    observed_at: str
    published_at: str | None
    provider_updated_at: str | None


_TABLE_COLUMNS = (
    "projection_id", "evidence_id", "version_id", "source_id", "document_id",
    "provider", "kind", "evidence_class", "title", "text", "body_sha256",
    "origin", "provenance", "observed_at", "published_at", "provider_updated_at",
)


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT evidence_fts_projection")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO evidence_fts_projection")
        conn.execute("RELEASE evidence_fts_projection")
        raise
    else:
        conn.execute("RELEASE evidence_fts_projection")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the isolated metadata and FTS5 projection tables idempotently."""
    with _atomic(conn):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS evidence_text_projection (
                projection_id TEXT PRIMARY KEY,
                evidence_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('body', 'filing')),
                evidence_class TEXT NOT NULL CHECK (
                    evidence_class IN ('body_evidence', 'filing_evidence')),
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                body_sha256 TEXT NOT NULL,
                origin TEXT,
                provenance TEXT NOT NULL CHECK (provenance IN ('legacy', 'versioned')),
                observed_at TEXT NOT NULL,
                published_at TEXT,
                provider_updated_at TEXT,
                UNIQUE (evidence_id, version_id)
            )"""
        )
        conn.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS evidence_text_fts USING fts5(
                title,
                text,
                evidence_id UNINDEXED,
                version_id UNINDEXED,
                source_id UNINDEXED,
                document_id UNINDEXED,
                provider UNINDEXED,
                kind UNINDEXED,
                evidence_class UNINDEXED,
                origin UNINDEXED,
                provenance UNINDEXED,
                content='evidence_text_projection',
                content_rowid='rowid'
            )"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS evidence_text_projection_no_update
               BEFORE UPDATE ON evidence_text_projection BEGIN
               SELECT RAISE(ABORT, 'evidence text projection is append-only'); END"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS evidence_text_projection_no_delete
               BEFORE DELETE ON evidence_text_projection BEGIN
               SELECT RAISE(ABORT, 'evidence text projection is append-only'); END"""
        )


def _required(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceTextProjectionError(f"invalid_{field}")
    return value


def _row_values(row: EvidenceText) -> tuple[tuple[object, ...] | None, str | None]:
    if row.evidence_class == "lead":
        if row.kind in {"body", "filing"}:
            return None, None
        if row.kind not in {"headline", "fact"}:
            raise EvidenceTextProjectionError("invalid_kind")
        return None, None

    if row.evidence_class not in {"body_evidence", "filing_evidence"}:
        raise EvidenceTextProjectionError("invalid_evidence_class")
    expected = "body" if row.evidence_class == "body_evidence" else "filing"
    if row.kind != expected:
        raise EvidenceTextProjectionError("evidence_class_kind_mismatch")

    for field in ("evidence_id", "version_id", "source_id", "document_id", "provider",
                  "title", "observed_at"):
        _required(getattr(row, field), field)
    if row.provenance not in {"legacy", "versioned"}:
        raise EvidenceTextProjectionError("invalid_provenance")
    if not isinstance(row.text, str) or not row.text.strip():
        raise EvidenceTextProjectionError("empty_evidence_text")
    if row.origin is not None and not isinstance(row.origin, str):
        raise EvidenceTextProjectionError("invalid_origin")
    for field in ("published_at", "provider_updated_at"):
        value = getattr(row, field)
        if value is not None and not isinstance(value, str):
            raise EvidenceTextProjectionError(f"invalid_{field}")

    body_sha256 = hashlib.sha256(row.text.encode("utf-8")).hexdigest()
    identity = ("evidence_text_projection_v1", row.evidence_id, row.version_id)
    projection_id = hashlib.sha256(
        json.dumps(identity, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    values: tuple[object, ...] = (
        projection_id, row.evidence_id, row.version_id, row.source_id,
        row.document_id, row.provider, row.kind, row.evidence_class, row.title,
        row.text, body_sha256, row.origin, row.provenance, row.observed_at,
        row.published_at, row.provider_updated_at,
    )
    return values, projection_id


def project_evidence_text(
    conn: sqlite3.Connection, rows: Iterable[EvidenceText]
) -> int:
    """Append eligible body/filing versions to this isolated FTS projection.

    Returns the number of newly added evidence versions. Exact replay is
    idempotent. A previously used ``(evidence_id, version_id)`` with different
    metadata or text raises rather than overwriting history. ``lead`` rows are
    intentionally omitted. FTS5 availability is required and errors propagate.
    """
    ensure_schema(conn)
    inserted = 0
    with _atomic(conn):
        for row in rows:
            values, projection_id = _row_values(row)
            if values is None:
                continue
            existing = conn.execute(
                f"SELECT {','.join(_TABLE_COLUMNS)} FROM evidence_text_projection "
                "WHERE projection_id=?",
                (projection_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise EvidenceTextProjectionError("projection_identity_conflict")
                continue

            conn.execute(
                f"INSERT INTO evidence_text_projection ({','.join(_TABLE_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in _TABLE_COLUMNS)})",
                values,
            )
            conn.execute(
                """INSERT INTO evidence_text_fts
                   (rowid, title, text, evidence_id, version_id, source_id,
                    document_id, provider, kind, evidence_class, origin, provenance)
                   SELECT rowid, title, text, evidence_id, version_id, source_id,
                          document_id, provider, kind, evidence_class, origin, provenance
                   FROM evidence_text_projection WHERE projection_id=?""",
                (projection_id,),
            )
            inserted += 1
    return inserted


__all__ = [
    "EvidenceText",
    "EvidenceTextProjectionError",
    "ensure_schema",
    "project_evidence_text",
]
