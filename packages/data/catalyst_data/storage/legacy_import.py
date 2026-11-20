"""Copy explicitly catalogued original web-body chunks into legacy versions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval import evidence_fts_projection
from catalyst_data.storage import body_versions, temporal_evidence
from catalyst_data.storage.legacy_cohorts import LegacyCohort

_CHUNK_COLUMNS = (
    "asset_id", "ordinal", "chunk_id", "text", "content_hash", "body_sha256",
    "canonical_url", "title", "publisher", "source_class", "eligible_at",
    "fetched_at", "capture_mode",
)
_HEADLINE_COLUMNS = ("asset_id", "ticker", "eligible_at")
_METADATA_COLUMNS = (
    "version_id", "cohort_id", "cohort_manifest_sha256", "membership_sha256",
    "source_snapshot_sha256", "source_store_id", "source_id", "document_id",
    "asset_id", "chunk_id", "ordinal", "representation", "original_body_sha256",
    "eligibility_rule_id", "eligibility_rule_version", "correction_bindings_json",
)

_METADATA_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS legacy_version_metadata (
        version_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        cohort_manifest_sha256 TEXT NOT NULL,
        membership_sha256 TEXT NOT NULL,
        source_snapshot_sha256 TEXT NOT NULL,
        source_store_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        representation TEXT NOT NULL CHECK (representation = 'legacy_chunk'),
        original_body_sha256 TEXT NOT NULL,
        eligibility_rule_id TEXT NOT NULL,
        eligibility_rule_version TEXT NOT NULL,
        correction_bindings_json TEXT NOT NULL,
        FOREIGN KEY (version_id) REFERENCES body_versions(version_id),
        UNIQUE (cohort_id, source_id, document_id),
        UNIQUE (cohort_id, asset_id, ordinal),
        UNIQUE (cohort_id, chunk_id)
    )""",
    """CREATE TRIGGER IF NOT EXISTS legacy_version_metadata_no_update
       BEFORE UPDATE ON legacy_version_metadata BEGIN
       SELECT RAISE(ABORT, 'legacy version metadata is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS legacy_version_metadata_no_delete
       BEFORE DELETE ON legacy_version_metadata BEGIN
       SELECT RAISE(ABORT, 'legacy version metadata is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS legacy_version_metadata_no_replace
       BEFORE INSERT ON legacy_version_metadata
       WHEN EXISTS (SELECT 1 FROM legacy_version_metadata WHERE version_id=NEW.version_id)
       BEGIN SELECT RAISE(ABORT, 'existing legacy version metadata cannot be replaced'); END""",
)


@contextmanager
def _destination_batch(destination: sqlite3.Connection):
    if not destination.in_transaction:
        destination.execute("BEGIN")
    destination.execute("SAVEPOINT legacy_chunk_import")
    try:
        yield
    except Exception:
        destination.execute("ROLLBACK TO legacy_chunk_import")
        destination.execute("RELEASE legacy_chunk_import")
        raise
    else:
        destination.execute("RELEASE legacy_chunk_import")


def _main_path(conn: sqlite3.Connection, field: str) -> Path:
    rows = conn.execute("PRAGMA database_list").fetchall()
    main = next((row[2] for row in rows if row[1] == "main"), "")
    if not main:
        raise ValueError(f"{field} must be a file-backed SQLite database")
    path = Path(main)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{field} must be an existing non-symlink SQLite file")
    return path.resolve()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema(conn: sqlite3.Connection, table: str, expected: tuple[str, ...]) -> None:
    obj = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
    if obj is None or obj[0] != "table":
        raise ValueError(f"legacy source is missing required table {table}")
    columns = tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{table}")'))
    if columns != expected:
        raise ValueError(f"legacy source table {table} has an invalid schema")


def _utc(value: str, field: str) -> str:
    try:
        return timefields.stored(value).isoformat(timespec="microseconds")
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"legacy source has invalid {field}") from None


