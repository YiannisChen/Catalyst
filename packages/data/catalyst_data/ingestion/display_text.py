"""Output-only cleanup for human-readable news text."""
from __future__ import annotations

import html
import re

_MOJIBAKE_MARKERS = ("â€", "â\x80", "Ã", "Â")
_WHITESPACE = re.compile(r"\s+")


def _marker_count(value: str) -> int:
    return sum(value.count(marker) for marker in _MOJIBAKE_MARKERS)


def normalize_display_text(value: str | None) -> str | None:
    """Repair common display encoding artifacts without mutating stored text."""
    if value is None:
        return None
    text = value
    if _marker_count(text):
        original_markers = _marker_count(text)
        for encoding in ("cp1252", "latin-1"):
            try:
                repaired = text.encode(encoding, errors="strict").decode("utf-8", errors="strict")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if _marker_count(repaired) < original_markers:
                text = repaired
                break
        else:
            # A cp1252-decoded C1 control (commonly U+009D) can make the
            # whole string unencodable even when the remaining text is valid.
            chunks = []
            for char in text:
                try:
                    chunks.append(char.encode("cp1252", errors="strict"))
                except UnicodeEncodeError:
                    try:
                        chunks.append(char.encode("latin-1", errors="strict"))
                    except UnicodeEncodeError:
                        chunks = []
                        break
            if chunks:
                try:
                    repaired = b"".join(chunks).decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    pass
                else:
                    if _marker_count(repaired) < original_markers:
                        text = repaired
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = text.replace("\xa0", " ")
    return _WHITESPACE.sub(" ", text).strip()
