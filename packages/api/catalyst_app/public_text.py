"""Fail-closed validation for bounded public diagnostic text."""
from __future__ import annotations

import re
import unicodedata

MAX_SAFE_PUBLIC_TEXT_CHARACTERS = 1_000
_UNSAFE_PUBLIC_TEXT = re.compile(
    r"(?:api[_ -]?key|provider[_ -]?key|authorization|bearer\s+\S+|"
    r"raw[_ -]?(?:provider[_ -]?)?response|provider\s+response)\s*(?:=|:|\b)|"
    r"\bsk-[A-Za-z0-9_-]{16,}\b",
    re.IGNORECASE,
)


def validate_safe_public_text(value: str | None) -> str | None:
    """Reject text that could disclose credentials or raw provider content.

    Callers configure Pydantic with ``hide_input_in_errors`` so the rejected
    public value is not reflected in serialized validation diagnostics.
    """
    if value is None:
        return None
    if not value or len(value) > MAX_SAFE_PUBLIC_TEXT_CHARACTERS:
        raise ValueError("public diagnostic text must be non-empty and bounded")
    if _UNSAFE_PUBLIC_TEXT.search(value):
        raise ValueError("public diagnostic text contains a prohibited secret or raw-provider pattern")
    return value


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*|[0-9]+")
_ENUM = re.compile(r"^[A-Z0-9][A-Z0-9 _./:#-]*$")
_HASH = re.compile(r"^[0-9a-fA-F-]{16,}$")
_NUMBER = re.compile(r"^[0-9.,%+\-]+$")
_URL = re.compile(r"^(?:https?://|//|www\.|/)\S*$", re.IGNORECASE)
_IDLIKE = re.compile(r"[:/_#=]")


def is_sensitive_metadata_sentence(value: str) -> bool:
    """Whether a metadata candidate is sentence-like enough to be a leak.

    A source/prompt/investigation sentence must be at least 12 normalized
    characters and three words, or at least eight CJK characters. Enum values,
    tickers, ids, hashes, numbers and URLs are not sentences.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if _URL.match(text) or _HASH.fullmatch(text) or _NUMBER.fullmatch(text):
        return False
    if _ENUM.fullmatch(text):
        return False
    if " " not in text and _IDLIKE.search(text):
        return False
    cjk_count = len(_CJK.findall(text))
    if cjk_count >= 8:
        return True
    normalized = re.sub(
        r"[\W_]+",
        "",
        unicodedata.normalize("NFKC", text).casefold(),
        flags=re.UNICODE,
    )
    if len(normalized) < 12:
        return False
    return len(_WORD.findall(text)) >= 3
