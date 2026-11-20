"""Unified, explicit-mode reads over append-only body-version storage.

This is an additive data-core API. It does not alter legacy serving paths or
make source-role decisions. Callers must register ticker/window metadata for
each body version before it can participate in a read.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_data.ingestion import input_manifest
from catalyst_data.storage import legacy_cohorts
from catalyst_data.storage.legacy_cohorts import LegacyCohort
from catalyst_data.storage.legacy_read import validate_legacy_version
from catalyst_data.storage.snapshots import VerifiedStores, validate_verified_stores
from catalyst_data.storage.time_corrections import CorrectionTable, load_corrections


class TemporalReadError(ValueError):
    """Invalid temporal query or conflicting evidence metadata."""


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS temporal_evidence_versions (
        version_id TEXT PRIMARY KEY,
        evidence_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        published_at_source TEXT,
        published_at_precision TEXT NOT NULL CHECK (
            published_at_precision IN ('second', 'minute', 'day', 'unknown')),
        api_published TEXT,
        page_published TEXT,
        UNIQUE (evidence_id, version),
        FOREIGN KEY (version_id) REFERENCES body_versions(version_id)
    )""",
    """CREATE TRIGGER IF NOT EXISTS temporal_evidence_versions_no_update
       BEFORE UPDATE ON temporal_evidence_versions BEGIN
       SELECT RAISE(ABORT, 'temporal evidence versions are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS temporal_evidence_versions_no_delete
       BEFORE DELETE ON temporal_evidence_versions BEGIN
       SELECT RAISE(ABORT, 'temporal evidence versions are append-only'); END""",
    """CREATE TABLE IF NOT EXISTS temporal_evidence_map (
        version_id TEXT NOT NULL,
        ticker TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('body', 'headline', 'filing', 'fact')),
        eligible_at TEXT,
        evidence_id TEXT,
        url TEXT,
        canonical_url TEXT,
        publisher TEXT,
        publisher_family TEXT,
        provider TEXT,
        source_class TEXT,
        origin TEXT,
        sec_accession TEXT,
        evidence_role TEXT NOT NULL CHECK (evidence_role IN ('evidence', 'lead')),
        association_observed_at TEXT NOT NULL,
        is_backfill INTEGER NOT NULL CHECK (is_backfill IN (0, 1)),
        time_flags_json TEXT NOT NULL,
        PRIMARY KEY (version_id, ticker, kind),
        FOREIGN KEY (version_id) REFERENCES body_versions(version_id)
    )""",
    """CREATE INDEX IF NOT EXISTS temporal_evidence_ticker_eligible
       ON temporal_evidence_map(ticker, eligible_at, kind)""",
    """CREATE TRIGGER IF NOT EXISTS temporal_evidence_map_no_update
       BEFORE UPDATE ON temporal_evidence_map BEGIN
       SELECT RAISE(ABORT, 'temporal evidence metadata is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS temporal_evidence_map_no_delete
       BEFORE DELETE ON temporal_evidence_map BEGIN
       SELECT RAISE(ABORT, 'temporal evidence metadata is append-only'); END""",
)


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """Keep helpers composable and never commit a caller-owned transaction."""
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT temporal_evidence_write")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO temporal_evidence_write")
        conn.execute("RELEASE temporal_evidence_write")
        raise
    else:
        conn.execute("RELEASE temporal_evidence_write")


