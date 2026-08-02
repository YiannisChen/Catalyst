"""Bounded, resumable corpus and lexical publication for Pre-B6."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from catalyst_data.corpus.filing_v3 import FilingV3Profile
from catalyst_data.corpus.manifest import INVENTORY_FIELDS
from catalyst_data.corpus.news_v2 import NewsV2Profile
from catalyst_data.corpus.persisted_id import is_valid_persisted_document_id
from catalyst_data.corpus.source_classifier import CLASSIFIER_VERSION, classify
from catalyst_data.corpus.tokenizer import TOKENIZER_MODEL_ID, TOKENIZER_REVISION
from catalyst_data.timeutil import normalize_utc_second_z


DEFAULT_SOURCE_BYTES = 64 * 1024**2
DEFAULT_CHUNK_TEXT_BYTES = 16 * 1024**2
MAX_RSS_BYTES = 6 * 1024**3
SEARCHABLE_STATUSES = ("active", "pending_embedding", "embedded", "metadata_only")


class ResumableResourceStop(RuntimeError):
    """A bounded publication stopped without invalidating committed staging."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.resumable = True
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class PublicationLimits:
    max_documents: int = 100
    source_utf8_bytes: int = DEFAULT_SOURCE_BYTES
    max_chunks: int = 500
    chunk_text_utf8_bytes: int = DEFAULT_CHUNK_TEXT_BYTES

    def __post_init__(self) -> None:
        if not 1 <= self.max_documents <= 100:
            raise ValueError("max_documents must be between 1 and 100")
        if self.source_utf8_bytes < 1:
            raise ValueError("source_utf8_bytes must be positive")
        if not 1 <= self.max_chunks <= 500:
            raise ValueError("max_chunks must be between 1 and 500")
        if self.chunk_text_utf8_bytes < 1:
            raise ValueError("chunk_text_utf8_bytes must be positive")


@dataclass(frozen=True)
class BufferStats:
    peak_documents: int
    peak_source_utf8_bytes: int
    peak_chunks: int
    peak_chunk_text_utf8_bytes: int
    retained_result_items: int = 0


@dataclass(frozen=True)
class StreamingCorpusResult:
    manifest_id: str
    document_count: int
    chunk_count: int
    inventory_digest: str


@dataclass(frozen=True)
class StreamingLexicalResult:
    manifest_id: str
    mode_served: str
    row_count: int
    digest: str


@dataclass(frozen=True)
class ReconciliationSummary:
    to_embed_count: int
    metadata_update_count: int
    tombstone_count: int


@dataclass(frozen=True)
class StreamingPublicationResult:
    build_id: str
    corpus: StreamingCorpusResult
    lexical: StreamingLexicalResult
    reconciliation: ReconciliationSummary
    buffer_stats: BufferStats


@dataclass(frozen=True)
class StreamingResourceEstimate:
    source_utf8_bytes: int
    eligible_document_count: int
    estimated_chunks: int
    largest_source_document_utf8_bytes: int
    phase_headroom_bytes: dict[str, int]
    estimated_peak_bytes: int
    required_headroom: int


@dataclass
class _MutableBufferStats:
    peak_documents: int = 0
    peak_source_utf8_bytes: int = 0
    peak_chunks: int = 0
    peak_chunk_text_utf8_bytes: int = 0

    def frozen(self) -> BufferStats:
        return BufferStats(
            peak_documents=self.peak_documents,
            peak_source_utf8_bytes=self.peak_source_utf8_bytes,
            peak_chunks=self.peak_chunks,
            peak_chunk_text_utf8_bytes=self.peak_chunk_text_utf8_bytes,
        )


@dataclass(frozen=True)
class _SourceDocument:
    source_kind: str
    source_key: str
    document_id: str
    source_utf8_bytes: int
    document: dict[str, Any]
    provider: str
    source_type: str
    eligibility: str


