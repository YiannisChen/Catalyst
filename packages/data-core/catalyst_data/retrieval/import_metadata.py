"""Typed, bounded metadata stream for the production LanceDB importer.

The stream is the single source of row metadata for the production dense
import. It reads only the served corpus relation (never a hardcoded table),
requires the current corpus manifest to match the expected manifest id, reads
in chunk_id order with ``fetchmany`` pages of at most 500, and validates every
identity/hash/ticker field before the row is accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator

from catalyst_data.corpus.streaming_publication import served_chunks_relation

SEARCHABLE_STATUSES = ("active", "pending_embedding", "embedded", "metadata_only")
MAX_PAGE_SIZE = 500
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")

_METADATA_COLUMNS = (
    "chunk_id", "document_id", "content_text", "content_hash", "metadata_hash",
    "available_at", "ticker_associations", "source_class",
    "chunk_profile_version", "status", "eligibility", "dedup_cluster_id",
    "cluster_first_available_at", "representative_document_id",
)


@dataclass(frozen=True)
class ImportMetadataRow:
    """One validated served-corpus row for the dense import pipeline."""

    chunk_id: str
    document_id: str
    content_text: str
    content_hash: str
    metadata_hash: str
    available_at: str
    ticker_associations: tuple[str, ...]
    source_class: str
    chunk_profile_version: str
    status: str
    eligibility: str
    dedup_cluster_id: str | None = None
    cluster_first_available_at: str | None = None
    representative_document_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """LanceDB-ready row payload (ticker list, nullable dedup fields)."""
        return {
            name: (
                list(self.ticker_associations)
                if name == "ticker_associations"
                else getattr(self, name)
            )
            for name in _METADATA_COLUMNS
        }


def _current_manifest_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current = 1"
    ).fetchone()
    if row is None:
        raise ValueError("corpus has no current manifest")
    return str(row[0])


def _parse_ticker_associations(raw: Any) -> tuple[str, ...]:
    if raw is None:
        raise ValueError("ticker_associations must be a JSON array")
    try:
        values = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("ticker_associations must be a JSON array") from exc
    if not isinstance(values, list):
        raise ValueError("ticker_associations must be a JSON array")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("ticker_associations contains an empty ticker")
        normalized.append(value.strip().upper())
    if not normalized:
        raise ValueError("ticker_associations must not be empty")
    return tuple(sorted(set(normalized)))


def _validate_row(raw: Any) -> ImportMetadataRow:
    if not isinstance(raw, (tuple, list)) or len(raw) != len(_METADATA_COLUMNS):
        raise ValueError("metadata row has an unexpected column count")
    values = dict(zip(_METADATA_COLUMNS, raw))

    identity: dict[str, str] = {}
    for name in (
        "chunk_id", "document_id", "available_at", "content_hash",
        "metadata_hash", "source_class", "chunk_profile_version",
        "status", "eligibility",
    ):
        value = values[name]
        if not isinstance(value, str) or not value:
            raise ValueError(f"null or empty identity field: {name}")
        identity[name] = value
    content_text = values["content_text"]
    if not isinstance(content_text, str):
        raise ValueError("content_text must be a string")
    for name in ("content_hash", "metadata_hash"):
        if _HEX64.fullmatch(identity[name]) is None:
            raise ValueError(f"{name} must be lowercase SHA-256")
    if identity["content_hash"] != hashlib.sha256(content_text.encode("utf-8")).hexdigest():
        raise ValueError(f"content_hash mismatch for chunk_id={identity['chunk_id']}")
    if identity["status"] not in SEARCHABLE_STATUSES:
        raise ValueError("status must use the searchable-status contract")
    if identity["eligibility"] != "eligible":
        raise ValueError("eligibility must be 'eligible'")
    tickers = _parse_ticker_associations(values["ticker_associations"])

    def _nullable(name: str) -> str | None:
        value = values[name]
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError(f"invalid nullable field: {name}")
        return value

    return ImportMetadataRow(
        chunk_id=identity["chunk_id"],
        document_id=identity["document_id"],
        content_text=content_text,
        content_hash=identity["content_hash"],
        metadata_hash=identity["metadata_hash"],
        available_at=identity["available_at"],
        ticker_associations=tickers,
        source_class=identity["source_class"],
        chunk_profile_version=identity["chunk_profile_version"],
        status=identity["status"],
        eligibility=identity["eligibility"],
        dedup_cluster_id=_nullable("dedup_cluster_id"),
        cluster_first_available_at=_nullable("cluster_first_available_at"),
        representative_document_id=_nullable("representative_document_id"),
    )


def iter_import_metadata(
    conn: sqlite3.Connection,
    *,
    corpus_manifest_id: str,
    page_size: int = 500,
) -> Iterator[ImportMetadataRow]:
    """Yield validated served-corpus rows in chunk_id order, bounded pages.

    Raises ValueError on manifest drift, malformed rows, hash violations,
    ticker JSON violations, or chunk_id order/duplicate violations.
    """
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    if _HEX64.fullmatch(corpus_manifest_id) is None:
        raise ValueError("corpus_manifest_id must be lowercase SHA-256")
    if _current_manifest_id(conn) != corpus_manifest_id:
        raise ValueError("current corpus manifest does not match expected corpus_manifest_id")

    chunks_relation = served_chunks_relation(conn)
    status_placeholders = ",".join("?" for _ in SEARCHABLE_STATUSES)
    sql = (
        f"SELECT {', '.join(_METADATA_COLUMNS)} FROM {chunks_relation} "
        "WHERE manifest_id = ? "
        f"AND status IN ({status_placeholders}) AND eligibility = 'eligible' "
        "ORDER BY chunk_id ASC"
    )
    cursor = conn.execute(sql, (corpus_manifest_id, *SEARCHABLE_STATUSES))
    previous_id = ""
    while True:
        page = cursor.fetchmany(page_size)
        if not page:
            return
        for raw in page:
            row = _validate_row(raw)
            if previous_id and row.chunk_id <= previous_id:
                raise ValueError("chunk_id not strictly sorted (duplicate or out-of-order)")
            previous_id = row.chunk_id
            yield row


__all__ = ["ImportMetadataRow", "MAX_PAGE_SIZE", "iter_import_metadata"]
