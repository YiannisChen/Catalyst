"""SEC document reparse with versioned parser identity (M3-4).

``reparse_filing`` runs the versioned primary-document extraction over raw SEC
archive bytes and reports non-empty extraction, document hash, parse quality,
and section keys/ordinals with per-section degradation flags.
``section_parse_degraded`` is a separately reported quality flag: it constrains
section-exact serving/citation claims but never folds into the DATA-01
primary-document gate (Final Migration TSD §5.3).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from catalyst_data.corpus.filing_v3 import _section_key
from catalyst_data.corpus.news_v2 import _normalize_text
from catalyst_data.sec.extract import (
    SEC_EXTRACT_PARSER_VERSION,
    extract_document_text,
)


# Section-item regex for reparse: supports "Item 1", "Item 1.01", "Item 1A"
# headings at line start (10-K lettered sub-items included).
_SECTION_ITEM_RE = re.compile(
    r"(?im)^\s*item\s+(\d{1,2}(?:\.\d{1,2})?[a-z]?)\b[^\n]*"
)


@dataclass(frozen=True)
class ReparsedSection:
    section_key: str
    ordinal: str  # 4-digit zero-padded
    section_parse_degraded: bool


@dataclass(frozen=True)
class FilingParseResult:
    accession: str
    parser_version: str
    primary_document_extracted: bool
    document_hash: str | None
    parse_quality: str  # full | degraded | not_applicable | failed
    sections: tuple[ReparsedSection, ...]


def _split_sections(text: str) -> tuple[ReparsedSection, ...]:
    matches = list(_SECTION_ITEM_RE.finditer(text))
    if not matches:
        return (
            ReparsedSection(
                section_key="unknown_000",
                ordinal="0001",
                section_parse_degraded=True,
            ),
        )
    sections: list[ReparsedSection] = []
    ordinal = 1
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        if not body:
            continue
        sections.append(
            ReparsedSection(
                section_key=_section_key(f"item_{match.group(1)}"),
                ordinal=f"{ordinal:04d}",
                section_parse_degraded=False,
            )
        )
        ordinal += 1
    if not sections:
        return (
            ReparsedSection(
                section_key="unknown_000",
                ordinal="0001",
                section_parse_degraded=True,
            ),
        )
    return tuple(sections)


def reparse_filing(
    raw_bytes: bytes,
    accession: str,
    parser_version: str = SEC_EXTRACT_PARSER_VERSION,
) -> FilingParseResult:
    """Reparse one SEC primary document with the versioned parser identity.

    Deterministic: the same input + parser version always produces the same
    document hash and section structure.
    """
    outcome = extract_document_text(
        raw_bytes,
        content_type="text/html",
        is_primary=True,
        parser_version=parser_version,
    )
    if outcome.status != "success":
        parse_quality = (
            "not_applicable"
            if outcome.status in ("empty_extract", "mandatory_failed")
            else "failed"
        )
        return FilingParseResult(
            accession=accession,
            parser_version=parser_version,
            primary_document_extracted=False,
            document_hash=None,
            parse_quality=parse_quality,
            sections=(),
        )

    text = _normalize_text(outcome.text)
    document_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    sections = _split_sections(text)
    degraded = any(section.section_parse_degraded for section in sections)
    return FilingParseResult(
        accession=accession,
        parser_version=parser_version,
        primary_document_extracted=True,
        document_hash=document_hash,
        parse_quality="degraded" if degraded else "full",
        sections=sections,
    )


__all__ = ["FilingParseResult", "ReparsedSection", "reparse_filing"]
