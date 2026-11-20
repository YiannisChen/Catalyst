"""Deterministic token-aware chunking for the ``news_v2`` profile."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
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


def _token_end_for_char(token_end_offsets: list[int], char_end: int) -> int:
    return min(len(token_end_offsets), bisect_left(token_end_offsets, char_end) + 1)


def _find_boundaries(text: str, offsets: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
    token_end_offsets = [token_end for _, token_end in offsets]
    paragraph_chars = [match.start() for match in re.finditer(r"\n\n", text)]
    if text:
        paragraph_chars.append(len(text))
    sentence_chars = [match.end() for match in _SENTENCE_END.finditer(text)]
    paragraphs = sorted({
        _token_end_for_char(token_end_offsets, end) for end in paragraph_chars
    })
    sentences = sorted({
        _token_end_for_char(token_end_offsets, end) for end in sentence_chars
    })
    return paragraphs, sentences


def _select_boundary(boundaries: list[int], lower: int, hard: int, target: int) -> int | None:
    first = bisect_left(boundaries, lower)
    stop = bisect_right(boundaries, hard, lo=first)
    if first == stop:
        return None
    pivot = bisect_left(boundaries, target, lo=first, hi=stop)
    candidate_indexes = [pivot] if pivot < stop else []
    if pivot > first:
        candidate_indexes.append(pivot - 1)
    return min(
        (boundaries[index] for index in candidate_indexes),
        key=lambda boundary: (abs(boundary - target), boundary),
    )


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


def _iter_chunk_text(
    *,
    document: dict[str, Any],
    text: str,
    prefix_title: str,
    profile_version: str,
    section_key: str,
    short_document_has_no_prefix: bool,
    section_parse_degraded: bool = False,
) -> Iterator[ChunkResult]:
    normalized = _normalize_text(text)
    if not normalized:
        return

    token_ids, offsets = _encoding(normalized)
    token_count = len(token_ids)
    metadata_hash = _metadata_hash(document, profile_version)
    document_id = document["document_id"]

    # Short document, no prefix path
    if token_count <= MAX_TOKENS and short_document_has_no_prefix:
        content_text = normalized
        ordinal = f"{1:0{ORDINAL_WIDTH}d}"
        yield ChunkResult(
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
            boundary_kind="document_end",
            body_token_start=0,
            body_token_end=token_count,
            body_overlap_tokens=0,
            prefix_token_count=0,
            prefix_truncated=False,
            section_parse_degraded=section_parse_degraded,
        )
        return

    prefix, prefix_count, prefix_truncated = _prefix(prefix_title)
    body_target = max(1, TARGET_TOKENS - prefix_count)
    body_max = max(1, MAX_TOKENS - prefix_count)
    paragraphs, sentences = _find_boundaries(normalized, offsets)
    paragraph_set = set(paragraphs)
    sentence_set = set(sentences)

    # --- token-fit helper ---
    def _find_fitting_end(
        _start: int,
        _candidate_end: int,
        _candidate_kind: str,
    ) -> tuple[int, str]:
        """Find largest end <= _candidate_end where prefix+body fits.

        Checks the semantic candidate first, then scans every smaller token
        boundary in reverse. Prefix is always included.
        """
        body_slice = normalized[offsets[_start][0]:offsets[_candidate_end - 1][1]]
        if count_tokens(f"{prefix}{body_slice}") <= MAX_TOKENS:
            return _candidate_end, _candidate_kind

        # Concatenated tokenizer counts are not assumed to be monotonic as the
        # end boundary moves. Scan every candidate boundary in reverse so the
        # first fitting result is the largest one we actually measured.
        for end in range(_candidate_end - 1, _start, -1):
            body = normalized[offsets[_start][0]:offsets[end - 1][1]]
            content_text = f"{prefix}{body}"
            if count_tokens(content_text) > MAX_TOKENS:
                continue
            if end in paragraph_set:
                kind = "paragraph"
            elif end in sentence_set:
                kind = "sentence"
            else:
                kind = "token_fallback"
            if count_tokens(content_text) > MAX_TOKENS:
                raise ValueError("fitted chunk exceeded token limit")
            return end, kind
        return _start, "token_fallback"
    # --- end token-fit helper ---

    prev_actual_end = 0
    start = 0
    index = 0

    while start < token_count:
        index += 1
        # --- choose semantic candidate end ---
        hard = min(token_count, start + body_max)
        target = min(token_count, start + body_target)
        lower = max(start + 1, target - MAX_OVERLAP)

        if token_count <= hard:
            candidate_end, kind = token_count, "document_end"
        else:
            candidate_end = _select_boundary(paragraphs, lower, hard, target)
            kind = "paragraph"
            if candidate_end is None:
                candidate_end = _select_boundary(sentences, lower, hard, target)
                kind = "sentence"
            if candidate_end is None:
                candidate_end, kind = target, "token_fallback"

        # --- reduce candidate_end until prefix+body fits ---
        actual_end, actual_kind = _find_fitting_end(start, candidate_end, kind)

        # Hard invariant: actual_end must be > start.  _prefix caps at 64 << 384
        # so at least one body token MUST fit.  If this fails, the tokenizer
        # model or input is fundamentally misconfigured.
        if actual_end <= start:
            raise ValueError(
                f"Chunk invariant violation: cannot fit any body token at "
                f"start={start} within MAX_TOKENS={MAX_TOKENS}"
            )

        # --- build content ---
        body = normalized[offsets[start][0]:offsets[actual_end - 1][1]]
        content_text = f"{prefix}{body}"
        if count_tokens(content_text) > MAX_TOKENS:
            raise ValueError("semantic window exceeded 384-token contract")
        content_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()

        # --- overlap: intersection with previous chunk ---
        actual_overlap = max(0, prev_actual_end - start)
        prev_actual_end = actual_end

        if actual_end >= token_count:
            actual_kind = "document_end"

        ordinal = f"{index:0{ORDINAL_WIDTH}d}"
        yield ChunkResult(
            chunk_id=f"{document_id}:{profile_version}:{section_key}:{ordinal}",
            document_id=document_id,
            chunk_profile_version=profile_version,
            section_key=section_key,
            ordinal=ordinal,
            content_text=content_text,
            content_hash=content_hash,
            metadata_hash=metadata_hash,
            source_class=document.get("source_class", "reported_news"),
            available_at=document.get("available_at", ""),
            ticker_associations=document.get("ticker_associations", "[]"),
            eligibility=document.get("eligibility", "eligible"),
            boundary_kind=actual_kind,
            body_token_start=start,
            body_token_end=actual_end,
            body_overlap_tokens=actual_overlap,
            prefix_token_count=prefix_count,
            prefix_truncated=prefix_truncated,
            section_parse_degraded=section_parse_degraded,
        )

        if actual_end >= token_count:
            break

        # --- next start (with overlap) ---
        start = max(start + 1, actual_end - MAX_OVERLAP)



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
    """Legacy list API over the bounded streaming chunk iterator."""
    return list(
        _iter_chunk_text(
            document=document,
            text=text,
            prefix_title=prefix_title,
            profile_version=profile_version,
            section_key=section_key,
            short_document_has_no_prefix=short_document_has_no_prefix,
            section_parse_degraded=section_parse_degraded,
        )
    )


class NewsV2Profile:
    profile_version = "news_v2"

    def iter_chunks(self, document: dict[str, Any]) -> Iterator[ChunkResult]:
        title = document.get("title", "")
        description = document.get("description") or ""
        text = f"{title}\n{description}" if description else title
        return _iter_chunk_text(
            document=document,
            text=text,
            prefix_title=title,
            profile_version=self.profile_version,
            section_key="body",
            short_document_has_no_prefix=True,
        )

    def chunk(self, document: dict[str, Any]) -> list[ChunkResult]:
        return list(self.iter_chunks(document))