def _timestamp(value: datetime | None, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TemporalReadError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise TemporalReadError(f"{field} must be a nonempty string")
    return value


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the version-to-query metadata projection; requires body schema."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_versions'"
    ).fetchone()
    if exists is None:
        raise TemporalReadError("body_versions.ensure_schema must run first")
    with _atomic(conn):
        for statement in _SCHEMA:
            conn.execute(statement)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(temporal_evidence_map)")}
        if "sec_accession" not in columns:
            # Preserve old rows without inventing accession from origin/title.
            conn.execute("ALTER TABLE temporal_evidence_map ADD COLUMN sec_accession TEXT")
        if "evidence_role" not in columns:
            # Existing rows have no verified source-role metadata: default them
            # to leads so an upgrade cannot silently promote them to evidence.
            conn.execute(
                "ALTER TABLE temporal_evidence_map ADD COLUMN evidence_role TEXT NOT NULL "
                "DEFAULT 'lead' CHECK (evidence_role IN ('evidence', 'lead'))"
            )
        if "association_observed_at" not in columns:
            # NULL means the association's arrival time is unknown; observed
            # reads fail closed until a trustworthy timestamp is supplied.
            conn.execute(
                "ALTER TABLE temporal_evidence_map ADD COLUMN association_observed_at TEXT"
            )


