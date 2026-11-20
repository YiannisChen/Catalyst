"""Text normalization helpers for corpus indexing."""

from __future__ import annotations

import hashlib
import re
import unicodedata


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


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


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
