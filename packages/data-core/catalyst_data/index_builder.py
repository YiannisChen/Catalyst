"""Article-level index record builder (dry-run only — no embeddings, no LanceDB).

Constructs L1 + L2 chunk records from the articles table for future LanceDB
ingestion.  One L1 record per canonical article_id.  L2 sentence chunks only
when body length ≥ min_l2_chars threshold.

L1 = whole-article vector (title + description).
L2 = sentence-level chunks from body only (description for articles;
     document text for filings).  Gated on len(body), not title+body.

Invariants asserted inside build_index_records (protect all callers):
  - L1 count == SELECT COUNT(*) FROM articles  (dedup: one L1 per article_id)
  - sum(len(r["tickers"]) for L1) == SELECT COUNT(*) FROM article_tickers

IMPORTANT: This module NEVER imports lancedb or any embedding library.
It is a pure data-structuring module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any


def _dict_rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def compute_content_hash(title: str, description: str | None) -> str:
    """Full 64-hex SHA-256 of NFC-normalized text.

    If description is non-empty: text = NFC(title + "\n" + description).
    If description is None/empty: text = NFC(title) — no trailing newline.

    Metadata (source_tier, dedup_group_id, quality_score) is NOT part of the
    hash — only the embedding input matters.
    """
    desc = (description or "").strip()
    if desc:
        text = unicodedata.normalize("NFC", f"{title}\n{desc}")
    else:
        text = unicodedata.normalize("NFC", title)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_article_records(
    conn: sqlite3.Connection, *, min_l2_chars: int = 800
) -> list[dict[str, Any]]:
    """Build L1 + L2 chunk records from articles JOIN article_tickers.

    L1: one per canonical article_id — content_text = title + "\n" + description.
    L2: sentence chunks from body only (description for articles), gated on
         len(body) >= min_l2_chars.  Title is never split into L2.

    Asserts dedup and lossless guards after building.  Raises AssertionError
    on violation — protects all callers including Step 4 embed runs.

    Never writes anything; no LanceDB imports.
    """
    # Fetch all articles with their ticker associations (grouped)
    rows = conn.execute("""
        SELECT
            a.article_id, a.provider, a.source_type, a.ticker,
            a.reference_date, a.published_utc,
            a.title, a.description,
            a.article_url, a.image_url, a.author,
            a.publisher_name, a.publisher_logo_url,
            a.source_tier, a.dedup_group_id,
            GROUP_CONCAT(at.ticker, ',') AS tickers_csv
        FROM articles a
        LEFT JOIN article_tickers at ON a.article_id = at.article_id
        GROUP BY a.article_id
        ORDER BY a.article_id
    """).fetchall()

    records: list[dict[str, Any]] = []

    for row in rows:
        (
            article_id, provider, source_type, scalar_ticker,
            reference_date, published_utc,
            title, description,
            article_url, image_url, author,
            publisher_name, publisher_logo_url,
            source_tier, dedup_group_id,
            tickers_csv,
        ) = row

        # Parse tickers from group_concat
        tickers = (
            sorted(set(t for t in (tickers_csv or "").split(",") if t))
            if tickers_csv
            else [scalar_ticker] if scalar_ticker else []
        )

        body = description or ""

        # --- L1: one per article_id ---
        content_hash = compute_content_hash(title, description)
        content_text = f"{title}\n{body}"

        l1_record = {
            "chunk_id": f"{article_id}::l1",
            "chunk_level": "l1",
            "article_id": article_id,
            "parent_article_id": article_id,
            "content_hash": content_hash,
            "content_text": content_text,
            "provider": provider,
            "source_type": source_type,
            "publisher_name": publisher_name,
            "publisher_logo_url": publisher_logo_url,
            "article_url": article_url,
            "image_url": image_url,
            "author": author,
            "published_utc": published_utc,
            "reference_date": reference_date,
            "source_tier": source_tier or 4,
            "dedup_group_id": dedup_group_id,
            "tickers": tickers,
        }
        records.append(l1_record)

        # --- L2: sentence chunks from body only ---
        # Gate on body length (NOT title+body).  Polygon descriptions max
        # 758 chars so L2 stays at 0; SEC long-form bodies will trigger L2.
        if len(body) >= min_l2_chars:
            sentences = _split_sentences(body)
            for idx, sent in enumerate(sentences):
                if not sent.strip():
                    continue
                l2_record = {
                    "chunk_id": f"{article_id}::l2s{idx:04d}",
                    "chunk_level": "l2",
                    "article_id": article_id,
                    "parent_article_id": article_id,
                    "content_hash": compute_content_hash(sent, None),
                    "content_text": sent,
                    "provider": provider,
                    "source_type": source_type,
                    "publisher_name": publisher_name,
                    "publisher_logo_url": publisher_logo_url,
                    "article_url": article_url,
                    "image_url": image_url,
                    "author": author,
                    "published_utc": published_utc,
                    "reference_date": reference_date,
                    "source_tier": source_tier or 4,
                    "dedup_group_id": dedup_group_id,
                    "tickers": tickers,
                }
                records.append(l2_record)

    # ---- Guards: protect all callers (CLI, Step 4 embed, tests) ----
    _assert_guards(conn, records)

    return records


def _assert_guards(
    conn: sqlite3.Connection, records: list[dict[str, Any]]
) -> None:
    """Assert dedup and lossless invariants.  Raises AssertionError on violation."""
    article_count = conn.execute(
        "SELECT COUNT(*) FROM articles"
    ).fetchone()[0]
    at_count = conn.execute(
        "SELECT COUNT(*) FROM article_tickers"
    ).fetchone()[0]

    l1_records = [r for r in records if r["chunk_level"] == "l1"]
    l1_count = len(l1_records)
    ticker_refs = sum(len(r["tickers"]) for r in l1_records)

    assert l1_count == article_count, (
        f"Dedup guard FAILED: L1={l1_count}, articles={article_count}"
    )
    assert ticker_refs == at_count, (
        f"Ticker-lossless guard FAILED: records={ticker_refs}, DB={at_count}"
    )


def build_index_records(
    conn, *, min_l2_chars: int = 800
) -> list[dict[str, Any]]:
    """Backward-compatible wrapper: article + filing records combined."""
    article_records = build_article_records(conn, min_l2_chars=min_l2_chars)
    filing_records = build_filing_records(conn, min_l2_chars=min_l2_chars)
    return article_records + filing_records


@dataclass(frozen=True)
class CorpusBuildResult:
    manifest_id: str
    chunks: list[Any]
    reconciliation: Any


@dataclass(frozen=True)
class CorpusAndLexicalBuildResult:
    corpus: CorpusBuildResult
    lexical: Any


def build_corpus(
    conn: sqlite3.Connection,
    *,
    certified_snapshot_identity: str,
    normalization_version: str = "1.0.0",
) -> CorpusBuildResult:
    """Build and atomically publish the B3 corpus from canonical documents."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile
    from catalyst_data.corpus.manifest import (
        build_manifest,
        compute_manifest_id,
        reconcile_and_publish,
    )
    from catalyst_data.corpus.news_v2 import NewsV2Profile
    from catalyst_data.corpus.source_classifier import CLASSIFIER_VERSION, classify
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION

    active_chunks: dict[str, dict[str, Any]] = {}
    produced_chunks: list[Any] = []
    article_columns = _table_columns(conn, "articles")
    provider_expr = "a.provider" if "provider" in article_columns else "COALESCE(a.source, 'polygon')"
    source_type_expr = "a.source_type" if "source_type" in article_columns else "'polygon_news'"
    publisher_expr = "a.publisher_name" if "publisher_name" in article_columns else "a.publisher"
    dedup_group_expr = "a.dedup_group_id" if "dedup_group_id" in article_columns else "NULL"
    eligibility_predicates = []
    if "is_canonical" in article_columns:
        eligibility_predicates.append("a.is_canonical = 1")
    if "is_rag_eligible" in article_columns:
        eligibility_predicates.append("a.is_rag_eligible = 1")
    where_clause = " AND ".join(eligibility_predicates) or "1 = 1"
    article_rows = _dict_rows(conn.execute(f"""
        SELECT
            a.article_id, {provider_expr} AS provider,
            {source_type_expr} AS source_type, a.published_utc,
            a.title, a.description, a.article_url,
            {publisher_expr} AS publisher_name,
            {dedup_group_expr} AS dedup_group_id,
            a.source_class, a.dedup_cluster_id,
            a.cluster_first_available_at, a.representative_document_id,
            GROUP_CONCAT(at.ticker, ',') AS tickers_csv
        FROM articles a
        LEFT JOIN article_tickers at ON at.article_id = a.article_id
        WHERE {where_clause}
        GROUP BY a.article_id
        ORDER BY a.article_id
    """))
    news_profile = NewsV2Profile()
    for row in article_rows:
        tickers = sorted({item for item in (row["tickers_csv"] or "").split(",") if item})
        ticker_associations = json.dumps(tickers, separators=(",", ":"))
        source_class = row["source_class"] or classify(
            row["source_type"],
            article_url=row["article_url"],
            publisher=row["publisher_name"],
        )
        cluster_id = row["dedup_cluster_id"] or row["dedup_group_id"]
        first_available = row["cluster_first_available_at"] or row["published_utc"]
        representative = row["representative_document_id"] or row["article_id"]
        conn.execute(
            """UPDATE articles SET source_class = ?, dedup_cluster_id = ?,
               cluster_first_available_at = ?, representative_document_id = ?
               WHERE article_id = ?""",
            (source_class, cluster_id, first_available, representative, row["article_id"]),
        )
        document = {
            "document_id": row["article_id"],
            "title": row["title"],
            "description": row["description"],
            "available_at": row["published_utc"],
            "ticker_associations": ticker_associations,
            "source_class": source_class,
            "dedup_cluster_id": cluster_id,
            "cluster_first_available_at": first_available,
            "representative_document_id": representative,
            "eligibility": "eligible",
        }
        for chunk in news_profile.chunk(document):
            produced_chunks.append(chunk)
            active_chunks[chunk.chunk_id] = {
                **vars(chunk),
                "dedup_cluster_id": cluster_id,
                "cluster_first_available_at": first_available,
                "representative_document_id": representative,
                "source_kind": "article",
                "provider": row["provider"],
                "source_type": row["source_type"],
            }

    filing_rows = _dict_rows(conn.execute("""
        SELECT
            f.filing_id, f.form_type, f.filed_at, f.ticker,
            f.dedup_group_id, fd.document_url, fd.document_type, fd.text
        FROM filings f
        JOIN filing_documents fd ON fd.filing_id = f.filing_id
        WHERE f.is_canonical = 1 AND f.is_rag_eligible = 1
          AND fd.extraction_status = 'success' AND length(trim(fd.text)) > 0
        ORDER BY f.filing_id, fd.document_type, fd.document_url
    """)) if _table_exists(conn, "filings") and _table_exists(conn, "filing_documents") else []
    filing_profile = FilingV2Profile()
    for row in filing_rows:
        document_type = row["document_type"] or "primary_doc"
        is_exhibit = document_type.lower().startswith("exhibit_99")
        declared_type = (
            document_type.lower().replace("exhibit_", "EX-").replace("_", ".")
            if is_exhibit else row["form_type"]
        )
        document_id = (
            f"{row['filing_id']}:{document_type.lower()}"
            if is_exhibit else row["filing_id"]
        )
        document = {
            "document_id": document_id,
            "filing_type": declared_type,
            "raw_text": row["text"],
            "available_at": row["filed_at"],
            "ticker_associations": json.dumps([row["ticker"]], separators=(",", ":")),
            "source_class": "official_government",
            "dedup_cluster_id": row["dedup_group_id"],
            "cluster_first_available_at": row["filed_at"],
            "representative_document_id": document_id,
            "eligibility": "eligible",
        }
        for chunk in filing_profile.chunk(document):
            produced_chunks.append(chunk)
            active_chunks[chunk.chunk_id] = {
                **vars(chunk),
                "dedup_cluster_id": row["dedup_group_id"],
                "cluster_first_available_at": row["filed_at"],
                "representative_document_id": document_id,
                "source_kind": "filing",
                "provider": "sec",
                "source_type": "sec_filing",
            }

    inventory_fields = (
        "chunk_id", "document_id", "chunk_profile_version", "section_key",
        "ordinal", "content_hash", "metadata_hash", "available_at",
        "source_class", "dedup_cluster_id", "cluster_first_available_at",
        "representative_document_id", "eligibility",
    )
    inventory = [
        {field: chunk[field] for field in inventory_fields}
        for chunk in active_chunks.values()
    ]
    manifest = build_manifest(
        normalization_version=normalization_version,
        chunk_profile_versions={"news": "news_v2", "filing": "filing_v2"},
        source_classifier_version=CLASSIFIER_VERSION,
        certified_snapshot_identity=certified_snapshot_identity,
        active_chunk_inventory=inventory,
        tokenizer_revision=TOKENIZER_REVISION,
        embedding_revision=None,
    )
    manifest_id = compute_manifest_id(manifest)
    manifest_json = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    reconciliation = reconcile_and_publish(
        conn,
        next_manifest_id=manifest_id,
        next_manifest_json=manifest_json,
        active_chunks=active_chunks,
    )
    return CorpusBuildResult(manifest_id, produced_chunks, reconciliation)


