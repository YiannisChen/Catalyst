"""M4-6: token counter registry and fail-closed counters (Phase 3 §14).

Priority: exact registered tokenizer -> approved conservative proxy -> UTF-8
byte count plus per-message overhead as a labelled fail-closed upper bound.
``characters / 4`` estimation is forbidden. Exact token guarantees apply only
to registered tokenizers; arbitrary models run under the conservative byte
bound or are ineligible for the Analyst role (Final TSD §31 cl. 5).
"""
from __future__ import annotations

from typing import Literal, Protocol

CountingMode = Literal["EXACT", "REGISTERED_PROXY", "UTF8_BYTE_UPPER_BOUND"]


class TokenCounter(Protocol):
    """Labelled deterministic counter (Phase 3 §14)."""

    counter_id: str
    provider: str
    model_id: str
    tokenizer_id: str
    tokenizer_revision: str
    counting_mode: CountingMode
    message_overhead_policy_version: str

    def count_text(self, text: str) -> int:
        ...

    def truncate_with_offsets(self, text: str, max_tokens: int) -> tuple[str, int, int]:
        """Return (truncated_text, original_token_count, included_token_count).

        Truncation operates on tokenizer offsets; it never silently slices
        characters or splits a UTF-8 code point (Phase 3 §17).
        """
        ...


class UTF8ByteUpperBoundCounter:
    """Conservative fail-closed counter: one token per UTF-8 byte plus a
    configured per-message overhead (Phase 3 §14; Final TSD §31 cl. 5)."""

    counting_mode: CountingMode = "UTF8_BYTE_UPPER_BOUND"

    def __init__(
        self,
        *,
        provider: str,
        model_id: str,
        message_overhead_tokens: int = 0,
        message_overhead_policy_version: str = "utf8_upper_bound_v1",
        tokenizer_id: str = "utf8-byte-upper-bound",
        tokenizer_revision: str = "v1",
    ) -> None:
        if message_overhead_tokens < 0:
            raise ValueError("message overhead tokens must be non-negative")
        self.counter_id = f"{provider}:{model_id}:utf8-byte-upper-bound"
        self.provider = provider
        self.model_id = model_id
        self.tokenizer_id = tokenizer_id
        self.tokenizer_revision = tokenizer_revision
        self.message_overhead_tokens = message_overhead_tokens
        self.message_overhead_policy_version = message_overhead_policy_version

    def count_text(self, text: str) -> int:
        return len(text.encode("utf-8")) + self.message_overhead_tokens

    def truncate_with_offsets(self, text: str, max_tokens: int) -> tuple[str, int, int]:
        original = len(text.encode("utf-8"))
        if max_tokens <= self.message_overhead_tokens:
            return "", original, 0
        budget = max_tokens - self.message_overhead_tokens
        if original <= budget:
            return text, original, original
        included = 0
        prefix: list[str] = []
        for character in text:
            next_count = included + len(character.encode("utf-8"))
            if next_count > budget:
                break
            prefix.append(character)
            included = next_count
        return "".join(prefix), original, included


__all__ = [
    "CountingMode",
    "TokenCounter",
    "UTF8ByteUpperBoundCounter",
]
