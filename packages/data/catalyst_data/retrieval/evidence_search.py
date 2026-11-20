"""Read-only FTS adapter gated by the authoritative temporal evidence reader.

FTS supplies candidate identities, never evidence text or source eligibility.
Only the exact versions allowed by ``as_of`` may match. Ranking is chronological,
not BM25: future/ineligible index contents must not influence a historical rank.
This independent projection is not connected to the served agent/app index.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from catalyst_data.storage import temporal_evidence
from catalyst_data.storage.as_of_stores import as_of_stores
from catalyst_data.storage.snapshots import (
    VerifiedStores,
    readonly_snapshot,
    validate_verified_stores,
)
from catalyst_data.storage.time_corrections import CorrectionTable

MAX_LIMIT = 200
# Five identity parameters per row, plus MATCH; stay below the historical
# SQLite default of 999 variables without relying on newer runtime limits.
_IDENTITY_BATCH_SIZE = 100
_IDENTITY_FIELDS = ("evidence_id", "version_id", "source_id", "document_id", "kind")


class EvidenceSearchError(ValueError):
    """Invalid search inputs or an unusable/inconsistent FTS projection."""


@contextmanager
def _read_snapshot(conn: sqlite3.Connection):
    """Keep eligibility and FTS reads consistent, preserving caller transactions."""
    owns_transaction = not conn.in_transaction
    previous_query_only = conn.execute("PRAGMA query_only").fetchone()[0]
    if owns_transaction:
        conn.execute("PRAGMA query_only=ON")
        try:
            conn.execute("BEGIN")
        except Exception:
            conn.execute(f"PRAGMA query_only={int(previous_query_only)}")
            raise
    try:
        yield
    finally:
        if owns_transaction:
            try:
                conn.rollback()
            finally:
                conn.execute(f"PRAGMA query_only={int(previous_query_only)}")


def _identity(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(row[field] for field in _IDENTITY_FIELDS)


def _require_readable_index(conn: sqlite3.Connection) -> None:
    """Probe required columns and the FTS index even for an empty eligible set."""
    try:
        conn.execute(
            "SELECT rowid,evidence_id,version_id,source_id,document_id,kind,"
            "evidence_class,title,text,body_sha256 FROM evidence_text_projection LIMIT 0"
        ).fetchall()
        conn.execute(
            "SELECT rowid FROM evidence_text_fts WHERE evidence_text_fts MATCH ? LIMIT 1",
            ('"catalystschemareadabilityprobe"',),
        ).fetchall()
    except sqlite3.DatabaseError:
        raise EvidenceSearchError("evidence FTS schema is missing or unreadable") from None


def _matching_identities(
    conn: sqlite3.Connection, query: str, eligible: dict[tuple[str, ...], dict[str, Any]]
) -> set[tuple[str, ...]]:
    matches: set[tuple[str, ...]] = set()
    identities = sorted(eligible)
    if not identities:
        try:
            # Validate the caller's MATCH expression even when temporal
            # filtering leaves no identities to intersect with the index.
            conn.execute(
                "SELECT rowid FROM evidence_text_fts WHERE evidence_text_fts MATCH ? LIMIT 1",
                (query,),
            ).fetchone()
        except sqlite3.OperationalError:
            raise EvidenceSearchError("FTS query could not be executed against the evidence index") from None
        return matches
    for offset in range(0, len(identities), _IDENTITY_BATCH_SIZE):
        batch = identities[offset:offset + _IDENTITY_BATCH_SIZE]
        placeholders = ",".join("(?,?,?,?,?)" for _ in batch)
        parameters = [value for identity in batch for value in identity]
        # SQL structure comes only from fixed identifiers and batch length.
        # The complete FTS expression and every evidence identity are bound.
        eligible_cte = f"""WITH eligible(evidence_id,version_id,source_id,document_id,kind) AS (
                    VALUES {placeholders}
                )"""
        mismatch_sql = eligible_cte + """
                SELECT 1 FROM evidence_text_projection p
                JOIN eligible e ON e.evidence_id=p.evidence_id AND e.version_id=p.version_id
                WHERE e.source_id IS NOT p.source_id OR e.document_id IS NOT p.document_id
                    OR e.kind IS NOT p.kind
                    OR NOT ((p.kind='body' AND p.evidence_class='body_evidence')
                    OR (p.kind='filing' AND p.evidence_class='filing_evidence'))
                LIMIT 1"""
        try:
            mismatch = conn.execute(mismatch_sql, parameters).fetchone()
        except sqlite3.OperationalError:
            raise EvidenceSearchError("evidence projection identity could not be validated") from None
        if mismatch is not None:
            raise EvidenceSearchError("projection identity or evidence class does not match authoritative evidence kind")

        sql = f"""WITH eligible(evidence_id,version_id,source_id,document_id,kind) AS (
                    VALUES {placeholders}
                )
                SELECT p.evidence_id,p.version_id,p.source_id,p.document_id,p.kind,
                       p.title,p.text,p.body_sha256
                FROM evidence_text_fts
                JOIN evidence_text_projection p ON p.rowid=evidence_text_fts.rowid
                JOIN eligible e ON e.evidence_id=p.evidence_id AND e.version_id=p.version_id
                    AND e.source_id=p.source_id AND e.document_id=p.document_id AND e.kind=p.kind
                WHERE evidence_text_fts MATCH ?
                  AND ((p.kind='body' AND p.evidence_class='body_evidence')
                    OR (p.kind='filing' AND p.evidence_class='filing_evidence'))"""
        try:
            rows = conn.execute(sql, (*parameters, query)).fetchall()
        except sqlite3.OperationalError:
            raise EvidenceSearchError("FTS query could not be executed against the evidence index") from None
        for row in rows:
            identity = tuple(row[index] for index in range(5))
            authoritative = eligible[identity]
            if (row[5], row[6], row[7]) != (
                authoritative["title"], authoritative["text"], authoritative["body_sha256"]
            ):
                raise EvidenceSearchError("projection content does not match authoritative evidence version")
            matches.add(identity)
    return matches


def search(
    conn: sqlite3.Connection,
    ticker: str,
    query: str,
    window_start: datetime,
    window_end: datetime,
    cutoff: datetime,
    *,
    mode: str,
    data_snapshot_id: str,
    correction_table_id: str,
    limit: int = 20,
    allowed_legacy_cohorts: list[str] | tuple[str, ...] = (),
    legacy_verified_stores: VerifiedStores | None = None,
    legacy_catalogs: dict[str, str | Path] | None = None,
    correction_tables: tuple[tuple[CorrectionTable, str], ...] = (),
    contract_version: str = "data-core-v1.0",
    dedup_version: str = "same-article-release-v1",
) -> dict[str, Any]:
    """Search eligible body/filing versions with explicit historical identity.

    ``query`` uses SQLite FTS5 MATCH syntax, supplied as a bound parameter.
    Limit is applied only after temporal qualification and exact version/identity
    intersection. Results contain the unchanged authoritative ``as_of.evidence``
    rows, never lead rows or projection-supplied text. No schema is created and
    no data is written. An absent/broken schema fails rather than falling back.
    """
    if not isinstance(query, str) or not query.strip():
        raise EvidenceSearchError("query must be a nonempty FTS expression")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise EvidenceSearchError(f"limit must be an integer between 1 and {MAX_LIMIT}")

    with _read_snapshot(conn):
        _require_readable_index(conn)
        read = temporal_evidence.as_of(
            conn, ticker, window_start, window_end, cutoff,
            mode=mode, kinds=["body", "filing"],
            allowed_legacy_cohorts=allowed_legacy_cohorts,
            legacy_verified_stores=legacy_verified_stores,
            legacy_catalogs=legacy_catalogs,
            correction_tables=correction_tables,
            data_snapshot_id=data_snapshot_id,
            correction_table_id=correction_table_id,
            contract_version=contract_version, dedup_version=dedup_version,
        )
        eligible = {_identity(row): row for row in read["evidence"]}
        matches = _matching_identities(conn, query, eligible)
        results = [eligible[identity] for identity in sorted(matches)]
        # Python's stable sort retains ascending identity order for equal dates.
        results.sort(key=lambda row: row["eligible_at"] or "", reverse=True)
        results = results[:limit]

    audit = dict(read["audit"])
    audit.update({
        "query": query,
        "limit": limit,
        "sort_policy": "eligible_at_desc_identity_asc",
        "ranking": "chronological_not_relevance",
        "result_count": len(results),
        "count_scope": "temporal_read_counts_before_fts; result_count_after_match_and_limit",
    })
    return {"results": results, "audit": audit}


def search_stores(
    stores: VerifiedStores,
    ticker: str,
    query: str,
    window_start: datetime,
    window_end: datetime,
    cutoff: datetime,
    *,
    mode: str,
    correction_table_id: str,
    store_ids: tuple[str, ...] | list[str] | None = None,
    correction_tables: list[CorrectionTable] | tuple[CorrectionTable, ...] = (),
    correction_store_ids: dict[str, str] | None = None,
    correction_artifacts: dict[str, str | Path] | None = None,
    limit: int = 20,
    allowed_legacy_cohorts: list[str] | tuple[str, ...] = (),
    legacy_catalogs: dict[str, str | Path] | None = None,
    contract_version: str = "data-core-v1.0",
    dedup_version: str = "same-article-release-v1",
) -> dict[str, Any]:
    """Search pinned stores, gated by one cross-store temporal read.

    FTS returns candidate identities only. Each candidate must match the exact
    version selected by `as_of_stores`; returned fields come from that read,
    never from a search projection. The verified manifest fixes immutable store paths and actual hashes for
    both temporal selection and search. Each hit resolves within its own store.
    """
    if not isinstance(query, str) or not query.strip():
        raise EvidenceSearchError("query must be a nonempty FTS expression")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise EvidenceSearchError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    try:
        read = as_of_stores(
            stores, ticker, window_start, window_end, cutoff,
            mode=mode, correction_table_id=correction_table_id, store_ids=store_ids,
            correction_tables=correction_tables,
            correction_store_ids=correction_store_ids, correction_artifacts=correction_artifacts,
            allowed_legacy_cohorts=allowed_legacy_cohorts,
            legacy_catalogs=legacy_catalogs,
            contract_version=contract_version, dedup_version=dedup_version,
            kinds=["body", "filing"],
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, EvidenceSearchError):
            raise
        raise EvidenceSearchError("cross-store temporal read failed") from exc

    eligible = {(row["store_id"], _identity(row)): row for row in read["evidence"]}
    matched: set[tuple[str, tuple[str, ...]]] = set()
    try:
        validate_verified_stores(stores)
        for store_id in sorted(read["audit"]["data_snapshot_ids"]):
            snapshot = stores.stores[store_id]
            scoped = {
                identity: row for (scope, identity), row in eligible.items()
                if scope == store_id
            }
            with readonly_snapshot(stores, snapshot) as conn:
                _require_readable_index(conn)
                matched.update(
                    (store_id, identity)
                    for identity in _matching_identities(conn, query, scoped)
                )
        validate_verified_stores(stores)
    except (OSError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, EvidenceSearchError):
            raise
        raise EvidenceSearchError("cross-store FTS projection or snapshot is inconsistent") from exc

    results = [row for identity, row in eligible.items() if identity in matched]
    results.sort(key=lambda row: (row["source_id"], row["document_id"], row["version_id"]))
    results.sort(key=lambda row: row["eligible_at"] or "", reverse=True)
    results = results[:limit]
    audit = dict(read["audit"])
    audit.update({
        "query": query,
        "limit": limit,
        "sort_policy": "eligible_at_desc_identity_asc",
        "ranking": "chronological_not_relevance",
        "result_count": len(results),
        "count_scope": "temporal_read_counts_before_fts; result_count_after_match_and_limit",
    })
    return {"results": results, "audit": audit}


__all__ = ["EvidenceSearchError", "MAX_LIMIT", "search", "search_stores"]
