"""Tests for pinned BGE-M3 tokenizer accessor."""
from __future__ import annotations

import re


def test_tokenizer_is_pinned_revision():
    """Token count must match pinned BGE-M3 revision deterministically."""
    from catalyst_data.corpus.tokenizer import (
        get_tokenizer, count_tokens, TOKENIZER_MODEL_ID, TOKENIZER_REVISION,
    )

    assert TOKENIZER_MODEL_ID == "BAAI/bge-m3"
    assert re.fullmatch(r"[0-9a-f]{40}", TOKENIZER_REVISION)
    tok = get_tokenizer()
    assert tok is not None

    text = "Hello world, this is a test."
    count = count_tokens(text)
    assert count > 0
    # Deterministic: same text -> same count
    assert count == count_tokens(text)


def test_tokenizer_revision_in_manifest():
    """Tokenizer model ID and bare revision are persisted separately."""
    from catalyst_data.corpus.tokenizer import (
        TOKENIZER_MODEL_ID, TOKENIZER_REVISION, tokenizer_identity,
    )
    manifest = tokenizer_identity()
    assert manifest == {
        "model_id": TOKENIZER_MODEL_ID,
        "revision": TOKENIZER_REVISION,
    }


def test_tokenizer_exposes_exact_offset_mapping():
    """Chunk profiles can slice source text without decoding token IDs."""
    from catalyst_data.corpus.tokenizer import tokenize_with_offsets

    token_ids, offsets = tokenize_with_offsets("alpha-beta 中文")
    assert len(token_ids) == len(offsets) > 0
    assert all(0 <= start < end <= len("alpha-beta 中文") for start, end in offsets)