_BUILD_SCHEMA = """
CREATE TABLE IF NOT EXISTS corpus_publication_builds (
    build_id TEXT PRIMARY KEY,
    certified_snapshot_identity TEXT NOT NULL,
    header_json TEXT NOT NULL CHECK (json_valid(header_json)),
    status TEXT NOT NULL,
    manifest_id TEXT,
    manifest_json TEXT CHECK (manifest_json IS NULL OR json_valid(manifest_json)),
    document_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    source_utf8_bytes INTEGER NOT NULL DEFAULT 0,
    inventory_digest TEXT,
    lexical_expected_digest TEXT,
    lexical_digest TEXT,
    lexical_row_count INTEGER NOT NULL DEFAULT 0,
    lexical_repair_checkpoint TEXT,
    lexical_repair_cursor TEXT,
    reconciliation_ready INTEGER NOT NULL DEFAULT 0,
    lexical_ready INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS corpus_build_documents (
    build_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_utf8_bytes INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress','complete')),
    chunk_count INTEGER NOT NULL DEFAULT 0,
    chunk_text_utf8_bytes INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (build_id, source_kind, source_key),
    FOREIGN KEY (build_id) REFERENCES corpus_publication_builds(build_id)
);

CREATE TABLE IF NOT EXISTS corpus_build_source_documents (
    build_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
    document_id TEXT NOT NULL,
    eligibility TEXT NOT NULL CHECK (eligibility IN ('eligible','ineligible')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (build_id, source_kind, source_key),
    FOREIGN KEY (build_id) REFERENCES corpus_publication_builds(build_id)
);
CREATE INDEX IF NOT EXISTS idx_corpus_build_source_documents_identity
ON corpus_build_source_documents(build_id, document_id, eligibility);

CREATE TABLE IF NOT EXISTS corpus_build_chunks (
    build_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    chunk_profile_version TEXT NOT NULL,
    section_key TEXT NOT NULL,
    ordinal TEXT NOT NULL,
    content_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    source_class TEXT NOT NULL,
    dedup_cluster_id TEXT,
    cluster_first_available_at TEXT,
    representative_document_id TEXT,
    available_at TEXT NOT NULL,
    ticker_associations TEXT NOT NULL CHECK (json_valid(ticker_associations)),
    eligibility TEXT NOT NULL,
    status TEXT NOT NULL,
    boundary_kind TEXT NOT NULL,
    body_token_start INTEGER NOT NULL,
    body_token_end INTEGER NOT NULL,
    body_overlap_tokens INTEGER NOT NULL,
    prefix_token_count INTEGER NOT NULL,
    prefix_truncated INTEGER NOT NULL,
    section_parse_degraded INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    provider TEXT,
    source_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (build_id, chunk_id),
    UNIQUE (build_id, document_id, chunk_profile_version, section_key, ordinal),
    FOREIGN KEY (build_id) REFERENCES corpus_publication_builds(build_id)
);
CREATE INDEX IF NOT EXISTS idx_corpus_build_chunks_order
ON corpus_build_chunks(build_id, chunk_id COLLATE BINARY);
CREATE INDEX IF NOT EXISTS idx_corpus_build_chunks_document
ON corpus_build_chunks(build_id, document_id);

CREATE TRIGGER IF NOT EXISTS trg_corpus_build_chunks_frozen_update
BEFORE UPDATE ON corpus_build_chunks
WHEN (SELECT status FROM corpus_publication_builds WHERE build_id=OLD.build_id)
     IN ('reconciliation_ready','lexical_ready','published')
BEGIN
    SELECT RAISE(ABORT, 'corpus_build_chunks_frozen');
END;
CREATE TRIGGER IF NOT EXISTS trg_corpus_build_chunks_frozen_delete
BEFORE DELETE ON corpus_build_chunks
WHEN (SELECT status FROM corpus_publication_builds WHERE build_id=OLD.build_id)
     IN ('reconciliation_ready','lexical_ready','published')
BEGIN
    SELECT RAISE(ABORT, 'corpus_build_chunks_frozen');
END;
CREATE TRIGGER IF NOT EXISTS trg_corpus_build_chunks_frozen_insert
BEFORE INSERT ON corpus_build_chunks
WHEN (SELECT status FROM corpus_publication_builds WHERE build_id=NEW.build_id)
     IN ('reconciliation_ready','lexical_ready','published')
BEGIN
    SELECT RAISE(ABORT, 'corpus_build_chunks_frozen');
END;

CREATE TABLE IF NOT EXISTS corpus_build_deltas (
    build_id TEXT NOT NULL,
    delta_kind TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    reason TEXT,
    previous_content_hash TEXT,
    previous_metadata_hash TEXT,
    replacement_chunk_id TEXT,
    PRIMARY KEY (build_id, delta_kind, chunk_id),
    FOREIGN KEY (build_id) REFERENCES corpus_publication_builds(build_id)
);
CREATE INDEX IF NOT EXISTS idx_corpus_build_deltas_page
ON corpus_build_deltas(build_id, delta_kind, chunk_id COLLATE BINARY);

CREATE TABLE IF NOT EXISTS corpus_build_fts_batches (
    build_id TEXT NOT NULL,
    batch_no INTEGER NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count BETWEEN 1 AND 500),
    text_utf8_bytes INTEGER NOT NULL,
    first_chunk_id TEXT NOT NULL,
    last_chunk_id TEXT NOT NULL,
    batch_digest TEXT NOT NULL,
    checkpoint_chunk_id TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (build_id, batch_no),
    FOREIGN KEY (build_id) REFERENCES corpus_publication_builds(build_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS corpus_build_chunks_fts USING fts5(
    build_id UNINDEXED,
    chunk_id UNINDEXED,
    content_text,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


_SERVED_COLUMNS = """
chunk_id, document_id, chunk_profile_version, section_key, ordinal,
content_text, content_hash, metadata_hash, source_class, dedup_cluster_id,
cluster_first_available_at, representative_document_id, available_at,
ticker_associations, eligibility, manifest_id, status, boundary_kind,
body_token_start, body_token_end, body_overlap_tokens, prefix_token_count,
prefix_truncated, section_parse_degraded, created_at, updated_at
"""


def ensure_streaming_publication_schema(conn: sqlite3.Connection) -> None:
    """Create derived publication state without changing source schema version."""
    conn.executescript(_BUILD_SCHEMA)
    fts_batch_columns = _table_columns(conn, "corpus_build_fts_batches")
    if "text_utf8_bytes" not in fts_batch_columns:
        conn.execute(
            "ALTER TABLE corpus_build_fts_batches "
            "ADD COLUMN text_utf8_bytes INTEGER NOT NULL DEFAULT 0"
        )
    if "batch_digest" not in fts_batch_columns:
        conn.execute(
            "ALTER TABLE corpus_build_fts_batches "
            "ADD COLUMN batch_digest TEXT NOT NULL DEFAULT ''"
        )
    if "checkpoint_chunk_id" not in fts_batch_columns:
        conn.execute(
            "ALTER TABLE corpus_build_fts_batches "
            "ADD COLUMN checkpoint_chunk_id TEXT NOT NULL DEFAULT ''"
        )
    lexical_columns = _table_columns(conn, "lexical_index_state")
    if "lexical_generation_id" not in lexical_columns:
        conn.execute("ALTER TABLE lexical_index_state ADD COLUMN lexical_generation_id TEXT")
    if "lexical_digest" not in lexical_columns:
        conn.execute("ALTER TABLE lexical_index_state ADD COLUMN lexical_digest TEXT")
    build_columns = _table_columns(conn, "corpus_publication_builds")
    if "lexical_repair_checkpoint" not in build_columns:
        conn.execute(
            "ALTER TABLE corpus_publication_builds "
            "ADD COLUMN lexical_repair_checkpoint TEXT"
        )
    if "lexical_repair_cursor" not in build_columns:
        conn.execute(
            "ALTER TABLE corpus_publication_builds "
            "ADD COLUMN lexical_repair_cursor TEXT"
        )
    conn.execute("DROP VIEW IF EXISTS corpus_served_chunks")
    conn.execute(
        f"""CREATE VIEW corpus_served_chunks AS
            SELECT c.chunk_id, c.document_id, c.chunk_profile_version,
                   c.section_key, c.ordinal, c.content_text, c.content_hash,
                   c.metadata_hash, c.source_class, c.dedup_cluster_id,
                   c.cluster_first_available_at, c.representative_document_id,
                   c.available_at, c.ticker_associations, c.eligibility,
                   b.manifest_id AS manifest_id, c.status, c.boundary_kind,
                   c.body_token_start, c.body_token_end, c.body_overlap_tokens,
                   c.prefix_token_count, c.prefix_truncated,
                   c.section_parse_degraded, c.created_at, c.updated_at
            FROM corpus_build_chunks c
            JOIN corpus_publication_builds b ON b.build_id=c.build_id
            JOIN corpus_manifest m ON m.manifest_id=b.manifest_id AND m.is_current=1
            WHERE b.status='published'
            UNION ALL
            SELECT {_SERVED_COLUMNS}
            FROM corpus_chunks legacy
            WHERE EXISTS (
                SELECT 1 FROM corpus_manifest m
                WHERE m.manifest_id=legacy.manifest_id AND m.is_current=1
            )
              AND NOT EXISTS (
                SELECT 1 FROM corpus_publication_builds b
                JOIN corpus_manifest m
                  ON m.manifest_id=b.manifest_id AND m.is_current=1
                WHERE b.status='published'
            )"""
    )
    conn.commit()


def served_chunks_relation(conn: sqlite3.Connection) -> str:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='corpus_served_chunks'"
    ).fetchone()
    return "corpus_served_chunks" if exists else "corpus_chunks"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _identity_values(header: Mapping[str, object]) -> dict[str, object]:
    return {
        "normalization_version": header["normalization_version"],
        "chunk_profile_versions": header["chunk_profile_versions"],
        "source_classifier_version": header["source_classifier_version"],
        "certified_snapshot_identity": header["certified_snapshot_identity"],
        "tokenizer_revision": header["tokenizer_revision"],
        "embedding_revision_or_null": header.get("embedding_revision"),
    }


def _update_streamed_identity(
    digest: Any,
    header: Mapping[str, object],
    rows: Iterable[Mapping[str, object]],
) -> None:
    values = _identity_values(header)
    keys = sorted((*values.keys(), "sorted_active_chunk_inventory"))
    digest.update(b"{")
    first_key = True
    for key in keys:
        if not first_key:
            digest.update(b",")
        first_key = False
        digest.update(_canonical_bytes(key))
        digest.update(b":")
        if key != "sorted_active_chunk_inventory":
            digest.update(_canonical_bytes(values[key]))
            continue
        digest.update(b"[")
        first_row = True
        for row in rows:
            missing = [field for field in INVENTORY_FIELDS if field not in row]
            if missing:
                raise ValueError(f"active inventory missing fields: {', '.join(missing)}")
            if not first_row:
                digest.update(b",")
            first_row = False
            digest.update(_canonical_bytes({field: row[field] for field in INVENTORY_FIELDS}))
        digest.update(b"]")
    digest.update(b"}")


def compute_streaming_manifest_id(
    header: Mapping[str, object],
    rows: Iterable[Mapping[str, object]],
) -> str:
    digest = hashlib.sha256()
    _update_streamed_identity(digest, header, rows)
    return digest.hexdigest()


def export_legacy_manifest(
    target: str | Path | TextIO,
    *,
    header: Mapping[str, object],
    inventory_rows: Iterable[Mapping[str, object]],
    created_at: str,
) -> None:
    """Stream the historical full-inventory manifest shape in binary key order."""
    with tempfile.TemporaryDirectory(prefix="catalyst-manifest-") as directory:
        spool = sqlite3.connect(Path(directory) / "inventory.db")
        spool.execute("CREATE TABLE rows (chunk_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        for row in inventory_rows:
            missing = [field for field in INVENTORY_FIELDS if field not in row]
            if missing:
                raise ValueError(f"active inventory missing fields: {', '.join(missing)}")
            payload = {field: row[field] for field in INVENTORY_FIELDS}
            spool.execute(
                "INSERT INTO rows(chunk_id, payload) VALUES (?, ?)",
                (str(row["chunk_id"]), _canonical_bytes(payload).decode("utf-8")),
            )
        spool.commit()

        should_close = not hasattr(target, "write")
        output = Path(target).open("w", encoding="utf-8") if should_close else target
        assert hasattr(output, "write")
        try:
            output.write("{")
            fields = (
                ("normalization_version", header["normalization_version"]),
                ("chunk_profile_versions", header["chunk_profile_versions"]),
                ("source_classifier_version", header["source_classifier_version"]),
                ("certified_snapshot_identity", header["certified_snapshot_identity"]),
            )
            first = True
            for key, value in fields:
                if not first:
                    output.write(",")
                first = False
                output.write(_canonical_bytes(key).decode("utf-8"))
                output.write(":")
                output.write(_canonical_bytes(value).decode("utf-8"))
            output.write(',"sorted_active_chunk_inventory":[')
            row_cursor = spool.execute(
                "SELECT payload FROM rows ORDER BY chunk_id COLLATE BINARY"
            )
            first_row = True
            for (payload,) in row_cursor:
                if not first_row:
                    output.write(",")
                first_row = False
                output.write(payload)
            tail = (
                ("tokenizer_revision", header["tokenizer_revision"]),
                ("tokenizer_model_id", header.get("tokenizer_model_id", TOKENIZER_MODEL_ID)),
                ("embedding_revision", header.get("embedding_revision")),
                ("created_at", created_at),
            )
            output.write("]")
            for key, value in tail:
                output.write(",")
                output.write(_canonical_bytes(key).decode("utf-8"))
                output.write(":")
                output.write(_canonical_bytes(value).decode("utf-8"))
            output.write("}")
        finally:
            if should_close:
                output.close()
            spool.close()


def _header(certified_snapshot_identity: str, normalization_version: str) -> dict[str, object]:
    return {
        "normalization_version": normalization_version,
        "chunk_profile_versions": {"news": "news_v2", "filing": "filing_v3"},
        "source_classifier_version": CLASSIFIER_VERSION,
        "certified_snapshot_identity": certified_snapshot_identity,
        "tokenizer_revision": TOKENIZER_REVISION,
        "tokenizer_model_id": TOKENIZER_MODEL_ID,
        "embedding_revision": None,
    }


def _build_id(header: Mapping[str, object]) -> str:
    return hashlib.sha256(
        _canonical_bytes({"schema_version": "corpus_streaming_build_v1", **dict(header)})
    ).hexdigest()


def _current_rss_bytes() -> int:
    try:
        import psutil

        return int(psutil.Process().memory_info().rss)
    except ImportError:
        statm = Path("/proc/self/statm")
        if statm.exists():
            resident_pages = int(statm.read_text().split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        output = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], text=True
        )
        return int(output.strip()) * 1024


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _article_text(title: object, description: object) -> str:
    title_text = str(title or "")
    description_text = str(description or "")
    return f"{title_text}\n{description_text}" if description_text else title_text


def _iter_article_documents(
    conn: sqlite3.Connection, *, page_size: int
) -> Iterator[_SourceDocument]:
    columns = _table_columns(conn, "articles")
    provider = "a.provider" if "provider" in columns else "COALESCE(a.source, 'polygon')"
    source_type = "a.source_type" if "source_type" in columns else "'polygon_news'"
    publisher = "a.publisher_name" if "publisher_name" in columns else "a.publisher"
    dedup_group = "a.dedup_group_id" if "dedup_group_id" in columns else "NULL"
    canonical = "a.is_canonical" if "is_canonical" in columns else "1"
    rag_eligible = "a.is_rag_eligible" if "is_rag_eligible" in columns else "1"
    last_key = ""
    while True:
        cursor = conn.execute(
            f"""SELECT a.article_id, {provider}, {source_type}, a.published_utc,
                       a.title, a.description, a.article_url, {publisher},
                       {dedup_group}, a.source_class, a.dedup_cluster_id,
                       a.cluster_first_available_at, a.representative_document_id,
                       (SELECT GROUP_CONCAT(ticker, ',') FROM (
                            SELECT ticker FROM article_tickers at
                            WHERE at.article_id=a.article_id ORDER BY ticker
                       )) AS tickers_csv, {canonical}, {rag_eligible}
                FROM articles a
                WHERE a.article_id > ? COLLATE BINARY
                ORDER BY a.article_id COLLATE BINARY LIMIT ?""",
            (last_key, page_size),
        )
        seen = 0
        while True:
            row = cursor.fetchone()
            if row is None:
                break
            seen += 1
            last_key = str(row[0])
            eligibility = "eligible" if bool(row[14]) and bool(row[15]) else "ineligible"
            document: dict[str, Any] = {
                "document_id": row[0],
                "eligibility": eligibility,
            }
            source_bytes = 0
            if eligibility == "eligible":
                tickers = sorted({item for item in (row[13] or "").split(",") if item})
                available_at = normalize_utc_second_z(row[3])
                first_available_at = normalize_utc_second_z(row[11] or row[3])
                source_class = row[9] or classify(
                    row[2], article_url=row[6], publisher=row[7]
                )
                cluster_id = row[10] or row[8]
                representative = row[12] or row[0]
                document.update({
                    "title": row[4],
                    "description": row[5],
                    "available_at": available_at,
                    "ticker_associations": json.dumps(tickers, separators=(",", ":")),
                    "source_class": source_class,
                    "dedup_cluster_id": cluster_id,
                    "cluster_first_available_at": first_available_at,
                    "representative_document_id": representative,
                })
                source_bytes = len(_article_text(row[4], row[5]).encode("utf-8"))
            yield _SourceDocument(
                source_kind="article",
                source_key=last_key,
                document_id=str(row[0]),
                source_utf8_bytes=source_bytes,
                document=document,
                provider=str(row[1] or ""),
                source_type=str(row[2] or ""),
                eligibility=eligibility,
            )
        if seen < page_size:
            break


def _iter_filing_documents(
    conn: sqlite3.Connection, *, page_size: int
) -> Iterator[_SourceDocument]:
    if not _table_exists(conn, "filings") or not _table_exists(conn, "filing_documents"):
        return
    last_filing = ""
    last_type = ""
    last_url = ""
    while True:
        cursor = conn.execute(
            """SELECT f.filing_id, f.form_type, f.filed_at, f.ticker,
                      f.dedup_group_id, fd.document_url, fd.document_type,
                      fd.text, fd.document_id, f.is_canonical, f.is_rag_eligible,
                      fd.extraction_status
               FROM filings f
               JOIN filing_documents fd ON fd.filing_id=f.filing_id
               WHERE (f.filing_id, COALESCE(fd.document_type,''), fd.document_url)
                     > (?, ?, ?)
               ORDER BY f.filing_id COLLATE BINARY,
                        COALESCE(fd.document_type,'') COLLATE BINARY,
                        fd.document_url COLLATE BINARY
               LIMIT ?""",
            (last_filing, last_type, last_url, page_size),
        )
        seen = 0
        while True:
            row = cursor.fetchone()
            if row is None:
                break
            seen += 1
            last_filing, last_type, last_url = str(row[0]), str(row[6] or ""), str(row[5])
            document_id = str(row[8] or "")
            eligibility = (
                "eligible"
                if bool(row[9])
                and bool(row[10])
                and row[11] == "success"
                and bool(str(row[7] or "").strip())
                and is_valid_persisted_document_id(document_id)
                else "ineligible"
            )
            document_type = str(row[6] or "primary_doc")
            is_exhibit = document_type.lower().startswith("exhibit_99")
            declared_type = (
                document_type.lower().replace("exhibit_", "EX-").replace("_", ".")
                if is_exhibit
                else str(row[1])
            )
            document: dict[str, Any] = {
                "document_id": document_id,
                "eligibility": eligibility,
            }
            source_bytes = 0
            if eligibility == "eligible":
                available_at = normalize_utc_second_z(row[2])
                document.update({
                    "filing_type": declared_type,
                    "document_role": document_type,
                    "raw_text": row[7],
                    "available_at": available_at,
                    "ticker_associations": json.dumps([row[3]], separators=(",", ":")),
                    "source_class": "official_government",
                    "dedup_cluster_id": row[4],
                    "cluster_first_available_at": available_at,
                    "representative_document_id": document_id,
                })
                source_bytes = len(str(row[7] or "").encode("utf-8"))
            yield _SourceDocument(
                source_kind="filing",
                source_key="\x1f".join((last_filing, last_type, last_url)),
                document_id=document_id,
                source_utf8_bytes=source_bytes,
                document=document,
                provider="sec",
                source_type="sec_filing",
                eligibility=eligibility,
            )
        if seen < page_size:
            break


def _iter_source_documents(
    conn: sqlite3.Connection, *, page_size: int
) -> Iterator[_SourceDocument]:
    if _table_exists(conn, "articles"):
        yield from _iter_article_documents(conn, page_size=page_size)
    yield from _iter_filing_documents(conn, page_size=page_size)


def _chunk_values(
    build_id: str,
    source: _SourceDocument,
    chunk: Any,
    now: str,
) -> tuple[object, ...]:
    document = source.document
    return (
        build_id,
        chunk.chunk_id,
        chunk.document_id,
        chunk.chunk_profile_version,
        chunk.section_key,
        chunk.ordinal,
        chunk.content_text,
        chunk.content_hash,
        chunk.metadata_hash,
        chunk.source_class,
        document.get("dedup_cluster_id"),
        document.get("cluster_first_available_at"),
        document.get("representative_document_id"),
        chunk.available_at,
        chunk.ticker_associations,
        chunk.eligibility,
        "active",
        chunk.boundary_kind,
        chunk.body_token_start,
        chunk.body_token_end,
        chunk.body_overlap_tokens,
        chunk.prefix_token_count,
        int(chunk.prefix_truncated),
        int(chunk.section_parse_degraded),
        source.source_kind,
        source.provider,
        source.source_type,
        now,
        now,
    )


_INSERT_CHUNK_SQL = """INSERT INTO corpus_build_chunks (
    build_id, chunk_id, document_id, chunk_profile_version, section_key,
    ordinal, content_text, content_hash, metadata_hash, source_class,
    dedup_cluster_id, cluster_first_available_at, representative_document_id,
    available_at, ticker_associations, eligibility, status, boundary_kind,
    body_token_start, body_token_end, body_overlap_tokens, prefix_token_count,
    prefix_truncated, section_parse_degraded, source_kind, provider, source_type,
    created_at, updated_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def _write_chunk_batch(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    source: _SourceDocument,
    rows: list[tuple[object, ...]],
    chunk_count: int,
    chunk_text_bytes: int,
    complete: bool,
    now: str,
) -> None:
    with conn:
        if rows:
            conn.executemany(_INSERT_CHUNK_SQL, rows)
        conn.execute(
            """UPDATE corpus_build_documents
               SET status=?, chunk_count=?, chunk_text_utf8_bytes=?, updated_at=?
               WHERE build_id=? AND source_kind=? AND source_key=?""",
            (
                "complete" if complete else "in_progress",
                chunk_count,
                chunk_text_bytes,
                now,
                build_id,
                source.source_kind,
                source.source_key,
            ),
        )


def _stage_document(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    source: _SourceDocument,
    limits: PublicationLimits,
    stats: _MutableBufferStats,
    now: str,
    failure_injector: Callable[[str], None] | None,
) -> tuple[int, int]:
    complete = conn.execute(
        """SELECT status, chunk_count, chunk_text_utf8_bytes
           FROM corpus_build_documents
           WHERE build_id=? AND source_kind=? AND source_key=?""",
        (build_id, source.source_kind, source.source_key),
    ).fetchone()
    if complete is not None and complete[0] == "complete":
        return int(complete[1]), int(complete[2])
    if source.source_utf8_bytes > limits.source_utf8_bytes:
        raise ResumableResourceStop(
            "source_document_too_large",
            f"{source.source_kind}:{source.source_key} requires "
            f"{source.source_utf8_bytes} UTF-8 bytes; budget is {limits.source_utf8_bytes}",
        )

    stats.peak_documents = max(stats.peak_documents, 1)
    stats.peak_source_utf8_bytes = max(
        stats.peak_source_utf8_bytes, source.source_utf8_bytes
    )
    with conn:
        conn.execute(
            "DELETE FROM corpus_build_chunks WHERE build_id=? AND document_id=?",
            (build_id, source.document_id),
        )
        conn.execute(
            """INSERT INTO corpus_build_documents
               (build_id, source_kind, source_key, document_id, source_utf8_bytes,
                status, chunk_count, chunk_text_utf8_bytes, updated_at)
               VALUES (?, ?, ?, ?, ?, 'in_progress', 0, 0, ?)
               ON CONFLICT(build_id, source_kind, source_key) DO UPDATE SET
                   document_id=excluded.document_id,
                   source_utf8_bytes=excluded.source_utf8_bytes,
                   status='in_progress', chunk_count=0,
                   chunk_text_utf8_bytes=0, updated_at=excluded.updated_at""",
            (
                build_id,
                source.source_kind,
                source.source_key,
                source.document_id,
                source.source_utf8_bytes,
                now,
            ),
        )

    profile = NewsV2Profile() if source.source_kind == "article" else FilingV3Profile()
    chunk_batch: list[tuple[object, ...]] = []
    batch_text_bytes = 0
    total_chunks = 0
    total_text_bytes = 0
    for chunk in profile.iter_chunks(source.document):
        text_bytes = len(chunk.content_text.encode("utf-8"))
        if text_bytes > limits.chunk_text_utf8_bytes:
            raise ResumableResourceStop(
                "chunk_text_too_large",
                f"{chunk.chunk_id} requires {text_bytes} UTF-8 bytes; "
                f"budget is {limits.chunk_text_utf8_bytes}",
            )
        if chunk_batch and (
            len(chunk_batch) >= limits.max_chunks
            or batch_text_bytes + text_bytes > limits.chunk_text_utf8_bytes
        ):
            _write_chunk_batch(
                conn,
                build_id=build_id,
                source=source,
                rows=chunk_batch,
                chunk_count=total_chunks,
                chunk_text_bytes=total_text_bytes,
                complete=False,
                now=now,
            )
            if failure_injector is not None:
                failure_injector("after_staging_batch")
            chunk_batch.clear()
            batch_text_bytes = 0
        chunk_batch.append(_chunk_values(build_id, source, chunk, now))
        batch_text_bytes += text_bytes
        total_chunks += 1
        total_text_bytes += text_bytes
        stats.peak_chunks = max(stats.peak_chunks, len(chunk_batch))
        stats.peak_chunk_text_utf8_bytes = max(
            stats.peak_chunk_text_utf8_bytes, batch_text_bytes
        )
    _write_chunk_batch(
        conn,
        build_id=build_id,
        source=source,
        rows=chunk_batch,
        chunk_count=total_chunks,
        chunk_text_bytes=total_text_bytes,
        complete=True,
        now=now,
    )
    if failure_injector is not None:
        failure_injector("after_staging_batch")
    return total_chunks, total_text_bytes


def _stage_source_presence(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    source: _SourceDocument,
    now: str,
) -> None:
    with conn:
        conn.execute(
            """INSERT INTO corpus_build_source_documents
               (build_id, source_kind, source_key, document_id, eligibility, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(build_id, source_kind, source_key) DO UPDATE SET
                   document_id=excluded.document_id,
                   eligibility=excluded.eligibility,
                   updated_at=excluded.updated_at""",
            (
                build_id,
                source.source_kind,
                source.source_key,
                source.document_id,
                source.eligibility,
                now,
            ),
        )


class _Progress:
    def __init__(
        self,
        callback: Callable[[dict[str, Any]], None] | None,
        interval: float,
        monotonic: Callable[[], float],
        rss_reader: Callable[[], int],
    ) -> None:
        self.callback = callback
        self.interval = interval
        self.monotonic = monotonic
        self.rss_reader = rss_reader
        self.started = monotonic()
        self.last = self.started

    def emit(
        self,
        *,
        phase: str,
        documents: int,
        chunks: int,
        source_bytes: int,
        chunk_bytes: int,
        final: bool = False,
    ) -> None:
        if self.callback is None:
            return
        now = self.monotonic()
        if not final and documents % 100 != 0 and now - self.last < self.interval:
            return
        self.callback(
            {
                "phase": phase,
                "documents": documents,
                "chunks": chunks,
                "source_utf8_bytes": source_bytes,
                "chunk_text_utf8_bytes": chunk_bytes,
                "rss_bytes": self.rss_reader(),
                "elapsed_seconds": max(0.0, now - self.started),
            }
        )
        self.last = now


def _iter_build_inventory_rows(
    conn: sqlite3.Connection, build_id: str
) -> Iterator[dict[str, object]]:
    select_fields = ", ".join(INVENTORY_FIELDS)
    cursor = conn.execute(
        f"""SELECT {select_fields} FROM corpus_build_chunks
            WHERE build_id=? AND eligibility='eligible'
            ORDER BY chunk_id COLLATE BINARY""",
        (build_id,),
    )
    for row in cursor:
        yield dict(zip(INVENTORY_FIELDS, row))


def _inventory_pass_one(conn: sqlite3.Connection, build_id: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    previous: str | None = None
    for row in _iter_build_inventory_rows(conn, build_id):
        chunk_id = str(row["chunk_id"])
        if previous is not None and chunk_id.encode("utf-8") <= previous.encode("utf-8"):
            raise RuntimeError("inventory is not in strict binary chunk_id order")
        previous = chunk_id
        digest.update(_canonical_bytes(row))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _manifest_phase(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    header: Mapping[str, object],
    now: str,
) -> tuple[str, int, str]:
    chunk_count, inventory_digest = _inventory_pass_one(conn, build_id)
    manifest_id = compute_streaming_manifest_id(
        header, _iter_build_inventory_rows(conn, build_id)
    )
    document_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM corpus_build_documents WHERE build_id=? AND status='complete'",
            (build_id,),
        ).fetchone()[0]
    )
    manifest_header = {
        **dict(header),
        "created_at": now,
        "inventory_storage": "corpus_build_chunks",
        "inventory_build_id": build_id,
        "inventory_row_count": chunk_count,
        "inventory_digest": inventory_digest,
    }
    manifest_json = _canonical_bytes(manifest_header).decode("utf-8")
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO corpus_manifest
               (manifest_id, manifest_json, is_current, created_at)
               VALUES (?, ?, 0, ?)""",
            (manifest_id, manifest_json, now),
        )
        conn.execute(
            """UPDATE corpus_manifest SET manifest_json=?
               WHERE manifest_id=? AND is_current=0""",
            (manifest_json, manifest_id),
        )
        conn.execute(
            """UPDATE corpus_publication_builds
               SET status='manifest_ready', manifest_id=?, manifest_json=?,
                   document_count=?, chunk_count=?, inventory_digest=?, updated_at=?
               WHERE build_id=?""",
            (
                manifest_id,
                manifest_json,
                document_count,
                chunk_count,
                inventory_digest,
                now,
                build_id,
            ),
        )
    return manifest_id, chunk_count, inventory_digest


def _reconciliation_phase(
    conn: sqlite3.Connection, *, build_id: str, now: str
) -> ReconciliationSummary:
    previous = served_chunks_relation(conn)
    with conn:
        conn.execute("DELETE FROM corpus_build_deltas WHERE build_id=?", (build_id,))
        conn.execute(
            f"""INSERT INTO corpus_build_deltas
                (build_id, delta_kind, chunk_id, document_id,
                 previous_content_hash, previous_metadata_hash)
                SELECT ?, 'to_embed', n.chunk_id, n.document_id,
                       p.content_hash, p.metadata_hash
                FROM corpus_build_chunks n
                LEFT JOIN {previous} p ON p.chunk_id=n.chunk_id
                WHERE n.build_id=? AND n.eligibility='eligible'
                  AND (p.chunk_id IS NULL OR p.content_hash != n.content_hash
                       OR p.dedup_cluster_id IS NOT n.dedup_cluster_id)""",
            (build_id, build_id),
        )
        conn.execute(
            f"""INSERT INTO corpus_build_deltas
                (build_id, delta_kind, chunk_id, document_id, reason,
                 previous_content_hash, previous_metadata_hash)
                SELECT ?, 'tombstone', n.chunk_id, n.document_id,
                       'dedup_cluster_reassigned', p.content_hash, p.metadata_hash
                FROM corpus_build_chunks n
                JOIN {previous} p ON p.chunk_id=n.chunk_id
                WHERE n.build_id=? AND n.eligibility='eligible'
                  AND p.dedup_cluster_id IS NOT n.dedup_cluster_id""",
            (build_id, build_id),
        )
        conn.execute(
            f"""INSERT INTO corpus_build_deltas
                (build_id, delta_kind, chunk_id, document_id,
                 previous_content_hash, previous_metadata_hash)
                SELECT ?, 'metadata_update', n.chunk_id, n.document_id,
                       p.content_hash, p.metadata_hash
                FROM corpus_build_chunks n
                JOIN {previous} p ON p.chunk_id=n.chunk_id
                WHERE n.build_id=? AND n.eligibility='eligible'
                  AND p.content_hash=n.content_hash
                  AND p.metadata_hash != n.metadata_hash
                  AND p.dedup_cluster_id IS n.dedup_cluster_id""",
            (build_id, build_id),
        )
        conn.execute(
            f"""INSERT INTO corpus_build_deltas
                (build_id, delta_kind, chunk_id, document_id, reason,
                 previous_content_hash, previous_metadata_hash,
                 replacement_chunk_id)
                SELECT ?, 'tombstone', p.chunk_id, p.document_id,
                       CASE
                         WHEN EXISTS (
                           SELECT 1 FROM corpus_build_source_documents source_doc
                           WHERE source_doc.build_id=?
                             AND source_doc.document_id=p.document_id
                             AND source_doc.eligibility='ineligible'
                         ) THEN 'eligibility_lost'
                         WHEN EXISTS (
                           SELECT 1 FROM corpus_build_chunks same_doc
                           WHERE same_doc.build_id=?
                             AND same_doc.document_id=p.document_id
                             AND same_doc.chunk_profile_version != p.chunk_profile_version
                         ) THEN 'profile_version_replaced'
                         WHEN EXISTS (
                           SELECT 1 FROM corpus_build_chunks same_doc
                           WHERE same_doc.build_id=?
                             AND same_doc.document_id=p.document_id
                         ) THEN 'disappeared_child'
                         ELSE 'document_removed'
                       END,
                       p.content_hash, p.metadata_hash,
                       (SELECT MIN(replacement.chunk_id COLLATE BINARY)
                        FROM corpus_build_chunks replacement
                        WHERE replacement.build_id=?
                          AND replacement.document_id=p.document_id
                          AND replacement.chunk_profile_version != p.chunk_profile_version)
                FROM {previous} p
                LEFT JOIN corpus_build_chunks n
                  ON n.build_id=? AND n.chunk_id=p.chunk_id
                WHERE n.chunk_id IS NULL""",
            (build_id, build_id, build_id, build_id, build_id, build_id),
        )
        conn.execute(
            """UPDATE corpus_build_chunks SET status='pending_embedding'
               WHERE build_id=? AND chunk_id IN (
                   SELECT chunk_id FROM corpus_build_deltas
                   WHERE build_id=? AND delta_kind='to_embed'
               )""",
            (build_id, build_id),
        )
        conn.execute(
            """UPDATE corpus_build_chunks SET status='metadata_only'
               WHERE build_id=? AND chunk_id IN (
                   SELECT chunk_id FROM corpus_build_deltas
                   WHERE build_id=? AND delta_kind='metadata_update'
               )""",
            (build_id, build_id),
        )
        conn.execute(
            """UPDATE corpus_publication_builds
               SET status='reconciliation_ready', reconciliation_ready=1, updated_at=?
               WHERE build_id=?""",
            (now, build_id),
        )
    return _reconciliation_summary(conn, build_id)


def _reconciliation_summary(
    conn: sqlite3.Connection, build_id: str
) -> ReconciliationSummary:
    def count(kind: str) -> int:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM corpus_build_deltas WHERE build_id=? AND delta_kind=?",
                (build_id, kind),
            ).fetchone()[0]
        )

    return ReconciliationSummary(
        to_embed_count=count("to_embed"),
        metadata_update_count=count("metadata_update"),
        tombstone_count=count("tombstone"),
    )


def iter_reconciliation_pages(
    conn: sqlite3.Connection,
    build_id: str,
    *,
    page_size: int = 500,
) -> Iterator[tuple[tuple[object, ...], ...]]:
    if not 1 <= page_size <= 500:
        raise ValueError("page_size must be between 1 and 500")
    cursor = conn.execute(
        """SELECT delta_kind, chunk_id, document_id, reason,
                  previous_content_hash, previous_metadata_hash,
                  replacement_chunk_id
           FROM corpus_build_deltas WHERE build_id=?
           ORDER BY delta_kind COLLATE BINARY, chunk_id COLLATE BINARY""",
        (build_id,),
    )
    while True:
        page = cursor.fetchmany(page_size)
        if not page:
            break
        yield tuple(tuple(row) for row in page)


def _update_lexical_digest(digest: Any, chunk_id: object, content_text: object) -> None:
    digest.update(_canonical_bytes([str(chunk_id), str(content_text)]))
    digest.update(b"\n")


def _lexical_source_digest(conn: sqlite3.Connection, build_id: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cursor = conn.execute(
        """SELECT chunk_id, content_text FROM corpus_build_chunks
           WHERE build_id=? AND eligibility='eligible'
             AND status IN ('active','pending_embedding','embedded','metadata_only')
           ORDER BY chunk_id COLLATE BINARY""",
        (build_id,),
    )
    for chunk_id, content_text in cursor:
        _update_lexical_digest(digest, chunk_id, content_text)
        count += 1
    return count, digest.hexdigest()


def _persisted_lexical_digest(
    conn: sqlite3.Connection, build_id: str
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cursor = conn.execute(
        """SELECT chunk_id, content_text FROM corpus_build_chunks_fts
           WHERE build_id=? ORDER BY chunk_id COLLATE BINARY""",
        (build_id,),
    )
    for chunk_id, content_text in cursor:
        _update_lexical_digest(digest, chunk_id, content_text)
        count += 1
    return count, digest.hexdigest()


def _lexical_rows_digest(rows: Iterable[tuple[object, object]]) -> str:
    digest = hashlib.sha256()
    for chunk_id, content_text in rows:
        _update_lexical_digest(digest, chunk_id, content_text)
    return digest.hexdigest()


def _iter_cursor_rows(cursor: Any, *, page_size: int = 500) -> Iterator[Any]:
    if not 1 <= page_size <= 500:
        raise ValueError("page_size must be between 1 and 500")
    while True:
        page = cursor.fetchmany(page_size)
        if not page:
            return
        yield from page


def _validated_fts_checkpoint(
    conn: sqlite3.Connection, build_id: str
) -> tuple[int, str | None]:
    valid_batches = 0
    checkpoint: str | None = None
    batches = conn.execute(
        """SELECT batch_no, row_count, text_utf8_bytes, first_chunk_id,
                  last_chunk_id, batch_digest, checkpoint_chunk_id
           FROM corpus_build_fts_batches
           WHERE build_id=? ORDER BY batch_no""",
        (build_id,),
    )
    for batch in _iter_cursor_rows(batches):
        batch_no, row_count, text_bytes, first_chunk_id, last_chunk_id = batch[:5]
        batch_digest, batch_checkpoint = batch[5:]
        if batch_no != valid_batches + 1 or batch_checkpoint != last_chunk_id:
            break
        if not 1 <= int(row_count) <= 500:
            break
        if checkpoint is not None and str(first_chunk_id).encode("utf-8") <= checkpoint.encode("utf-8"):
            break
        lower_sql = "" if checkpoint is None else "AND chunk_id > ? COLLATE BINARY"
        params: list[object] = [build_id]
        if checkpoint is not None:
            params.append(checkpoint)
        params.append(batch_checkpoint)
        persisted_cursor = conn.execute(
            f"""SELECT chunk_id, content_text FROM corpus_build_chunks_fts
                WHERE build_id=? {lower_sql}
                  AND chunk_id <= ? COLLATE BINARY
                ORDER BY chunk_id COLLATE BINARY""",
            params,
        )
        source_cursor = conn.execute(
            f"""SELECT chunk_id, content_text FROM corpus_build_chunks
                WHERE build_id=? AND eligibility='eligible'
                  AND status IN ('active','pending_embedding','embedded','metadata_only')
                  {lower_sql} AND chunk_id <= ? COLLATE BINARY
                ORDER BY chunk_id COLLATE BINARY""",
            params,
        )
        persisted_rows = _iter_cursor_rows(persisted_cursor)
        source_rows = _iter_cursor_rows(source_cursor)
        digest = hashlib.sha256()
        observed_count = 0
        observed_text_bytes = 0
        observed_first: object | None = None
        observed_last: object | None = None
        rows_match = True
        while True:
            persisted_row = next(persisted_rows, None)
            source_row = next(source_rows, None)
            if persisted_row is None and source_row is None:
                break
            if persisted_row is None or source_row is None or persisted_row != source_row:
                rows_match = False
                break
            if observed_first is None:
                observed_first = persisted_row[0]
            observed_last = persisted_row[0]
            observed_count += 1
            observed_text_bytes += len(str(persisted_row[1]).encode("utf-8"))
            _update_lexical_digest(digest, persisted_row[0], persisted_row[1])
        if (
            not rows_match
            or observed_count != int(row_count)
            or observed_text_bytes != int(text_bytes)
            or observed_first != first_chunk_id
            or observed_last != last_chunk_id
            or digest.hexdigest() != batch_digest
        ):
            break
        valid_batches += 1
        checkpoint = str(batch_checkpoint)
    return valid_batches, checkpoint


def _discard_invalid_fts_suffix(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    valid_batches: int,
    checkpoint: str | None,
    failure_injector: Callable[[str], None] | None,
) -> None:
    repair_state = conn.execute(
        """SELECT lexical_repair_checkpoint, lexical_repair_cursor
           FROM corpus_publication_builds WHERE build_id=?""",
        (build_id,),
    ).fetchone()
    stored_checkpoint = repair_state[0] if repair_state is not None else None
    cursor = repair_state[1] if repair_state is not None else None
    if stored_checkpoint != checkpoint or cursor is None:
        predicate = "" if checkpoint is None else "AND chunk_id > ? COLLATE BINARY"
        params: tuple[object, ...] = (
            (build_id,) if checkpoint is None else (build_id, checkpoint)
        )
        cursor_row = conn.execute(
            f"""SELECT MAX(chunk_id COLLATE BINARY)
                FROM corpus_build_chunks_fts
                WHERE build_id=? {predicate}""",
            params,
        ).fetchone()
        cursor = None if cursor_row is None else cursor_row[0]
        with conn:
            conn.execute(
                """UPDATE corpus_publication_builds
                   SET lexical_repair_checkpoint=?, lexical_repair_cursor=?
                   WHERE build_id=?""",
                (checkpoint, cursor, build_id),
            )

    while cursor is not None:
        predicate = "" if checkpoint is None else "AND chunk_id > ? COLLATE BINARY"
        params: list[object] = [build_id]
        if checkpoint is not None:
            params.append(checkpoint)
        params.append(cursor)
        rows = conn.execute(
            f"""SELECT rowid, chunk_id FROM corpus_build_chunks_fts
                WHERE build_id=? {predicate}
                  AND chunk_id <= ? COLLATE BINARY
                ORDER BY chunk_id COLLATE BINARY DESC
                LIMIT 500""",
            params,
        ).fetchmany(500)
        if not rows:
            cursor = None
            break
        rowids = [int(row[0]) for row in rows]
        placeholders = ",".join("?" for _ in rowids)
        next_cursor = str(rows[-1][1])
        with conn:
            conn.execute(
                f"DELETE FROM corpus_build_chunks_fts WHERE rowid IN ({placeholders})",
                rowids,
            )
            has_more_params: list[object] = [build_id]
            if checkpoint is not None:
                has_more_params.append(checkpoint)
            has_more_params.append(next_cursor)
            has_more = conn.execute(
                f"""SELECT 1 FROM corpus_build_chunks_fts
                    WHERE build_id=? {predicate}
                      AND chunk_id < ? COLLATE BINARY LIMIT 1""",
                has_more_params,
            ).fetchone()
            cursor = next_cursor if has_more is not None else None
            conn.execute(
                """UPDATE corpus_publication_builds
                   SET lexical_repair_cursor=? WHERE build_id=?""",
                (cursor, build_id),
            )
        if failure_injector is not None:
            failure_injector("after_fts_cleanup_batch")

    while True:
        batch_rows = conn.execute(
            """SELECT rowid FROM corpus_build_fts_batches
               WHERE build_id=? AND batch_no>? ORDER BY batch_no LIMIT 500""",
            (build_id, valid_batches),
        ).fetchmany(500)
        if not batch_rows:
            break
        rowids = [int(row[0]) for row in batch_rows]
        placeholders = ",".join("?" for _ in rowids)
        with conn:
            conn.execute(
                f"DELETE FROM corpus_build_fts_batches WHERE rowid IN ({placeholders})",
                rowids,
            )
    with conn:
        conn.execute(
            """UPDATE corpus_publication_builds
               SET lexical_repair_checkpoint=NULL, lexical_repair_cursor=NULL
               WHERE build_id=?""",
            (build_id,),
        )


def _fts_phase(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    manifest_id: str,
    limits: PublicationLimits,
    now: str,
    failure_injector: Callable[[str], None] | None,
    stats: _MutableBufferStats,
) -> StreamingLexicalResult:
    fts_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_build_chunks_fts'"
    ).fetchone()
    if fts_exists is None:
        raise RuntimeError("FTS5 table corpus_build_chunks_fts is unavailable")
    expected_count, expected_digest = _lexical_source_digest(conn, build_id)
    valid_batches, checkpoint = _validated_fts_checkpoint(conn, build_id)
    _discard_invalid_fts_suffix(
        conn,
        build_id=build_id,
        valid_batches=valid_batches,
        checkpoint=checkpoint,
        failure_injector=failure_injector,
    )
    with conn:
        conn.execute(
            """UPDATE corpus_publication_builds
               SET lexical_ready=0, lexical_expected_digest=?, lexical_digest=NULL,
                   lexical_row_count=0, updated_at=? WHERE build_id=?""",
            (expected_digest, now, build_id),
        )

    checkpoint_predicate = "" if checkpoint is None else "AND chunk_id > ? COLLATE BINARY"
    cursor_params: tuple[object, ...] = (
        (build_id,) if checkpoint is None else (build_id, checkpoint)
    )
    cursor = conn.execute(
        f"""SELECT chunk_id, content_text, content_hash
           FROM corpus_build_chunks
           WHERE build_id=? AND eligibility='eligible'
             AND status IN ('active','pending_embedding','embedded','metadata_only')
             {checkpoint_predicate}
           ORDER BY chunk_id COLLATE BINARY""",
        cursor_params,
    )
    batch_no = valid_batches

    def commit_batch(rows: list[tuple[object, ...]], text_utf8_bytes: int) -> None:
        nonlocal batch_no
        if not rows:
            return
        batch_no += 1
        with conn:
            conn.executemany(
                """INSERT INTO corpus_build_chunks_fts
                   (build_id, chunk_id, content_text) VALUES (?, ?, ?)""",
                ((build_id, row[0], row[1]) for row in rows),
            )
            conn.execute(
                """INSERT INTO corpus_build_fts_batches
                   (build_id, batch_no, row_count, text_utf8_bytes,
                    first_chunk_id, last_chunk_id, batch_digest,
                    checkpoint_chunk_id, committed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    build_id,
                    batch_no,
                    len(rows),
                    text_utf8_bytes,
                    rows[0][0],
                    rows[-1][0],
                    _lexical_rows_digest(
                        (row[0], row[1]) for row in rows
                    ),
                    rows[-1][0],
                    now,
                ),
            )
        if failure_injector is not None:
            failure_injector("after_fts_batch")

    fts_batch: list[tuple[object, ...]] = []
    fts_batch_bytes = 0
    for row in cursor:
        text_bytes = len(str(row[1]).encode("utf-8"))
        if text_bytes > limits.chunk_text_utf8_bytes:
            raise ResumableResourceStop(
                "fts_chunk_text_too_large",
                f"{row[0]} requires {text_bytes} UTF-8 bytes; "
                f"budget is {limits.chunk_text_utf8_bytes}",
            )
        if fts_batch and (
            len(fts_batch) >= limits.max_chunks
            or fts_batch_bytes + text_bytes > limits.chunk_text_utf8_bytes
        ):
            commit_batch(fts_batch, fts_batch_bytes)
            fts_batch.clear()
            fts_batch_bytes = 0
        fts_batch.append(tuple(row))
        fts_batch_bytes += text_bytes
        stats.peak_chunks = max(stats.peak_chunks, len(fts_batch))
        stats.peak_chunk_text_utf8_bytes = max(
            stats.peak_chunk_text_utf8_bytes, fts_batch_bytes
        )
    commit_batch(fts_batch, fts_batch_bytes)

    actual_count, digest_hex = _persisted_lexical_digest(conn, build_id)
    if actual_count != expected_count or digest_hex != expected_digest:
        raise RuntimeError("FTS source count/digest parity failure")
    with conn:
        conn.execute(
            """UPDATE corpus_publication_builds
               SET status='lexical_ready', lexical_ready=1,
                   lexical_digest=?, lexical_row_count=?, updated_at=?
               WHERE build_id=?""",
            (digest_hex, actual_count, now, build_id),
        )
    return StreamingLexicalResult(
        manifest_id=manifest_id,
        mode_served="fts5",
        row_count=actual_count,
        digest=digest_hex,
    )