def register_version(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    ticker: str,
    kind: str,
    evidence_id: str,
    evidence_role: str,
    association_observed_at: datetime,
    eligible_at: datetime | None = None,
    published_at_source: str | None = None,
    published_at_precision: str = "unknown",
    api_published: str | None = None,
    page_published: str | None = None,
    url: str | None = None,
    canonical_url: str | None = None,
    publisher: str | None = None,
    publisher_family: str | None = None,
    provider: str | None = None,
    source_class: str | None = None,
    origin: str | None = None,
    sec_accession: str | None = None,
    is_backfill: bool = False,
    time_flags: tuple[str, ...] | list[str] = (),
) -> int:
    """Attach immutable time and ticker/query metadata to a stored body version.

    ``eligible_at`` overrides the version's publication time when an explicit
    timefields/correction rule supplies a different eligibility timestamp.
    Legacy versions must provide their original ``legacy_eligible_at`` here.
    Version numbering is allocated once per evidence/version identity and is
    reused if the same version is associated with additional tickers.
    """
    version_id = _identifier(version_id, "version_id")
    evidence_id = _identifier(evidence_id, "evidence_id")
    ticker = _identifier(ticker.upper(), "ticker")
    if evidence_role not in {"evidence", "lead"}:
        raise TemporalReadError("evidence_role must be evidence or lead")
    association_observed = _timestamp(association_observed_at, "association_observed_at")
    if kind not in {"body", "headline", "filing", "fact"}:
        raise TemporalReadError("kind must be body, headline, filing, or fact")
    if sec_accession is not None and (
        kind != "filing" or not isinstance(sec_accession, str)
        or re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", sec_accession) is None
    ):
        raise TemporalReadError("sec_accession must be a canonical filing accession")
    if published_at_source is not None:
        published_at_source = _identifier(published_at_source, "published_at_source")
        if published_at_source not in {"sec_index", "api", "page", "rss", "wire_match", "correction"}:
            raise TemporalReadError("published_at_source is not a recognized contract source")
    if published_at_precision not in {"second", "minute", "day", "unknown"}:
        raise TemporalReadError("published_at_precision must be second, minute, day, or unknown")
    for field, value in (("api_published", api_published), ("page_published", page_published)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise TemporalReadError(f"{field} must be a nonempty string or None")
    if not isinstance(is_backfill, bool):
        raise TemporalReadError("is_backfill must be bool")
    if not isinstance(time_flags, (tuple, list)) or any(not isinstance(x, str) for x in time_flags):
        raise TemporalReadError("time_flags must be a sequence of strings")
    with _atomic(conn):
        stored = conn.execute(
            "SELECT provenance, legacy_eligible_at, published_at FROM body_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if stored is None:
            raise TemporalReadError("version_id is not present in body_versions")
        provenance, legacy_eligible, published = stored
        if provenance == "legacy":
            if eligible_at is None:
                eligible = legacy_eligible
            else:
                eligible = _timestamp(eligible_at, "eligible_at")
                if eligible != legacy_eligible:
                    raise TemporalReadError("legacy eligible_at must match legacy_eligible_at")
        else:
            eligible = _timestamp(eligible_at, "eligible_at", optional=True) or published

        version_record = conn.execute(
            "SELECT evidence_id,version,published_at_source,published_at_precision,api_published,page_published "
            "FROM temporal_evidence_versions WHERE version_id=?", (version_id,),
        ).fetchone()
        version_fields = (evidence_id, published_at_source, published_at_precision,
                          api_published, page_published)
        if version_record is None:
            next_version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM temporal_evidence_versions WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO temporal_evidence_versions (version_id,evidence_id,version,published_at_source,"
                "published_at_precision,api_published,page_published) VALUES (?,?,?,?,?,?,?)",
                (version_id, evidence_id, next_version, published_at_source,
                 published_at_precision, api_published, page_published),
            )
        else:
            if version_record[0] != evidence_id or tuple(version_record[2:]) != version_fields[1:]:
                raise TemporalReadError("temporal version identity or time metadata is immutable")
            next_version = version_record[1]

        fields = (version_id, ticker, kind, eligible, evidence_id, url, canonical_url,
                  publisher, publisher_family, provider, source_class, origin,
                  evidence_role, association_observed,
                  int(is_backfill), json.dumps(list(time_flags), ensure_ascii=False, separators=(",", ":")), sec_accession)
        current = conn.execute(
            "SELECT version_id,ticker,kind,eligible_at,evidence_id,url,canonical_url,publisher,"
            "publisher_family,provider,source_class,origin,evidence_role,association_observed_at,"
            "is_backfill,time_flags_json,sec_accession "
            "FROM temporal_evidence_map WHERE version_id=? AND ticker=? AND kind=?",
            (version_id, ticker, kind),
        ).fetchone()
        if current is not None:
            if tuple(current) != fields:
                raise TemporalReadError("temporal metadata for this version/ticker/kind is immutable")
            return next_version
        conn.execute(
            "INSERT INTO temporal_evidence_map (version_id,ticker,kind,eligible_at,evidence_id,url,"
            "canonical_url,publisher,publisher_family,provider,source_class,origin,evidence_role,"
            "association_observed_at,is_backfill,time_flags_json,sec_accession) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            fields,
        )
        return next_version


def _public_row(row: sqlite3.Row | tuple[Any, ...], temporal_status: str,
                lead_reason: str | None = None) -> dict[str, Any]:
    names = (
        "version_id", "source_id", "document_id", "title", "text", "body_sha256",
        "observed_at", "published_at", "provider_updated_at", "provenance",
        "migration_cohort_id", "legacy_eligible_at", "published_at_source",
        "published_at_precision", "api_published", "page_published", "version",
        "first_seen_at", "ticker", "kind",
        "eligible_at", "evidence_id", "url", "canonical_url", "publisher",
        "publisher_family", "provider", "source_class", "origin", "evidence_role",
        "association_observed_at", "is_backfill", "time_flags", "sec_accession", "effective_published_at",
    )
    item = dict(zip(names, row))
    item["time_flags"] = json.loads(item["time_flags"])
    item["is_backfill"] = bool(item["is_backfill"])
    item["temporal_status"] = temporal_status
    if lead_reason is not None:
        item["lead_reason"] = lead_reason
    return item


def _database_file_sha256(conn: sqlite3.Connection) -> str:
    try:
        main = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        path = Path(main)
        if not main or not path.is_absolute() or not path.is_file() or Path(str(path) + "-wal").exists():
            raise ValueError
        return input_manifest.fingerprint(path)
    except (OSError, RuntimeError, StopIteration, sqlite3.Error, ValueError):
        raise TemporalReadError("catalog-only raw legacy reads require a stable file snapshot") from None


def _has_raw_legacy_member(
    conn: sqlite3.Connection, cohort_id: str, members: set[tuple[str, str]],
) -> bool:
    ordered = sorted(members)
    # Bound the VALUES relation below SQLite's traditional 999-variable limit.
    for offset in range(0, len(ordered), 400):
        batch = ordered[offset:offset + 400]
        values = ",".join("(?,?)" for _ in batch)
        parameters = [value for member in batch for value in member]
        parameters.append(cohort_id)
        found = conn.execute(
            f"WITH raw_members(source_id,document_id) AS (VALUES {values}) "
            "SELECT 1 FROM raw_members AS r JOIN body_versions AS v "
            "ON v.source_id=r.source_id AND v.document_id=r.document_id "
            "WHERE v.provenance='legacy' AND v.legacy_cohort=? LIMIT 1",
            parameters,
        ).fetchone()
        if found is not None:
            return True
    return False


def _verified_legacy_cohorts(
    conn: sqlite3.Connection,
    allowed: list[str] | tuple[str, ...],
    verified: VerifiedStores | None,
    catalogs: dict[str, str | Path] | None,
    corrections: list[tuple[CorrectionTable, str]],
) -> dict[str, LegacyCohort]:
    if not allowed:
        if catalogs:
            raise TemporalReadError("legacy catalogs require an explicit legacy allowlist")
        return {}
    if not isinstance(verified, VerifiedStores) or not isinstance(catalogs, dict):
        raise TemporalReadError("legacy reads require verified stores and explicit pinned catalog paths")
    if set(catalogs) != set(allowed):
        raise TemporalReadError("legacy catalog paths must exactly match the allowlist")
    try:
        validate_verified_stores(verified)
        loaded = {}
        for cohort_id in allowed:
            cohort = legacy_cohorts.load_cohort(catalogs[cohort_id], verified)
            if cohort.cohort_id != cohort_id:
                raise ValueError("catalog identity differs from requested cohort")
            loaded[cohort_id] = cohort
            raw_members = {
                (record.source_id, record.document_id) for record in cohort.records
                if record.representation == "raw_full_body"
            }
            if raw_members and _has_raw_legacy_member(conn, cohort_id, raw_members):
                if conn.in_transaction and conn.execute("PRAGMA query_only").fetchone()[0] != 1:
                    raise ValueError(
                        "catalog-only raw legacy reads cannot use a writable caller transaction"
                    )
                if _database_file_sha256(conn) != cohort.source_snapshot_sha256:
                    raise ValueError("catalog-only raw body requires its exact source snapshot")

        if corrections:
            receipt = input_manifest.verify(
                verified.manifest_path, invocation=list(verified.invocation),
                required_env=verified.required_env, environ=verified.environ_override,
            )
            pinned = [Path(row["path"]) for row in receipt["files"]]
            for table, _store_id in corrections:
                matches = [path for path in pinned
                           if path.is_file() and path.name.endswith(".csv")
                           and path.stem + ":" + table.sha256 == table.table_id
                           and input_manifest.fingerprint(path) == table.sha256]
                if len(matches) != 1:
                    raise ValueError("correction table is not an unambiguous pinned CSV artifact")
                path = matches[0]
                parsed = load_corrections(
                    path.read_bytes(), kind=table.kind, filename=path.name,
                    expected_sha256=table.sha256,
                )
                if parsed != table:
                    raise ValueError("correction table differs from its pinned CSV bytes")
        validate_verified_stores(verified)
        return loaded
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
        if isinstance(exc, TemporalReadError):
            raise
        raise TemporalReadError(f"legacy cohort validation failed: {exc}") from None


def as_of(
    conn: sqlite3.Connection,
    ticker: str,
    window_start: datetime,
    window_end: datetime,
    at: datetime | None,
    *,
    mode: str,
    kinds: list[str] | tuple[str, ...] | None = None,
    allowed_legacy_cohorts: list[str] | tuple[str, ...] = (),
    legacy_verified_stores: VerifiedStores | None = None,
    legacy_catalogs: dict[str, str | Path] | None = None,
    correction_tables: list[tuple[CorrectionTable, str]] | tuple[tuple[CorrectionTable, str], ...] = (),
    data_snapshot_id: str,
    correction_table_id: str,
    contract_version: str = "data-core-v1.0",
    dedup_version: str = "same-article-release-v1",
) -> dict[str, Any]:
    """Read the final eligible version per document, separating leads.

    Observed mode requires both document first_seen_at and version observed_at
    to be no later than ``at``. Published mode uses the pinned legacy allowlist
    or the versioned publication/modification-time rules in the contract.
    Published mode is cut off at the stage ``window_end`` (contract v1.1): ``at``
    may be omitted (it then is ``window_end``) but may not be later, so a later
    ``at`` can never admit text rewritten after the window. Observed mode needs
    ``at``, the observed-as-of time.
    """
    if mode not in {"observed", "published"}:
        raise TemporalReadError("mode must explicitly be observed or published")
    ticker = _identifier(ticker.upper(), "ticker")
    start = _timestamp(window_start, "window_start")
    end = _timestamp(window_end, "window_end")
    if start >= end:
        raise TemporalReadError("window_start must precede window_end")
    if mode == "published":
        cutoff = end if at is None else _timestamp(at, "at")
        if cutoff > end:
            raise TemporalReadError("published at must not be later than window_end")
    else:
        cutoff = _timestamp(at, "at")
    snapshot = _identifier(data_snapshot_id, "data_snapshot_id")
    corrections = _identifier(correction_table_id, "correction_table_id")
    contract = _identifier(contract_version, "contract_version")
    dedup = _identifier(dedup_version, "dedup_version")
    if kinds is None:
        kinds = ("body", "headline", "filing", "fact")
    if not kinds or any(k not in {"body", "headline", "filing", "fact"} for k in kinds):
        raise TemporalReadError("kinds must contain valid evidence kinds")
    if any(not isinstance(x, str) or not x.strip() for x in allowed_legacy_cohorts):
        raise TemporalReadError("legacy cohort allowlist must contain nonempty strings")
    if len(set(allowed_legacy_cohorts)) != len(allowed_legacy_cohorts):
        raise TemporalReadError("legacy cohort allowlist must not contain duplicates")
    if not isinstance(correction_tables, (list, tuple)):
        raise TemporalReadError("correction_tables must be a sequence of table/store bindings")
    normalized_corrections: list[tuple[CorrectionTable, str]] = []
    for table, store_id in correction_tables:
        if not isinstance(table, CorrectionTable):
            raise TemporalReadError("correction_tables must contain validated CorrectionTable objects")
        normalized_corrections.append((table, _identifier(store_id, "correction_store_id")))
    if len({(table.table_id, store_id) for table, store_id in normalized_corrections}) != len(normalized_corrections):
        raise TemporalReadError("duplicate correction table/store binding")
    cohort_map = _verified_legacy_cohorts(
        conn, allowed_legacy_cohorts, legacy_verified_stores, legacy_catalogs,
        normalized_corrections,
    )
    correction_bindings = [
        {"table_id": table.table_id, "sha256": table.sha256, "store_id": store_id}
        for table, store_id in normalized_corrections
    ]

    placeholders = ",".join("?" for _ in kinds)
    rows = conn.execute(
        "SELECT v.version_id,v.source_id,v.document_id,v.title,v.body,v.body_sha256,"
        "v.observed_at,v.published_at,v.provider_updated_at,v.provenance,v.legacy_cohort,"
        "v.legacy_eligible_at,x.published_at_source,x.published_at_precision,x.api_published,"
        "x.page_published,x.version,d.first_seen_at,m.ticker,m.kind,m.eligible_at,m.evidence_id,"
        "m.url,m.canonical_url,m.publisher,m.publisher_family,m.provider,m.source_class,m.origin,"
        "m.evidence_role,m.association_observed_at,m.is_backfill,m.time_flags_json,m.sec_accession,v.rowid "
        "FROM body_versions v "
        "JOIN body_documents d ON d.source_id=v.source_id AND d.document_id=v.document_id "
        "JOIN temporal_evidence_versions x ON x.version_id=v.version_id "
        "JOIN temporal_evidence_map m ON m.version_id=v.version_id "
        f"WHERE m.ticker=? AND m.kind IN ({placeholders}) "
        "ORDER BY v.source_id,v.document_id,m.kind,v.observed_at,v.rowid",
        (ticker, *kinds),
    ).fetchall()

    # One latest admissible version per document/ticker/kind. Invalid newer
    # versions remain in leads, while an older verified version can still be
    # the historically available version.
    grouped: dict[tuple[str, str, str, str, str | None], list[dict[str, Any]]] = {}
    correction_conflicts = 0
    for row in rows:
        record = dict(zip((
            "version_id", "source_id", "document_id", "title", "text", "body_sha256",
            "observed_at", "published_at", "provider_updated_at", "provenance",
            "migration_cohort_id", "legacy_eligible_at", "published_at_source",
            "published_at_precision", "api_published", "page_published", "version",
            "first_seen_at", "ticker", "kind",
            "eligible_at", "evidence_id", "url", "canonical_url", "publisher",
            "publisher_family", "provider", "source_class", "origin", "evidence_role",
            "association_observed_at", "is_backfill", "time_flags_json", "sec_accession",
            "revision_order",
        ), row))
        record["effective_published_at"] = record["published_at"]
        record["_legacy_metadata"] = None
        correction_flags: list[str] = []
        if record["provenance"] == "legacy":
            cohort_id = record["migration_cohort_id"]
            if cohort_id not in allowed_legacy_cohorts or cohort_id not in cohort_map:
                continue
            cohort = cohort_map[cohort_id]
            try:
                cohort.require_corrections(correction_bindings)
                record["_legacy_metadata"] = validate_legacy_version(
                    conn, record, cohort, correction_bindings,
                )
                record["_legacy_metadata"]["catalog_snapshot_captured_at"] = cohort.snapshot_captured_at
                record["_legacy_capture_floor"] = cohort.snapshot_captured_at
            except (TypeError, ValueError) as exc:
                raise TemporalReadError(f"legacy cohort validation failed: {exc}") from None
        for table, store_id in normalized_corrections:
            if table.kind == "finnhub":
                if (record["eligible_at"] is None or record["url"] is None
                        or record["publisher"] is None):
                    continue
                decision = table.apply(
                    record["eligible_at"], store=store_id, url=record["url"],
                    publisher=record["publisher"],
                )
            elif record["kind"] == "filing":
                accession = record["sec_accession"]
                if accession is None:
                    continue
                decision = table.apply(
                    record["eligible_at"], store=store_id, accession=accession,
                )
            else:
                continue
            if decision.status in {"corrected", "already_corrected"}:
                record["eligible_at"] = decision.effective_at
                record["effective_published_at"] = decision.effective_at
                correction_flags.extend(decision.time_flags)
            elif decision.status == "conflict":
                correction_conflicts += 1
                correction_flags.append("correction_conflict:" + table.table_id)
        if not (record["eligible_at"] is not None and start < record["eligible_at"] <= end):
            continue
        record["time_flags_json"] = json.dumps(
            list(dict.fromkeys(json.loads(record["time_flags_json"]) + correction_flags)),
            ensure_ascii=False, separators=(",", ":"),
        )
        if mode == "observed":
            legacy_observed_at = max(
                record["first_seen_at"], record["observed_at"],
                record["association_observed_at"] or "", record.get("_legacy_capture_floor", ""),
            )
            if (legacy_observed_at > cutoff
                    or record["association_observed_at"] is None
                    or record["association_observed_at"] > cutoff):
                continue
            if record["provenance"] == "legacy" and record["eligible_at"] > cutoff:
                continue
            if record["evidence_role"] == "lead":
                status, reason = "lead", "evidence_role_lead"
            else:
                status, reason = "observed", None
        elif record["provenance"] == "legacy":
            if (record["migration_cohort_id"] not in allowed_legacy_cohorts
                    or record["eligible_at"] > cutoff):
                continue
            if record["evidence_role"] == "lead":
                status, reason = "lead", "evidence_role_lead"
            else:
                status, reason = "legacy_eligible_unverified_history", None
        else:
            if record["effective_published_at"] is None or record["effective_published_at"] > cutoff:
                continue
            if record["evidence_role"] == "lead":
                status, reason = "lead", "evidence_role_lead"
            elif record["provider_updated_at"] is None:
                status, reason = "point_in_time_unverified", "provider_updated_at_unknown"
            elif record["provider_updated_at"] > cutoff:
                status, reason = "point_in_time_unverified", "modified_after_cutoff"
            else:
                status, reason = "published_verified", None
        key = (record["source_id"], record["document_id"], record["kind"],
               record["provenance"], record["migration_cohort_id"])
        record["_status"] = status
        record["_lead_reason"] = reason
        grouped.setdefault(key, []).append(record)

    evidence: list[dict[str, Any]] = []
    leads: list[dict[str, Any]] = []
    for versions in grouped.values():
        # Keep all unverified published versions as leads; choose only the last
        # verified/legacy-eligible version as evidence. Published history follows
        # the provider's revision clock; retrieval order can differ during backfill.
        if mode == "published":
            versions.sort(key=lambda x: (
                x["provider_updated_at"] or "", x["version"], x["observed_at"],
                x["revision_order"],
            ))
        else:
            versions.sort(key=lambda x: (x["observed_at"], x["revision_order"]))
        valid = [v for v in versions if v["_lead_reason"] is None]
        if valid:
            selected = valid[-1]
            item = _public_row(_row_for_public(selected), selected["_status"])
            if selected["_legacy_metadata"] is not None:
                item.update(selected["_legacy_metadata"])
            evidence.append(item)
        for version in versions:
            if version["_lead_reason"] is None:
                continue
            item = _public_row(_row_for_public(version), version["_status"], version["_lead_reason"])
            if version["_legacy_metadata"] is not None:
                item.update(version["_legacy_metadata"])
            leads.append(item)

    evidence.sort(key=lambda x: (x["eligible_at"] or "", x["source_id"], x["document_id"], x["version_id"]))
    leads.sort(key=lambda x: (x["eligible_at"] or "", x["source_id"], x["document_id"], x["version_id"]))
    counts: dict[str, int] = {}
    for item in evidence + leads:
        counts[item["provenance"]] = counts.get(item["provenance"], 0) + 1
    if cohort_map:
        _verified_legacy_cohorts(
            conn, allowed_legacy_cohorts, legacy_verified_stores, legacy_catalogs,
            normalized_corrections,
        )
    return {
        "evidence": evidence,
        "leads": leads,
        "audit": {
            "mode": mode,
            "label": "回溯查询" if mode == "published" else "实时/前瞻查询",
            "cutoff": cutoff,
            "ticker": ticker,
            "window_start": start,
            "window_end": end,
            "data_snapshot_id": snapshot,
            "correction_table_id": corrections,
            "contract_version": contract,
            "dedup_version": dedup,
            "allowed_legacy_cohorts": sorted(allowed_legacy_cohorts),
            "provenance_counts": counts,
            "evidence_count": len(evidence),
            "lead_count": len(leads),
            "correction_conflict_count": correction_conflicts,
        },
    }


def _row_for_public(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record["version_id"], record["source_id"], record["document_id"], record["title"],
        record["text"], record["body_sha256"], record["observed_at"], record["published_at"],
        record["provider_updated_at"], record["provenance"], record["migration_cohort_id"],
        record["legacy_eligible_at"], record["published_at_source"],
        record["published_at_precision"], record["api_published"], record["page_published"],
        record["version"], record["first_seen_at"], record["ticker"], record["kind"],
        record["eligible_at"], record["evidence_id"], record["url"], record["canonical_url"],
        record["publisher"], record["publisher_family"], record["provider"], record["source_class"],
        record["origin"], record["evidence_role"], record["association_observed_at"],
        record["is_backfill"], record["time_flags_json"], record["sec_accession"],
        record["effective_published_at"],
    )