def build_corpus_and_lexical_index(
    conn: sqlite3.Connection,
    *,
    certified_snapshot_identity: str,
    clock,
    normalization_version: str = "1.0.0",
) -> CorpusAndLexicalBuildResult:
    """Publish the B3 corpus, then build the B4 index for that exact manifest."""
    from catalyst_data.retrieval.fts5_builder import build_fts5_index

    corpus = build_corpus(
        conn,
        certified_snapshot_identity=certified_snapshot_identity,
        normalization_version=normalization_version,
    )
    lexical = build_fts5_index(conn, corpus.manifest_id, clock=clock)
    return CorpusAndLexicalBuildResult(corpus=corpus, lexical=lexical)

def index_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return aggregate summary of built index records.

    Polymorphic — handles both article records (article_id) and filing
    records (corpus_item_id + source_kind) via unified accessor.
    """
    def _rid(r):
        return r.get("corpus_item_id") or r["article_id"]

    def _sk(r):
        return r.get("source_kind", "article")

    l1 = [r for r in records if r["chunk_level"] == "l1"]
    l2 = [r for r in records if r["chunk_level"] == "l2"]

    l2_eligible_ids = {(_rid(r), _sk(r)) for r in l2}
    total_corpus_ids = {(_rid(r), _sk(r)) for r in l1}

    per_tier: dict[str, dict[int, dict[str, int]]] = {}
    for r in l1:
        sk = _sk(r)
        tier = r["source_tier"]
        per_tier.setdefault(sk, {}).setdefault(tier, {"l1": 0, "l2": 0})
        per_tier[sk][tier]["l1"] += 1

    for r in l2:
        sk = _sk(r)
        tier = r["source_tier"]
        per_tier.setdefault(sk, {}).setdefault(tier, {"l1": 0, "l2": 0})
        per_tier[sk][tier]["l2"] += 1

    article_l1 = sum(1 for r in l1 if _sk(r) == "article")
    article_l2 = sum(1 for r in l2 if _sk(r) == "article")
    filing_l1 = sum(1 for r in l1 if _sk(r) == "filing")
    filing_l2 = sum(1 for r in l2 if _sk(r) == "filing")

    return {
        "l1_count": len(l1),
        "l2_count": len(l2),
        "article_l1_count": article_l1,
        "article_l2_count": article_l2,
        "filing_l1_count": filing_l1,
        "filing_l2_count": filing_l2,
        "l2_eligible_count": len(l2_eligible_ids),
        "l2_eligible_pct": (
            round(100 * len(l2_eligible_ids) / len(total_corpus_ids), 2)
            if total_corpus_ids else 0
        ),
        "would_embed_count": len(l1) + len(l2),
        "per_tier": per_tier,
    }


# ---------------------------------------------------------------------------
# Filing record builder (Step 3C)
# ---------------------------------------------------------------------------

def build_filing_records(
    conn, *, min_l2_chars: int = 800
) -> list[dict[str, Any]]:
    """Build L1 + L2 index records from filings LEFT JOIN filing_documents.

    Emits EXACTLY ONE L1 per rag-eligible filing.  When a filing has multiple
    successful document rows, picks the BEST one: exhibit_99_1 preferred over
    primary_doc.  Body text and L2 sentences come from the chosen document.
    Done in Python (not SQL GROUP BY — SQLite GROUP BY doesn't guarantee
    row selection order).
    """
    rows = conn.execute("""
        SELECT
            f.filing_id, f.cik, f.ticker, f.form_type, f.filed_at,
            f.accession_number, f.url, f.source_tier,
            fd.text AS doc_text, fd.extraction_status, fd.document_type
        FROM filings f
        LEFT JOIN filing_documents fd
            ON f.filing_id = fd.filing_id
            AND fd.extraction_status = 'success'
        WHERE f.is_rag_eligible = 1
        ORDER BY f.filing_id, fd.document_type ASC
    """).fetchall()

    # Deduplicate: one row per filing_id.  ORDER BY document_type ASC
    # puts exhibit_99_1 before primary_doc (e < p).  For each filing_id,
    # pick the first row that has real document text.  If no document row
    # exists, fall back to the NULL LEFT JOIN row (metadata-only filing).
    best_doc: dict[str, tuple] = {}
    null_rows: dict[str, tuple] = {}
    for row in rows:
        fid = row[0]
        if row[9] is not None:  # has document text
            if fid not in best_doc:
                best_doc[fid] = row  # exhibit comes first, never overwrite
        else:
            if fid not in null_rows:
                null_rows[fid] = row  # save NULL row as fallback
    # Fill gaps: filings with no documents get the NULL row
    for fid, null_row in null_rows.items():
        if fid not in best_doc:
            best_doc[fid] = null_row

    records: list[dict[str, Any]] = []

    for fid, row in best_doc.items():
        (filing_id, cik, ticker, form_type, filed_at,
         accession_number, url, source_tier, doc_text, extraction_status, doc_type) = row

        title = f"{form_type} filed {filed_at}"
        body = doc_text or ""
        content_hash = compute_content_hash(title, body)

        content_text = f"{title}\n{body}" if body else title
        l1_record = {
            "chunk_id": f"{filing_id}::l1",
            "chunk_level": "l1",
            "corpus_item_id": filing_id,
            "source_kind": "filing",
            "content_hash": content_hash,
            "content_text": content_text,
            "provider": "sec",
            "source_type": "sec_filing",
            "tickers": [ticker],
            "source_tier": source_tier or 1,
            "filed_at": filed_at,
            "form_type": form_type,
            "accession_number": accession_number,
            "filing_url": url,
        }
        records.append(l1_record)

        if extraction_status == "success" and len(body) >= min_l2_chars:
            sentences = _split_sentences(body)
            for idx, sent in enumerate(sentences):
                if not sent.strip():
                    continue
                l2_record = {
                    "chunk_id": f"{filing_id}::l2s{idx:04d}",
                    "chunk_level": "l2",
                    "corpus_item_id": filing_id,
                    "source_kind": "filing",
                    "content_hash": compute_content_hash(sent, ""),
                    "content_text": sent,
                    "provider": "sec",
                    "source_type": "sec_filing",
                    "tickers": [ticker],
                    "source_tier": source_tier or 1,
                    "filed_at": filed_at,
                    "form_type": form_type,
                    "accession_number": accession_number,
                    "filing_url": url,
                }
                records.append(l2_record)

    # Guard: L1 count == COUNT rag-eligible filings
    rag_count = conn.execute(
        "SELECT COUNT(*) FROM filings WHERE is_rag_eligible = 1"
    ).fetchone()[0]
    l1_count = sum(1 for r in records if r["chunk_level"] == "l1")
    assert l1_count == rag_count, (
        f"Filing dedup guard FAILED: L1={l1_count}, rag_eligible_filings={rag_count}"
    )

    return records

# ---------------------------------------------------------------------------
# Internal: sentence splitting (no heavy deps)
# ---------------------------------------------------------------------------

import re

_SENTENCE_END_RE = re.compile(r'(?<=[.!?])\s+')


def _split_sentences(text: str) -> list[str]:
    """Simple regex-based sentence splitter (Punkt-free fallback).

    Tries nltk.tokenize.sent_tokenize first (if nltk is already installed);
    falls back to regex splitting on [.!?] followed by whitespace.
    """
    try:
        from nltk.tokenize import sent_tokenize  # type: ignore[import-untyped]
        return sent_tokenize(text)
    except (ImportError, LookupError):
        pass

    # Fallback: split on sentence-ending punctuation + whitespace
    parts = _SENTENCE_END_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Incremental indexer (dry-run, delta-only — Step 2)
# ---------------------------------------------------------------------------

def build_incremental_records(
    conn: sqlite3.Connection, *, min_l2_chars: int = 800
) -> dict[str, Any]:
    """Diff ELIGIBLE articles vs index_state pending rows, build delta records only.

    Step 4a reconciliation: applies the SAME eligibility filter as persist_index_state
    (is_rag_eligible=1 AND >=1 canonical association). Uses corpus_item_id with
    source_kind='article' and status='pending' for the join — stale/embedded rows are
    ignored. Recomputes content_hash per article via compute_content_hash.

    Returns a dict — NEVER writes index_state or index_manifests. Those are populated
    exclusively by persist_index_state (full build) or the GPU embed phase.

    Returns:
        { new_article_count: int,
          changed_article_count: int,
          total_delta_articles: int,
          l1_count: int,
          l2_count: int,
          l2_eligible_count: int,
          would_embed_count: int,
          delta_article_ids: list[str] }
    """
    from catalyst_data.eligibility import eligible_article_ids

    # Only consider eligible articles (S1 filter)
    eligible = eligible_article_ids(conn)
    if not eligible:
        return {
            "new_article_count": 0, "changed_article_count": 0,
            "total_delta_articles": 0, "l1_count": 0, "l2_count": 0,
            "l2_eligible_count": 0, "would_embed_count": 0,
            "delta_article_ids": [],
        }

    # Fetch eligible articles with title/description for hash computation
    placeholders_e = ",".join("?" for _ in eligible)
    all_rows = conn.execute(
        f"""SELECT a.article_id, a.title, a.description
            FROM articles a
            WHERE a.article_id IN ({placeholders_e})
            ORDER BY a.article_id""",
        eligible,
    ).fetchall()

    # Indexed articles with content_hash — only PENDING rows (S2: ignore stale/embedded)
    indexed_rows = conn.execute("""
        SELECT s.corpus_item_id, s.content_hash
        FROM index_state s
        WHERE s.source_kind = 'article'
          AND s.chunk_level = 'l1'
          AND s.status = 'pending'
        ORDER BY s.corpus_item_id
    """).fetchall()
    indexed: dict[str, str] = {r[0]: r[1] for r in indexed_rows}

    # Determine delta: new articles + articles with changed content_hash
    new_ids: list[str] = []
    changed_ids: list[str] = []

    for article_id, title, desc in all_rows:
        current_hash = compute_content_hash(title, desc)
        if article_id not in indexed:
            new_ids.append(article_id)
        elif indexed[article_id] != current_hash:
            changed_ids.append(article_id)

    delta_ids = new_ids + changed_ids

    if not delta_ids:
        return {
            "new_article_count": 0, "changed_article_count": 0,
            "total_delta_articles": 0, "l1_count": 0, "l2_count": 0,
            "l2_eligible_count": 0, "would_embed_count": 0,
            "delta_article_ids": [],
        }

    # Fetch full article data for delta ids only — join canonical associations (S1)
    placeholders = ",".join("?" for _ in delta_ids)
    rows = conn.execute(
        f"""SELECT
                a.article_id, a.provider, a.source_type, a.ticker,
                a.reference_date, a.published_utc,
                a.title, a.description,
                a.article_url, a.image_url, a.author,
                a.publisher_name, a.publisher_logo_url,
                a.source_tier, a.dedup_group_id,
                GROUP_CONCAT(at.ticker, ',') AS tickers_csv
            FROM articles a
            LEFT JOIN article_tickers at ON a.article_id = at.article_id AND at.is_canonical = 1
            WHERE a.article_id IN ({placeholders})
            GROUP BY a.article_id
            ORDER BY a.article_id""",
        delta_ids,
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        (
            article_id, provider, source_type, scalar_ticker,
            reference_date, published_utc,
            title, description,
            article_url, image_url, author,
            publisher_name, publisher_logo_url,
            source_tier, dedup_group_id,
            tickers_csv,
        ) = row

        tickers = (
            sorted(set(t for t in (tickers_csv or "").split(",") if t))
            if tickers_csv
            else [scalar_ticker] if scalar_ticker else []
        )

        body = description or ""

        # L1
        content_hash = compute_content_hash(title, description)
        content_text = f"{title}\n{body}"

        l1_record = {
            "chunk_id": f"{article_id}::l1",
            "chunk_level": "l1",
            "article_id": article_id,
            "parent_article_id": article_id,
            "content_hash": content_hash,
            "content_text": content_text,
            "provider": provider,
            "source_type": source_type,
            "publisher_name": publisher_name,
            "publisher_logo_url": publisher_logo_url,
            "article_url": article_url,
            "image_url": image_url,
            "author": author,
            "published_utc": published_utc,
            "reference_date": reference_date,
            "source_tier": source_tier or 4,
            "dedup_group_id": dedup_group_id,
            "tickers": tickers,
        }
        records.append(l1_record)

        # L2
        if len(body) >= min_l2_chars:
            sentences = _split_sentences(body)
            for idx, sent in enumerate(sentences):
                if not sent.strip():
                    continue
                l2_record = {
                    "chunk_id": f"{article_id}::l2s{idx:04d}",
                    "chunk_level": "l2",
                    "article_id": article_id,
                    "parent_article_id": article_id,
                    "content_hash": compute_content_hash(sent, None),
                    "content_text": sent,
                    "provider": provider,
                    "source_type": source_type,
                    "publisher_name": publisher_name,
                    "publisher_logo_url": publisher_logo_url,
                    "article_url": article_url,
                    "image_url": image_url,
                    "author": author,
                    "published_utc": published_utc,
                    "reference_date": reference_date,
                    "source_tier": source_tier or 4,
                    "dedup_group_id": dedup_group_id,
                    "tickers": tickers,
                }
                records.append(l2_record)

    l1 = [r for r in records if r["chunk_level"] == "l1"]
    l2 = [r for r in records if r["chunk_level"] == "l2"]

    return {
        "new_article_count": len(new_ids),
        "changed_article_count": len(changed_ids),
        "total_delta_articles": len(delta_ids),
        "l1_count": len(l1),
        "l2_count": len(l2),
        "l2_eligible_count": len({r["article_id"] for r in l2}),
        "would_embed_count": len(l1) + len(l2),
        "delta_article_ids": delta_ids,
    }


# ---------------------------------------------------------------------------
# Step 4a — Persist index_state as pending queue (UPSERT-by-chunk_id)
# ---------------------------------------------------------------------------

def persist_index_state(conn, *, min_l2_chars: int = 800) -> dict:
    """Build eligible article + filing records and UPSERT into index_state.

    Article eligibility: is_rag_eligible=1 AND has >=1 canonical article_tickers
    association (per-association canonicality from S1).

    UPSERT semantics keyed by chunk_id:
      - Existing row with same content_hash → skip (idempotent).
      - Existing row with different content_hash → mark status='stale'.
      - New chunk_id → INSERT as status='pending'.
      - After stale marking, INSERT the new row as status='pending'.
      Exactly one pending row per chunk_id after each run.

    Returns dict with counts for observability.
    """
    from catalyst_data.eligibility import eligible_article_ids
    from catalyst_data.index_builder import compute_content_hash, _split_sentences
    from catalyst_data.storage.sqlite import _migrate_index_state_step4a
    _migrate_index_state_step4a(conn)

    total_new = 0
    article_l1 = 0
    article_l2 = 0

    # --- Article records (eligible only) ---
    eligible_ids = eligible_article_ids(conn)
    if eligible_ids:
        placeholders = ",".join("?" for _ in eligible_ids)
        rows = conn.execute(
            f"""SELECT
                    a.article_id, a.provider, a.source_type, a.ticker,
                    a.reference_date, a.published_utc,
                    a.title, a.description,
                    a.article_url, a.image_url, a.author,
                    a.publisher_name, a.publisher_logo_url,
                    a.source_tier, a.dedup_group_id,
                    GROUP_CONCAT(at.ticker, ',') AS tickers_csv
                FROM articles a
                LEFT JOIN article_tickers at ON a.article_id = at.article_id AND at.is_canonical = 1
                WHERE a.article_id IN ({placeholders})
                GROUP BY a.article_id
                ORDER BY a.article_id""",
            eligible_ids,
        ).fetchall()

        for row in rows:
            (
                article_id, provider, source_type, scalar_ticker,
                reference_date, published_utc,
                title, description,
                article_url, image_url, author,
                publisher_name, publisher_logo_url,
                source_tier, dedup_group_id,
                tickers_csv,
            ) = row

            tickers = (
                sorted(set(t for t in (tickers_csv or "").split(",") if t))
                if tickers_csv
                else [scalar_ticker] if scalar_ticker else []
            )

            body = description or ""
            content_text = f"{title}\n{body}" if body else title
            content_hash = compute_content_hash(title, description)

            # UPSERT L1
            if _upsert_index_row(conn, f"{article_id}::l1", "l1", article_id,
                                 "article", content_hash, content_text,
                                 provider, source_type, source_tier or 4,
                                 tickers, reference_date, published_utc,
                                 publisher_name, publisher_logo_url,
                                 article_url, image_url, author, dedup_group_id):
                total_new += 1
                article_l1 += 1

            # L2 chunks
            if len(body) >= min_l2_chars:
                sentences = _split_sentences(body)
                for idx, sent in enumerate(sentences):
                    if not sent.strip():
                        continue
                    chunk_id = f"{article_id}::l2s{idx:04d}"
                    sent_hash = compute_content_hash(sent, None)
                    if _upsert_index_row(conn, chunk_id, "l2", article_id,
                                         "article", sent_hash, sent,
                                         provider, source_type, source_tier or 4,
                                         tickers, reference_date, published_utc,
                                         publisher_name, publisher_logo_url,
                                         article_url, image_url, author, dedup_group_id):
                        total_new += 1
                        article_l2 += 1

    conn.commit()

    # --- Filing records (existing behavior: is_rag_eligible=1 only) ---
    filing_l1, filing_l2 = _persist_filing_records(conn, min_l2_chars)
    total_new += (filing_l1 + filing_l2)

    # Count pending rows from DB (reflects actual state, not just new inserts)
    article_l1_db = conn.execute(
        "SELECT COUNT(*) FROM index_state WHERE source_kind='article' AND chunk_level='l1' AND status='pending'"
    ).fetchone()[0]
    article_l2_db = conn.execute(
        "SELECT COUNT(*) FROM index_state WHERE source_kind='article' AND chunk_level='l2' AND status='pending'"
    ).fetchone()[0]

    return {
        "article_l1_pending": article_l1_db,
        "article_l2_pending": article_l2_db,
        "filing_l1_pending": filing_l1,
        "filing_l2_pending": filing_l2,
        "total_new_rows": total_new,
        "eligible_article_count": len(eligible_ids),
    }


def _upsert_index_row(conn, chunk_id, chunk_level, corpus_item_id, source_kind,
                      content_hash, content_text, provider, source_type,
                      source_tier, tickers, reference_date, published_utc,
                      publisher_name, publisher_logo_url,
                      article_url, image_url, author, dedup_group_id) -> bool:
    """UPSERT one index_state row by chunk_id. Returns True if a new pending row was inserted."""
    import json

    existing = conn.execute(
        "SELECT content_hash, status FROM index_state WHERE chunk_id = ?",
        (chunk_id,)
    ).fetchall()

    # If existing row with same hash → skip
    for ex_hash, ex_status in existing:
        if ex_hash == content_hash and ex_status == 'pending':
            return False

    # Mark all existing rows for this chunk_id as stale
    conn.execute(
        "UPDATE index_state SET status = 'stale' WHERE chunk_id = ? AND status = 'pending'",
        (chunk_id,)
    )

    # Insert new pending row (REPLACE handles stale row with same chunk_id)
    conn.execute(
        """INSERT INTO index_state
           (chunk_id, chunk_level, corpus_item_id, source_kind,
            content_hash, content_text, status,
            provider, source_type, source_tier,
            tickers_json, reference_date, published_utc,
            publisher_name, publisher_logo_url,
            article_url, image_url, author, dedup_group_id)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (chunk_id, chunk_level, corpus_item_id, source_kind,
         content_hash, content_text,
         provider, source_type, source_tier,
         json.dumps(tickers) if tickers else "[]",
         reference_date, published_utc,
         publisher_name, publisher_logo_url,
         article_url, image_url, author, dedup_group_id),
    )
    return True


