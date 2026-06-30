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
import sqlite3
import unicodedata
from typing import Any


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


def build_index_records(
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


def index_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return aggregate summary of built index records."""
    l1 = [r for r in records if r["chunk_level"] == "l1"]
    l2 = [r for r in records if r["chunk_level"] == "l2"]

    l2_eligible_ids = {r["article_id"] for r in l2}
    total_article_ids = {r["article_id"] for r in l1}

    per_tier: dict[int, dict[str, int]] = {}
    for r in l1:
        tier = r["source_tier"]
        if tier not in per_tier:
            per_tier[tier] = {"l1": 0, "l2": 0}
        per_tier[tier]["l1"] += 1

    for r in l2:
        tier = r["source_tier"]
        if tier not in per_tier:
            per_tier[tier] = {"l1": 0, "l2": 0}
        per_tier[tier]["l2"] += 1

    return {
        "l1_count": len(l1),
        "l2_count": len(l2),
        "l2_eligible_count": len(l2_eligible_ids),
        "l2_eligible_pct": (
            round(100 * len(l2_eligible_ids) / len(total_article_ids), 2)
            if total_article_ids else 0
        ),
        "would_embed_count": len(l1) + len(l2),
        "per_tier": per_tier,
    }


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
