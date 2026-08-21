"""Fail-closed validation for bounded public diagnostic text."""
from __future__ import annotations

import re

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
