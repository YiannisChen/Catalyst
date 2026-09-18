"""End-to-end evidence coverage across generation boundaries (M8-A).

Classifies every requested document (canonical asset identity) by exactly one
:class:`CoverageState`, so a rebuild cannot silently ship a corpus whose
canonical FULL_TEXT documents never reached the inactive build, the candidate
FTS index, or the served generation.

The audit is read-only and identity-bound: it returns the canonical counts and
a digest of the classified rows. Benchmark expectations may be supplied as a
*separate post-build audit input*; they can never change the classification,
the source selection, or the build/corpus identity.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "v1_1_generation_coverage_v1"
FULL_TEXT = "FULL_TEXT"


class CoverageState(str, Enum):
    ABSENT_FROM_CANONICAL = "ABSENT_FROM_CANONICAL"
    NOT_FULL_TEXT = "NOT_FULL_TEXT"
    OMITTED_FROM_BUILD = "OMITTED_FROM_BUILD"
    CANDIDATE_NOT_FTS_INDEXED = "CANDIDATE_NOT_FTS_INDEXED"
    CANDIDATE_FTS_READY = "CANDIDATE_FTS_READY"
    SERVED_NOT_FTS_INDEXED = "SERVED_NOT_FTS_INDEXED"
    SERVED_FTS_READY = "SERVED_FTS_READY"
    DENSE_READY = "DENSE_READY"


FTS_READY_STATES = frozenset(
    {
        CoverageState.CANDIDATE_FTS_READY,
        CoverageState.SERVED_FTS_READY,
        CoverageState.DENSE_READY,
    }
)
_FTS_READY_VALUES = frozenset(state.value for state in FTS_READY_STATES)


@dataclass(frozen=True)
class CoverageRow:
    document_id: str
    state: CoverageState
    canonical_content_state: str | None
    candidate_chunk_count: int
    candidate_fts_row_count: int
    served_chunk_count: int
    dense_status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "state": self.state.value,
            "canonical_content_state": self.canonical_content_state,
            "candidate_chunk_count": self.candidate_chunk_count,
            "candidate_fts_row_count": self.candidate_fts_row_count,
            "served_chunk_count": self.served_chunk_count,
            "dense_status": self.dense_status,
        }


@dataclass(frozen=True)
class GenerationCoverageReport:
    schema_version: str
    corpus_manifest_id: str
    build_id: str | None
    rows: Mapping[str, CoverageRow]
    counts: Mapping[str, int]
    report_digest: str
    full_text_count: int
    candidate_filing_chunk_count: int
    expected_evidence: Mapping[str, str]

    def state_for(self, document_id: str) -> CoverageState:
        return self.rows[document_id].state

    def expected_evidence_summary(self) -> dict[str, Any]:
        """``19/19``-style counts for the post-seal evidence audit input."""
        unresolved = sorted(
            key for key, value in self.expected_evidence.items() if value == "UNRESOLVED"
        )
        not_ready = sorted(
            key
            for key, value in self.expected_evidence.items()
            if value != "UNRESOLVED" and value not in _FTS_READY_VALUES
        )
        ready = sum(
            1 for value in self.expected_evidence.values() if value in _FTS_READY_VALUES
        )
        return {
            "expected_count": len(self.expected_evidence),
            "full_text_fts_ready": ready,
            "unresolved_ids": unresolved,
            "not_full_text_or_not_indexed_ids": not_ready,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "corpus_manifest_id": self.corpus_manifest_id,
            "build_id": self.build_id,
            "counts": dict(self.counts),
            "full_text_count": self.full_text_count,
            "candidate_filing_chunk_count": self.candidate_filing_chunk_count,
            "rows": {key: row.as_dict() for key, row in sorted(self.rows.items())},
            "expected_evidence": dict(sorted(self.expected_evidence.items())),
            "expected_evidence_summary": self.expected_evidence_summary(),
            "report_digest": self.report_digest,
        }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1", (name,)
        ).fetchone()
        is not None
    )


def _canonical_states(
    conn: sqlite3.Connection, document_ids: Iterable[str]
) -> dict[str, str]:
    ids = tuple(dict.fromkeys(document_ids))
    if not ids:
        return {}
    if not _table_exists(conn, "canonical_assets"):
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT asset_id, content_state FROM canonical_assets "
        f"WHERE asset_id IN ({placeholders})",
        ids,
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _chunks_by_asset(
    conn: sqlite3.Connection, *, build_id: str
) -> dict[str, tuple[str, ...]]:
    rows = conn.execute(
        "SELECT canonical_asset_id, chunk_id FROM corpus_build_chunks "
        "WHERE build_id=? AND canonical_asset_id IS NOT NULL "
        "ORDER BY chunk_id",
        (build_id,),
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for asset_id, chunk_id in rows:
        grouped.setdefault(str(asset_id), []).append(str(chunk_id))
    return {key: tuple(value) for key, value in grouped.items()}


def _candidate_fts_chunk_ids(
    conn: sqlite3.Connection, *, build_id: str
) -> frozenset[str]:
    if not _table_exists(conn, "corpus_build_chunks_fts"):
        return frozenset()
    rows = conn.execute(
        "SELECT chunk_id FROM corpus_build_chunks_fts WHERE build_id=?",
        (build_id,),
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _served_chunks_by_document(
    conn: sqlite3.Connection, *, corpus_manifest_id: str
) -> dict[str, tuple[str, ...]]:
    from catalyst_data.corpus.streaming_publication import served_chunks_relation

    relation = served_chunks_relation(conn)
    rows = conn.execute(
        f"SELECT chunk_id, document_id FROM {relation} WHERE manifest_id=?",
        (corpus_manifest_id,),
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for chunk_id, document_id in rows:
        grouped.setdefault(str(document_id), []).append(str(chunk_id))
    return {key: tuple(value) for key, value in grouped.items()}


def _served_document_to_asset(
    conn: sqlite3.Connection, document_ids: Iterable[str]
) -> dict[str, str]:
    ids = tuple(dict.fromkeys(document_ids))
    if not ids or not _table_exists(conn, "corpus_build_chunks"):
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT DISTINCT corpus_document_id, canonical_asset_id "
        "FROM corpus_build_chunks WHERE corpus_document_id IN "
        f"({placeholders}) AND canonical_asset_id IS NOT NULL",
        ids,
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _served_fts_chunk_ids(conn: sqlite3.Connection) -> frozenset[str]:
    """Served FTS membership across the legacy and per-build FTS tables."""
    chunk_ids: set[str] = set()
    for table in ("corpus_chunks_fts", "corpus_build_chunks_fts"):
        if not _table_exists(conn, table):
            continue
        rows = conn.execute(f"SELECT chunk_id FROM {table}").fetchall()
        chunk_ids.update(str(row[0]) for row in rows)
    return frozenset(chunk_ids)


def _embedded_chunk_ids(conn: sqlite3.Connection) -> frozenset[str]:
    if not _table_exists(conn, "index_state"):
        return frozenset()
    rows = conn.execute(
        "SELECT chunk_id FROM index_state WHERE status='embedded'"
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)



def _evidence_chunk_states(
    conn: sqlite3.Connection,
    *,
    build_id: str | None,
    corpus_manifest_id: str,
    chunk_ids: Iterable[str],
) -> dict[str, tuple[str | None, str | None, str, bool, bool]]:
    """Resolve expected *evidence (chunk) ids* to their build-scoped chunk.

    Returns ``{chunk_id: (document_id, canonical_asset_id, content_state,
    has_body, fts_indexed)}``. The M8-A gate is expressed in chunk ids, so the
    audit must resolve the exact chunk, not only the parent asset identity.
    Chunks that are not present anywhere are simply absent from the mapping.
    """
    ids = tuple(dict.fromkeys(chunk_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    if build_id is not None and _table_exists(conn, "corpus_build_chunks"):
        rows = conn.execute(
            "SELECT chunk_id, corpus_document_id, canonical_asset_id, "
            "content_state, content_text FROM corpus_build_chunks "
            f"WHERE build_id=? AND chunk_id IN ({placeholders})",
            (build_id, *ids),
        ).fetchall()
        fts_ids: frozenset[str] = frozenset()
        if _table_exists(conn, "corpus_build_chunks_fts"):
            fts_ids = frozenset(
                str(row[0])
                for row in conn.execute(
                    "SELECT chunk_id FROM corpus_build_chunks_fts "
                    f"WHERE build_id=? AND chunk_id IN ({placeholders})",
                    (build_id, *ids),
                ).fetchall()
            )
    else:
        from catalyst_data.corpus.streaming_publication import served_chunks_relation

        relation = served_chunks_relation(conn)
        rows = conn.execute(
            "SELECT chunk_id, document_id, NULL, content_state, content_text "
            f"FROM {relation} WHERE manifest_id=? AND chunk_id IN ({placeholders})",
            (corpus_manifest_id, *ids),
        ).fetchall()
        fts_ids = frozenset(
            str(row[0])
            for row in conn.execute(
                "SELECT chunk_id FROM corpus_chunks_fts "
                f"WHERE chunk_id IN ({placeholders})",
                ids,
            ).fetchall()
        )
    resolved: dict[str, tuple[str | None, str | None, str, bool, bool]] = {}
    for row in rows:
        text = row[4]
        resolved[str(row[0])] = (
            str(row[1]) if row[1] is not None else None,
            str(row[2]) if row[2] is not None else None,
            str(row[3] or ""),
            text is not None and bool(str(text).strip()),
            str(row[0]) in fts_ids,
        )
    return resolved


def _classify(
    *,
    canonical_state: str | None,
    candidate_chunk_count: int,
    candidate_fts_row_count: int,
    served_chunk_count: int,
    served_fts_row_count: int,
    dense_present: bool,
) -> CoverageState:
    if canonical_state is None:
        return CoverageState.ABSENT_FROM_CANONICAL
    if canonical_state != FULL_TEXT:
        return CoverageState.NOT_FULL_TEXT
    if candidate_chunk_count > 0:
        if candidate_fts_row_count == 0:
            return CoverageState.CANDIDATE_NOT_FTS_INDEXED
        return (
            CoverageState.DENSE_READY
            if dense_present
            else CoverageState.CANDIDATE_FTS_READY
        )
    if served_chunk_count == 0:
        return CoverageState.OMITTED_FROM_BUILD
    if served_fts_row_count == 0:
        return CoverageState.SERVED_NOT_FTS_INDEXED
    return (
        CoverageState.DENSE_READY if dense_present else CoverageState.SERVED_FTS_READY
    )


def audit_generation_coverage(
    conn: sqlite3.Connection,
    *,
    corpus_manifest_id: str,
    build_id: str | None = None,
    document_ids: Iterable[str] | None = None,
    expected_evidence_ids: Iterable[str] = (),
) -> GenerationCoverageReport:
    """Classify the requested documents across canonical/build/served/FTT/dense."""
    candidate_by_asset: dict[str, tuple[str, ...]] = {}
    candidate_fts: frozenset[str] = frozenset()
    if build_id is not None:
        candidate_by_asset = _chunks_by_asset(conn, build_id=build_id)
        candidate_fts = _candidate_fts_chunk_ids(conn, build_id=build_id)

    # Served coverage is always measured against the CURRENT served manifest;
    # the requested manifest id is the candidate under audit.
    current = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current=1 LIMIT 1"
    ).fetchone()
    served_manifest_id = str(current[0]) if current is not None else corpus_manifest_id
    served_by_document = _served_chunks_by_document(
        conn, corpus_manifest_id=served_manifest_id
    )
    document_to_asset = _served_document_to_asset(conn, served_by_document)
    served_by_asset: dict[str, tuple[str, ...]] = {}
    for document_id, chunk_ids in served_by_document.items():
        asset_id = document_to_asset.get(document_id, document_id)
        served_by_asset[asset_id] = served_by_asset.get(asset_id, ()) + chunk_ids

    served_fts = _served_fts_chunk_ids(conn)
    embedded = _embedded_chunk_ids(conn)

    requested = tuple(dict.fromkeys(document_ids or ()))
    if requested:
        identities = requested
    else:
        identities = tuple(
            dict.fromkeys(
                (
                    *candidate_by_asset.keys(),
                    *served_by_asset.keys(),
                )
            )
        )

    canonical_states = _canonical_states(conn, identities)

    rows: dict[str, CoverageRow] = {}
    counts: dict[str, int] = {state.value: 0 for state in CoverageState}
    for document_id in identities:
        candidate_chunks = candidate_by_asset.get(document_id, ())
        served_chunks = served_by_asset.get(document_id, ())
        candidate_fts_rows = sum(1 for c in candidate_chunks if c in candidate_fts)
        served_fts_rows = sum(1 for c in served_chunks if c in served_fts)
        pool = candidate_chunks or served_chunks
        dense_status = (
            "ABSENT"
            if not pool
            else ("PRESENT" if any(c in embedded for c in pool) else "ABSENT")
        )
        state = _classify(
            canonical_state=canonical_states.get(document_id),
            candidate_chunk_count=len(candidate_chunks),
            candidate_fts_row_count=candidate_fts_rows,
            served_chunk_count=len(served_chunks),
            served_fts_row_count=served_fts_rows,
            dense_present=dense_status == "PRESENT",
        )
        counts[state.value] += 1
        rows[document_id] = CoverageRow(
            document_id=document_id,
            state=state,
            canonical_content_state=canonical_states.get(document_id),
            candidate_chunk_count=len(candidate_chunks),
            candidate_fts_row_count=candidate_fts_rows,
            served_chunk_count=len(served_chunks),
            dense_status=dense_status,
        )

    full_text_count = sum(
        1 for state in canonical_states.values() if state == FULL_TEXT
    )
    filing_chunk_count = 0
    if build_id is not None:
        # Whole-build count: an IN(...) list of every candidate chunk id blows
        # the SQLite variable limit on a real (500k-chunk) candidate.
        filing_chunk_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM corpus_build_chunks WHERE build_id=? "
                "AND chunk_profile_version LIKE 'filing%'",
                (build_id,),
            ).fetchone()[0]
        )

    evidence_ids = tuple(dict.fromkeys(expected_evidence_ids))
    resolved_chunks = _evidence_chunk_states(
        conn,
        build_id=build_id,
        corpus_manifest_id=corpus_manifest_id,
        chunk_ids=evidence_ids,
    )
    resolved_asset_states = _canonical_states(
        conn,
        (asset for _, asset, _, _, _ in resolved_chunks.values() if asset),
    )
    expected: dict[str, str] = {}
    for evidence_id in evidence_ids:
        row = rows.get(evidence_id)
        if row is not None:
            expected[evidence_id] = row.state.value
            continue
        chunk = resolved_chunks.get(evidence_id)
        if chunk is None:
            expected[evidence_id] = "UNRESOLVED"
            continue
        _document_id, asset_id, content_state, has_body, fts_indexed = chunk
        asset_state = resolved_asset_states.get(asset_id) if asset_id else None
        if content_state == FULL_TEXT and has_body:
            expected[evidence_id] = (
                CoverageState.CANDIDATE_FTS_READY.value
                if fts_indexed
                else CoverageState.CANDIDATE_NOT_FTS_INDEXED.value
            )
        elif asset_state is not None and asset_state != FULL_TEXT:
            expected[evidence_id] = CoverageState.NOT_FULL_TEXT.value
        elif build_id is not None:
            expected[evidence_id] = CoverageState.OMITTED_FROM_BUILD.value
        else:
            expected[evidence_id] = CoverageState.SERVED_NOT_FTS_INDEXED.value

    digest_payload = {
        "schema_version": SCHEMA_VERSION,
        "corpus_manifest_id": corpus_manifest_id,
        "build_id": build_id,
        "rows": {key: rows[key].as_dict() for key in sorted(rows)},
        "counts": counts,
    }
    report_digest = hashlib.sha256(
        json.dumps(
            digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return GenerationCoverageReport(
        schema_version=SCHEMA_VERSION,
        corpus_manifest_id=corpus_manifest_id,
        build_id=build_id,
        rows=rows,
        counts=counts,
        report_digest=report_digest,
        full_text_count=full_text_count,
        candidate_filing_chunk_count=filing_chunk_count,
        expected_evidence=expected,
    )


__all__ = [
    "CoverageRow",
    "CoverageState",
    "FTS_READY_STATES",
    "GenerationCoverageReport",
    "SCHEMA_VERSION",
    "audit_generation_coverage",
]