def _reconcile(source: sqlite3.Connection, cohort: LegacyCohort) -> list[tuple]:
    if not {"web_body_chunks", "web_headlines"} <= set(cohort.source_tables):
        raise ValueError("legacy cohort must declare web_body_chunks and web_headlines")
    _schema(source, "web_body_chunks", _CHUNK_COLUMNS)
    _schema(source, "web_headlines", _HEADLINE_COLUMNS)

    seen_documents: set[tuple[str, str]] = set()
    seen_chunks: set[tuple[str, int]] = set()
    seen_chunk_ids: set[str] = set()
    reconciled = []
    for record in cohort.records:
        if record.representation != "legacy_chunk":
            raise ValueError("legacy chunk importer accepts legacy_chunk representation only")
        if (record.asset_id is None or record.chunk_id is None or record.ordinal is None
                or record.original_body_sha256 is None):
            raise ValueError("legacy chunk identity is incomplete")
        document_key = (record.source_id, record.document_id)
        chunk_key = (record.asset_id, record.ordinal)
        if document_key in seen_documents or chunk_key in seen_chunks or record.chunk_id in seen_chunk_ids:
            raise ValueError("duplicate legacy document or source chunk identity")
        seen_documents.add(document_key)
        seen_chunks.add(chunk_key)
        seen_chunk_ids.add(record.chunk_id)

        rows = source.execute(
            "SELECT " + ",".join(_CHUNK_COLUMNS) +
            " FROM web_body_chunks WHERE asset_id=? AND ordinal=?",
            (record.asset_id, record.ordinal),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("legacy cohort chunk is missing or ambiguous in source")
        row = dict(zip(_CHUNK_COLUMNS, rows[0]))
        if row["chunk_id"] != record.chunk_id:
            raise ValueError("legacy chunk ID differs from the source row")
        text = row["text"]
        title = row["title"]
        if not isinstance(text, str) or not text.strip() or hashlib.sha256(text.encode("utf-8")).hexdigest() != record.text_sha256:
            raise ValueError("legacy chunk text hash differs from the catalog")
        if row["content_hash"] != hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise ValueError("legacy chunk stored text hash conflicts with its text")
        if not isinstance(title, str) or hashlib.sha256(title.encode("utf-8")).hexdigest() != record.title_sha256:
            raise ValueError("legacy title hash differs from the catalog")
        if row["body_sha256"] != record.original_body_sha256:
            raise ValueError("legacy original body hash differs from the source row")
        if _utc(row["eligible_at"], "eligible_at") != record.eligible_at:
            raise ValueError("legacy eligible_at differs from the source row")

        headline_rows = source.execute(
            "SELECT ticker,eligible_at FROM web_headlines WHERE asset_id=? ORDER BY ticker",
            (record.asset_id,),
        ).fetchall()
        source_tickers = tuple(row[0] for row in headline_rows)
        if source_tickers != tuple(sorted(record.tickers)):
            raise ValueError("legacy ticker associations differ from the source rows")
        if any(_utc(eligible_at, "headline eligible_at") != record.eligible_at
               for _ticker, eligible_at in headline_rows):
            raise ValueError("legacy ticker association eligibility differs from the catalog")
        reconciled.append((record, row))
    return sorted(reconciled, key=lambda item: (
        item[0].evidence_id, item[0].source_id, item[0].document_id,
        item[0].asset_id, item[0].ordinal,
    ))


def _metadata_values(cohort: LegacyCohort, record, version_id: str) -> tuple:
    correction_bindings = json.dumps(
        [list(binding) for binding in cohort.correction_bindings],
        ensure_ascii=True, separators=(",", ":"),
    )
    return (
        version_id, cohort.cohort_id, cohort.manifest_sha256, cohort.membership_sha256,
        cohort.source_snapshot_sha256, cohort.source_store_id, record.source_id,
        record.document_id, record.asset_id, record.chunk_id, record.ordinal,
        record.representation, record.original_body_sha256, cohort.eligibility_rule_id,
        cohort.eligibility_rule_version, correction_bindings,
    )


def migrate_legacy_chunks(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    *,
    cohort: LegacyCohort,
) -> dict:
    """Import the exact catalogued chunk rows as separate legacy body versions.

    The source must be a committed, file-backed snapshot whose bytes match the
    cohort SHA and whose tables match every listed chunk and ticker association.
    This adapter neither concatenates chunks nor classifies evidence roles. It
    records snapshot capture as the earliest proven presence/association time;
    provider publication and modification clocks remain unknown. All destination
    writes live in a savepoint and remain caller-committed.
    """
    if source is destination:
        raise ValueError("source and destination must be distinct connections")
    if not isinstance(cohort, LegacyCohort):
        raise ValueError("a validated legacy cohort is required")
    if source.in_transaction:
        raise ValueError("legacy source must have no uncommitted transaction")
    source_path = _main_path(source, "source")
    destination_path = _main_path(destination, "destination")
    try:
        same_file = source_path.samefile(destination_path)
    except OSError:
        raise ValueError("cannot verify that source and destination are separate SQLite files") from None
    if same_file:
        raise ValueError("source and destination must be separate SQLite files")
    if Path(str(source_path) + "-wal").exists():
        raise ValueError("legacy source snapshot must not have a WAL sidecar")
    journal_mode = source.execute("PRAGMA journal_mode").fetchone()[0]
    if str(journal_mode).casefold() == "wal":
        raise ValueError("legacy source snapshot must not use WAL mode")

    before_sha = _file_sha256(source_path)
    if before_sha != cohort.source_snapshot_sha256:
        raise ValueError("legacy source bytes do not match the cohort snapshot SHA")
    previous_query_only = source.execute("PRAGMA query_only").fetchone()[0]
    inserted = existing = projected = 0
    try:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        reconciled = _reconcile(source, cohort)
        with _destination_batch(destination):
            body_versions.ensure_schema(destination)
            temporal_evidence.ensure_schema(destination)
            evidence_fts_projection.ensure_schema(destination)
            for statement in _METADATA_SCHEMA:
                destination.execute(statement)

            # Confirm no source bytes changed while the read snapshot was held;
            # the destination savepoint still owns all writes if this fails.
            for record, row in reconciled:
                observed_at = timefields.stored(cohort.snapshot_captured_at)
                eligible_at = timefields.stored(record.eligible_at)
                old = destination.execute(
                    "SELECT 1 FROM body_versions WHERE source_id=? AND document_id=? AND provenance='legacy' "
                    "AND legacy_cohort=? AND legacy_eligible_at=? AND title=? AND body=?",
                    (record.source_id, record.document_id, cohort.cohort_id,
                     record.eligible_at, row["title"], row["text"]),
                ).fetchone()
                version_id = body_versions.put_version(
                    destination, source_id=record.source_id, document_id=record.document_id,
                    title=row["title"], body=row["text"], observed_at=observed_at,
                    provenance="legacy", legacy_cohort=cohort.cohort_id,
                    legacy_eligible_at=eligible_at,
                )
                if old is None:
                    inserted += 1
                else:
                    existing += 1

                metadata_values = _metadata_values(cohort, record, version_id)
                prior_metadata = destination.execute(
                    f"SELECT {','.join(_METADATA_COLUMNS)} FROM legacy_version_metadata WHERE version_id=?",
                    (version_id,),
                ).fetchone()
                if prior_metadata is None:
                    destination.execute(
                        f"INSERT INTO legacy_version_metadata ({','.join(_METADATA_COLUMNS)}) "
                        f"VALUES ({','.join('?' for _ in _METADATA_COLUMNS)})",
                        metadata_values,
                    )
                elif tuple(prior_metadata) != metadata_values:
                    raise ValueError("legacy version metadata conflicts with the pinned cohort")

                for ticker in record.tickers:
                    temporal_evidence.register_version(
                        destination, version_id=version_id, ticker=ticker, kind=record.kind,
                        evidence_id=record.evidence_id, evidence_role=record.evidence_role,
                        association_observed_at=observed_at, eligible_at=eligible_at,
                        url=row["canonical_url"], canonical_url=row["canonical_url"],
                        publisher=row["publisher"], provider=record.source_id,
                        source_class=row["source_class"], is_backfill=True,
                    )

                if record.evidence_role == "evidence":
                    evidence_class = "filing_evidence" if record.kind == "filing" else "body_evidence"
                    projected += evidence_fts_projection.project_evidence_text(destination, [
                        evidence_fts_projection.EvidenceText(
                            evidence_id=record.evidence_id, version_id=version_id,
                            source_id=record.source_id, document_id=record.document_id,
                            provider=record.source_id, kind=record.kind,
                            evidence_class=evidence_class, title=row["title"], text=row["text"],
                            origin=None, provenance="legacy",
                            observed_at=observed_at.isoformat(timespec="microseconds"),
                            published_at=None, provider_updated_at=None,
                        )
                    ])

            # Keep the source read transaction open until after this check.
            # With WAL refused above, the snapshot cannot be replaced between
            # reconciliation and the destination savepoint release.
            if _file_sha256(source_path) != before_sha:
                raise ValueError("legacy source snapshot changed during import")
    finally:
        if source.in_transaction:
            source.rollback()
        if not previous_query_only:
            source.execute("PRAGMA query_only=OFF")
    return {
        "cohort_id": cohort.cohort_id,
        "source_snapshot_sha256": before_sha,
        "listed_chunks": len(reconciled),
        "inserted_versions": inserted,
        "existing_versions": existing,
        "projected_evidence_chunks": projected,
        "provenance": "legacy",
        "full_text_reconstructed": False,
        "role_policy_approved": False,
    }


__all__ = ["migrate_legacy_chunks"]
