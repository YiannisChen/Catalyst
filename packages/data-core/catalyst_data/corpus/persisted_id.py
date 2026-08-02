"""Pre-B6 canonical persisted document ID validator.

Contract: a persisted document ID is a 64-character lowercase hex string
matching ``[0-9a-f]{64}``. Anything else (bare, uppercase, non-hex,
short, None) is not a valid persisted ID; legacy rows remain outside the
active Pre-B6 filing_v3 corpus.
"""

from __future__ import annotations


def is_valid_persisted_document_id(value: object) -> bool:
    """Return True if *value* is a valid persisted document ID.

    A valid ID is exactly 64 lowercase hex characters.
    """
    if not isinstance(value, str):
        return False
    if len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)
