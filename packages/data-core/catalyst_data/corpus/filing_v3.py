"""filing_v3 section-aware chunking (max 384 / target 320 / overlap 48)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from catalyst_data.corpus.news_v2 import (
    MAX_OVERLAP,
    MAX_PREFIX_TOKENS,
    MAX_TOKENS,
    TARGET_TOKENS,
    _iter_chunk_text,
    _normalize_text,
)
from catalyst_data.corpus.profile import ChunkResult

# Re-export for tests
assert MAX_TOKENS == 384 and TARGET_TOKENS == 320 and MAX_OVERLAP == 48
assert MAX_PREFIX_TOKENS == 64

_ITEM_RE = re.compile(r"(?im)^\s*item\s+(\d{1,2}(?:\.\d{2})?)\b[^\n]*")
_PART_RE = re.compile(r"(?im)^\s*part\s+([ivx]+)\b[^\n]*")


def _section_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9.]+", "_", value.lower()).strip("_")
    return normalized or "unknown_000"


class FilingV3Profile:
    profile_version = "filing_v3"

    def iter_chunks(self, document: dict[str, Any]) -> Iterator[ChunkResult]:
        form = str(document.get("filing_type") or document.get("form_type") or "")
        role = str(document.get("document_role") or "")
        raw = document.get("raw_text") or ""
        text = _normalize_text(raw)
        if not text:
            return

        degraded = False
        sections: list[dict[str, str]] = []

        if role.startswith("exhibit_99") or form.upper().startswith("EX-99"):
            sections = [
                {
                    "section_key": _section_key(role or form),
                    "heading": role or form,
                    "text": text,
                }
            ]
        else:
            matches = list(_ITEM_RE.finditer(text))
            if not matches:
                sections = [{"section_key": "unknown_000", "heading": "", "text": text}]
                degraded = True
            else:
                preamble = text[: matches[0].start()].strip()
                if preamble:
                    sections.append(
                        {"section_key": "unknown_000", "heading": "", "text": preamble}
                    )
                for i, m in enumerate(matches):
                    end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                    sections.append(
                        {
                            "section_key": f"item_{m.group(1)}",
                            "heading": m.group(0).strip(),
                            "text": text[m.end() : end].strip(),
                        }
                    )

        filing_document = dict(document)
        filing_document.setdefault("source_class", "official_government")
        seen: dict[str, int] = {}
        for section in sections:
            if not section.get("text"):
                continue
            base = _section_key(section["section_key"])
            seen[base] = seen.get(base, 0) + 1
            key = base if seen[base] == 1 else f"{base}_{seen[base]:02d}"
            heading = _normalize_text(section.get("heading") or "")
            body = _normalize_text(section["text"])
            yield from _iter_chunk_text(
                document=filing_document,
                text=body,
                prefix_title=heading,
                profile_version=self.profile_version,
                section_key=key,
                short_document_has_no_prefix=not heading,
                section_parse_degraded=degraded,
            )

    def chunk(self, document: dict[str, Any]) -> list[ChunkResult]:
        return list(self.iter_chunks(document))
