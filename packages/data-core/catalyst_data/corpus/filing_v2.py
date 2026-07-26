"""Section-aware deterministic chunking for 8-K and EX-99.x documents."""

from __future__ import annotations

import re
from typing import Any

from .news_v2 import _chunk_text, _normalize_text
from .profile import ChunkResult

_ITEM_HEADING = re.compile(r"(?im)^\s*item\s+(\d{1,2}\.\d{2})\b[^\n]*")


def _section_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9.]+", "_", value.lower()).strip("_")
    return normalized or "unknown_000"


def _raw_sections(filing_type: str, raw_text: str) -> tuple[list[dict[str, str]], bool]:
    normalized = _normalize_text(raw_text)
    if not normalized:
        return [], False

    if filing_type.upper().startswith("EX-99"):
        return [{
            "section_key": _section_key(filing_type).replace(".", "_"),
            "heading": filing_type,
            "text": normalized,
        }], False

    matches = list(_ITEM_HEADING.finditer(normalized)) if filing_type.upper() == "8-K" else []
    if not matches:
        return [{"section_key": "unknown_000", "heading": "", "text": normalized}], True

    sections: list[dict[str, str]] = []
    preamble = normalized[:matches[0].start()].strip()
    if preamble:
        sections.append({"section_key": "unknown_000", "heading": "", "text": preamble})
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        sections.append({
            "section_key": f"item_{match.group(1)}",
            "heading": match.group(0).strip(),
            "text": normalized[match.end():end].strip(),
        })
    return sections, False


class FilingV2Profile:
    profile_version = "filing_v2"

    def chunk(self, document: dict[str, Any]) -> list[ChunkResult]:
        sections = document.get("sections")
        degraded = False
        if not sections:
            sections, degraded = _raw_sections(
                document.get("filing_type", ""),
                document.get("raw_text", ""),
            )

        filing_document = dict(document)
        filing_document.setdefault("source_class", "official_government")
        seen: dict[str, int] = {}
        chunks: list[ChunkResult] = []
        for section in sections or []:
            base_key = _section_key(section.get("section_key", "unknown_000"))
            seen[base_key] = seen.get(base_key, 0) + 1
            key = base_key if seen[base_key] == 1 else f"{base_key}_{seen[base_key]:02d}"
            heading = _normalize_text(section.get("heading") or section.get("title", ""))
            body = _normalize_text(section.get("text", ""))
            if not body:
                continue
            section_text = f"{heading}\n{body}" if heading else body
            chunks.extend(_chunk_text(
                document=filing_document,
                text=section_text,
                prefix_title=heading,
                profile_version=self.profile_version,
                section_key=key,
                short_document_has_no_prefix=True,
                section_parse_degraded=degraded,
            ))
        return chunks