def _persist_filing_records(conn, min_l2_chars):
    """Build and persist filing index_state records. Returns (l1_count, l2_count)."""
    from catalyst_data.index_builder import compute_content_hash, _split_sentences

    rows = conn.execute("""
        SELECT
            f.filing_id, f.cik, f.ticker, f.form_type, f.filed_at,
            f.accession_number, f.url, f.source_tier,
            fd.text AS doc_text, fd.extraction_status, fd.document_type
        FROM filings f
        LEFT JOIN filing_documents fd
            ON f.filing_id = fd.filing_id
            AND fd.extraction_status = 'success'
        WHERE f.is_rag_eligible = 1
        ORDER BY f.filing_id, fd.document_type ASC
    """).fetchall()

    # Deduplicate: one row per filing_id (exhibit preferred)
    best_doc = {}
    null_rows = {}
    for row in rows:
        fid = row[0]
        if row[9] is not None:
            if fid not in best_doc:
                best_doc[fid] = row
        else:
            if fid not in null_rows:
                null_rows[fid] = row
    for fid, null_row in null_rows.items():
        if fid not in best_doc:
            best_doc[fid] = null_row

    l1_count = 0
    l2_count = 0

    for fid, row in best_doc.items():
        (filing_id, cik, ticker, form_type, filed_at,
         accession_number, url, source_tier, doc_text, extraction_status, doc_type) = row

        title = f"{form_type} filed {filed_at}"
        body = doc_text or ""
        content_hash = compute_content_hash(title, body)
        content_text = f"{title}\n{body}" if body else title

        import json
        tickers_json = json.dumps([ticker]) if ticker else "[]"

        if _upsert_index_row(conn, f"{filing_id}::l1", "l1", filing_id,
                             "filing", content_hash, content_text,
                             "sec", "sec_filing", source_tier or 1,
                             [ticker] if ticker else [], filed_at, filed_at,
                             None, None, url, None, None, None):
            l1_count += 1

        if extraction_status == "success" and len(body) >= min_l2_chars:
            sentences = _split_sentences(body)
            for idx, sent in enumerate(sentences):
                if not sent.strip():
                    continue
                chunk_id = f"{filing_id}::l2s{idx:04d}"
                sent_hash = compute_content_hash(sent, None)
                if _upsert_index_row(conn, chunk_id, "l2", filing_id,
                                     "filing", sent_hash, sent,
                                     "sec", "sec_filing", source_tier or 1,
                                     [ticker] if ticker else [], filed_at, filed_at,
                                     None, None, url, None, None, None):
                    l2_count += 1

    conn.commit()
    return l1_count, l2_count