def _cutover(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    manifest_id: str,
    certified_snapshot_identity: str,
    now: str,
    failure_injector: Callable[[str], None] | None,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT certified_snapshot_identity, status, document_count,
                      chunk_count, inventory_digest, lexical_expected_digest,
                      lexical_digest, lexical_row_count, reconciliation_ready,
                      lexical_ready
               FROM corpus_publication_builds WHERE build_id=?""",
            (build_id,),
        ).fetchone()
        if row is None or row[1] != "lexical_ready":
            raise RuntimeError("publication build is not lexical-ready")
        if row[0] != certified_snapshot_identity:
            raise RuntimeError("publication source identity mismatch")
        if not row[4] or not row[5] or row[5] != row[6]:
            raise RuntimeError("publication digest readiness mismatch")
        if int(row[3]) != int(row[7]) or int(row[8]) != 1 or int(row[9]) != 1:
            raise RuntimeError("publication count/readiness mismatch")
        complete_docs = int(
            conn.execute(
                """SELECT COUNT(*) FROM corpus_build_documents
                   WHERE build_id=? AND status='complete'""",
                (build_id,),
            ).fetchone()[0]
        )
        staged_chunks = int(
            conn.execute(
                "SELECT COUNT(*) FROM corpus_build_chunks WHERE build_id=?",
                (build_id,),
            ).fetchone()[0]
        )
        fts_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM corpus_build_chunks_fts WHERE build_id=?",
                (build_id,),
            ).fetchone()[0]
        )
        if complete_docs != int(row[2]) or staged_chunks != int(row[3]) or fts_rows != int(row[7]):
            raise RuntimeError("publication persisted count mismatch")
        persisted_fts_count, persisted_fts_digest = _persisted_lexical_digest(
            conn, build_id
        )
        if (
            persisted_fts_count != int(row[7])
            or persisted_fts_digest != row[5]
            or persisted_fts_digest != row[6]
        ):
            raise RuntimeError("publication persisted lexical count/digest mismatch")
        manifest = conn.execute(
            "SELECT 1 FROM corpus_manifest WHERE manifest_id=?",
            (manifest_id,),
        ).fetchone()
        if manifest is None:
            raise RuntimeError("unpublished corpus manifest missing")

        manifest_json = conn.execute(
            "SELECT manifest_json FROM corpus_publication_builds WHERE build_id=?",
            (build_id,),
        ).fetchone()[0]
        conn.execute("UPDATE corpus_manifest SET is_current=0 WHERE is_current=1")
        conn.execute(
            """UPDATE corpus_manifest
               SET manifest_json=?, is_current=1 WHERE manifest_id=?""",
            (manifest_json, manifest_id),
        )
        conn.execute("DELETE FROM lexical_index_state")
        conn.execute(
            """INSERT INTO lexical_index_state
               (singleton_id, schema_version, corpus_manifest_id, mode_served,
                row_count, built_at, lexical_generation_id, lexical_digest)
               VALUES (1, '1.0.0', ?, 'fts5', ?, ?, ?, ?)""",
            (manifest_id, int(row[7]), now, build_id, row[6]),
        )
        conn.execute(
            """UPDATE corpus_publication_builds
               SET status='published', published_at=?, updated_at=?
               WHERE build_id=?""",
            (now, now, build_id),
        )
        if failure_injector is not None:
            failure_injector("during_cutover")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _result(conn: sqlite3.Connection, build_id: str, stats: BufferStats) -> StreamingPublicationResult:
    row = conn.execute(
        """SELECT manifest_id, document_count, chunk_count, inventory_digest,
                  lexical_row_count, lexical_digest
           FROM corpus_publication_builds WHERE build_id=?""",
        (build_id,),
    ).fetchone()
    if row is None or not row[0] or not row[3] or not row[5]:
        raise RuntimeError("publication result is incomplete")
    return StreamingPublicationResult(
        build_id=build_id,
        corpus=StreamingCorpusResult(
            manifest_id=str(row[0]),
            document_count=int(row[1]),
            chunk_count=int(row[2]),
            inventory_digest=str(row[3]),
        ),
        lexical=StreamingLexicalResult(
            manifest_id=str(row[0]),
            mode_served="fts5",
            row_count=int(row[4]),
            digest=str(row[5]),
        ),
        reconciliation=_reconciliation_summary(conn, build_id),
        buffer_stats=stats,
    )


def build_streaming_corpus_and_lexical_index(
    conn: sqlite3.Connection,
    *,
    certified_snapshot_identity: str,
    clock: Callable[[], str],
    normalization_version: str = "1.0.0",
    limits: PublicationLimits = PublicationLimits(),
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_interval: float = 30.0,
    monotonic: Callable[[], float] = time.monotonic,
    rss_reader: Callable[[], int] = _current_rss_bytes,
    failure_injector: Callable[[str], None] | None = None,
) -> StreamingPublicationResult:
    """Build and atomically select one corpus+lexical generation."""
    if progress_interval < 0:
        raise ValueError("progress_interval must be non-negative")
    header = _header(certified_snapshot_identity, normalization_version)
    build_id = _build_id(header)
    now = clock()
    ensure_streaming_publication_schema(conn)
    with conn:
        conn.execute(
            """INSERT INTO corpus_publication_builds
               (build_id, certified_snapshot_identity, header_json, status,
                created_at, updated_at)
               VALUES (?, ?, ?, 'staging', ?, ?)
               ON CONFLICT(build_id) DO NOTHING""",
            (build_id, certified_snapshot_identity, _canonical_bytes(header), now, now),
        )
    existing_status = conn.execute(
        "SELECT status FROM corpus_publication_builds WHERE build_id=?", (build_id,)
    ).fetchone()[0]
    empty_stats = BufferStats(0, 0, 0, 0)
    if existing_status == "published":
        return _result(conn, build_id, empty_stats)

    stats = _MutableBufferStats()
    progress = _Progress(progress_callback, progress_interval, monotonic, rss_reader)
    documents = 0
    chunks = 0
    source_bytes = 0
    chunk_bytes = 0
    for source in _iter_source_documents(conn, page_size=limits.max_documents):
        _stage_source_presence(conn, build_id=build_id, source=source, now=now)
        if source.eligibility != "eligible":
            continue
        document_chunks, document_chunk_bytes = _stage_document(
            conn,
            build_id=build_id,
            source=source,
            limits=limits,
            stats=stats,
            now=now,
            failure_injector=failure_injector,
        )
        documents += 1
        chunks += document_chunks
        source_bytes += source.source_utf8_bytes
        chunk_bytes += document_chunk_bytes
        progress.emit(
            phase="staging",
            documents=documents,
            chunks=chunks,
            source_bytes=source_bytes,
            chunk_bytes=chunk_bytes,
        )
    with conn:
        conn.execute(
            """UPDATE corpus_publication_builds
               SET status='staging_ready', document_count=?, chunk_count=?,
                   source_utf8_bytes=?, updated_at=? WHERE build_id=?""",
            (documents, chunks, source_bytes, now, build_id),
        )
    progress.emit(
        phase="staging",
        documents=documents,
        chunks=chunks,
        source_bytes=source_bytes,
        chunk_bytes=chunk_bytes,
        final=True,
    )
    if failure_injector is not None:
        failure_injector("after_staging")

    manifest_id, _chunk_count, _inventory_digest = _manifest_phase(
        conn, build_id=build_id, header=header, now=now
    )
    if failure_injector is not None:
        failure_injector("after_manifest")

    _reconciliation_phase(conn, build_id=build_id, now=now)
    if failure_injector is not None:
        failure_injector("after_reconciliation")

    _fts_phase(
        conn,
        build_id=build_id,
        manifest_id=manifest_id,
        limits=limits,
        now=now,
        failure_injector=failure_injector,
        stats=stats,
    )
    if failure_injector is not None:
        failure_injector("before_cutover")
    _cutover(
        conn,
        build_id=build_id,
        manifest_id=manifest_id,
        certified_snapshot_identity=certified_snapshot_identity,
        now=now,
        failure_injector=failure_injector,
    )
    return _result(conn, build_id, stats.frozen())


def iter_chunk_pages(
    conn: sqlite3.Connection,
    build_id: str,
    *,
    page_size: int = 500,
) -> Iterator[tuple[tuple[object, ...], ...]]:
    if not 1 <= page_size <= 500:
        raise ValueError("page_size must be between 1 and 500")
    cursor = conn.execute(
        """SELECT chunk_id, document_id, chunk_profile_version, section_key,
                  ordinal, content_hash, metadata_hash, available_at,
                  source_class, dedup_cluster_id, cluster_first_available_at,
                  representative_document_id, eligibility
           FROM corpus_build_chunks WHERE build_id=?
           ORDER BY chunk_id COLLATE BINARY""",
        (build_id,),
    )
    while True:
        page = cursor.fetchmany(page_size)
        if not page:
            break
        yield tuple(tuple(row) for row in page)


def estimate_streaming_publication_resources(
    conn: sqlite3.Connection,
    *,
    limits: PublicationLimits = PublicationLimits(),
) -> StreamingResourceEstimate:
    source_bytes = 0
    document_count = 0
    largest = 0
    estimated_chunks = 0
    for source in _iter_source_documents(conn, page_size=limits.max_documents):
        if source.eligibility != "eligible":
            continue
        size = source.source_utf8_bytes
        source_bytes += size
        document_count += 1
        largest = max(largest, size)
        estimated_chunks += max(1, (size + 1199) // 1200)

    tokenizer_cache = 1536 * 1024**2
    sqlite_cache = 256 * 1024**2
    interpreter = 256 * 1024**2
    source_phase = (
        tokenizer_cache
        + sqlite_cache
        + interpreter
        + max(largest, limits.source_utf8_bytes)
        + limits.chunk_text_utf8_bytes
    )
    phase_headroom = {
        "source_and_tokenizer": source_phase,
        "inventory": sqlite_cache + interpreter + 8 * 1024**2,
        "reconciliation": sqlite_cache + interpreter + 16 * 1024**2,
        "fts": 2 * sqlite_cache + interpreter + limits.chunk_text_utf8_bytes,
        "cutover": sqlite_cache + interpreter + 4 * 1024**2,
    }
    required = max(phase_headroom.values())
    return StreamingResourceEstimate(
        source_utf8_bytes=source_bytes,
        eligible_document_count=document_count,
        estimated_chunks=estimated_chunks,
        largest_source_document_utf8_bytes=largest,
        phase_headroom_bytes=phase_headroom,
        estimated_peak_bytes=required,
        required_headroom=required,
    )


__all__ = [
    "BufferStats",
    "PublicationLimits",
    "ResumableResourceStop",
    "StreamingPublicationResult",
    "StreamingResourceEstimate",
    "build_streaming_corpus_and_lexical_index",
    "compute_streaming_manifest_id",
    "ensure_streaming_publication_schema",
    "estimate_streaming_publication_resources",
    "export_legacy_manifest",
    "iter_chunk_pages",
    "iter_reconciliation_pages",
    "served_chunks_relation",
]
