"""Deterministic token-aware chunking for the ``news_v2`` profile."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from .profile import ChunkResult
from .tokenizer import count_tokens, tokenize_with_offsets

MAX_TOKENS = 384
TARGET_TOKENS = 320
MAX_OVERLAP = 48
MAX_PREFIX_TOKENS = 64
ORDINAL_WIDTH = 4

_SENTENCE_END = re.compile(r"[.!?。！？][)\"'\]】）}」』]*?(?=\s|$)")


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line.rstrip(" \t")) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _metadata_hash(document: dict[str, Any], profile_version: str) -> str:
    metadata = {
        "document_id": document["document_id"],
        "ticker_associations": document.get("ticker_associations", "[]"),
        "available_at": document.get("available_at", ""),
        "source_class": document.get("source_class", "reported_news"),
        "dedup_cluster_id": document.get("dedup_cluster_id"),
        "representative_document_id": document.get("representative_document_id"),
        "eligibility": document.get("eligibility", "eligible"),
        "chunk_profile_version": profile_version,
    }
    return hashlib.sha256(_canonical_json(metadata)).hexdigest()


def _encoding(text: str) -> tuple[list[int], list[tuple[int, int]]]:
    return tokenize_with_offsets(text)


def _token_end_for_char(offsets: list[tuple[int, int]], char_end: int) -> int:
    for index, (_, token_end) in enumerate(offsets):
        if token_end >= char_end:
            return index + 1
    return len(offsets)


def _find_boundaries(text: str, offsets: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
    paragraph_chars = [match.start() for match in re.finditer(r"\n\n", text)]
    if text:
        paragraph_chars.append(len(text))
    sentence_chars = [match.end() for match in _SENTENCE_END.finditer(text)]
    paragraphs = sorted({_token_end_for_char(offsets, end) for end in paragraph_chars})
    sentences = sorted({_token_end_for_char(offsets, end) for end in sentence_chars})
    return paragraphs, sentences


def _select_boundary(boundaries: list[int], lower: int, hard: int, target: int) -> int | None:
    candidates = [boundary for boundary in boundaries if lower <= boundary <= hard]
    return min(candidates, key=lambda boundary: (abs(boundary - target), boundary)) if candidates else None


def _prefix(title: str) -> tuple[str, int, bool]:
    normalized = _normalize_text(title)
    if not normalized:
        return "", 0, False

    _, offsets = _encoding(normalized)
    candidate = normalized
    truncated = False
    if len(offsets) > MAX_PREFIX_TOKENS:
        candidate = normalized[:offsets[MAX_PREFIX_TOKENS - 1][1]]
        truncated = True

    while candidate and count_tokens(f"{candidate}\n") > MAX_PREFIX_TOKENS:
        _, candidate_offsets = _encoding(candidate)
        candidate = candidate[:candidate_offsets[-2][1]] if len(candidate_offsets) > 1 else ""
        truncated = True

    prefix = f"{candidate}\n" if candidate else ""
    return prefix, count_tokens(prefix), truncated


def _chunk_text(
    *,
    document: dict[str, Any],
    text: str,
    prefix_title: str,
    profile_version: str,
    section_key: str,
    short_document_has_no_prefix: bool,
    section_parse_degraded: bool = False,
) -> list[ChunkResult]:
    normalized = _normalize_text(text)
    if not normalized:
        return []

    token_ids, offsets = _encoding(normalized)
    token_count = len(token_ids)
    if token_count <= MAX_TOKENS and short_document_has_no_prefix:
        windows = [(0, token_count, "document_end", 0)]
        prefix, prefix_count, prefix_truncated = "", 0, False
    else:
        prefix, prefix_count, prefix_truncated = _prefix(prefix_title)
        body_target = max(1, TARGET_TOKENS - prefix_count)
        body_max = max(1, MAX_TOKENS - prefix_count)
        paragraphs, sentences = _find_boundaries(normalized, offsets)
        windows: list[tuple[int, int, str, int]] = []
        start = 0
        while start < token_count:
            target = min(token_count, start + body_target)
            hard = min(token_count, start + body_max)
            lower = max(start + 1, target - MAX_OVERLAP)
            if token_count <= hard:
                end, kind = token_count, "document_end"
            else:
                end = _select_boundary(paragraphs, lower, hard, target)
                kind = "paragraph"
                if end is None:
                    end = _select_boundary(sentences, lower, hard, target)
                    kind = "sentence"
                if end is None:
                    end, kind = target, "token_fallback"
            next_start = token_count if end == token_count else max(start + 1, end - MAX_OVERLAP)
            windows.append((start, end, kind, 0 if end == token_count else end - next_start))
            start = next_start

    metadata_hash = _metadata_hash(document, profile_version)
    chunks: list[ChunkResult] = []
    for index, (start, end, boundary_kind, overlap) in enumerate(windows, start=1):
        body = normalized[offsets[start][0]:offsets[end - 1][1]]
        content_text = f"{prefix}{body}" if prefix else body
        if count_tokens(content_text) > MAX_TOKENS:
            raise ValueError("semantic window exceeded 384-token contract")
        ordinal = f"{index:0{ORDINAL_WIDTH}d}"
        document_id = document["document_id"]
        chunks.append(ChunkResult(
            chunk_id=f"{document_id}:{profile_version}:{section_key}:{ordinal}",
            document_id=document_id,
            chunk_profile_version=profile_version,
            section_key=section_key,
            ordinal=ordinal,
            content_text=content_text,
            content_hash=hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
            metadata_hash=metadata_hash,
            source_class=document.get("source_class", "reported_news"),
            available_at=document.get("available_at", ""),
            ticker_associations=document.get("ticker_associations", "[]"),
            eligibility=document.get("eligibility", "eligible"),
            boundary_kind=boundary_kind,
            body_token_start=start,
            body_token_end=end,
            body_overlap_tokens=overlap,
            prefix_token_count=prefix_count,
            prefix_truncated=prefix_truncated,
            section_parse_degraded=section_parse_degraded,
        ))
    return chunks


class NewsV2Profile:
    profile_version = "news_v2"

    def chunk(self, document: dict[str, Any]) -> list[ChunkResult]:
        title = document.get("title", "")
        description = document.get("description") or ""
        text = f"{title}\n{description}" if description else title
        return _chunk_text(
            document=document,
            text=text,
            prefix_title=title,
            profile_version=self.profile_version,
            section_key="body",
            short_document_has_no_prefix=True,
        )
