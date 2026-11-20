"""Pinned BGE-M3 tokenizer accessor.

Uses a pinned revision so token counts are deterministic across runs.
The tokenizer is lazy-loaded on first access and cached in the module.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizer

TOKENIZER_MODEL_ID: str = "BAAI/bge-m3"
TOKENIZER_REVISION: str = "5617a9f61b028005a4858fdac845db406aefb181"

_tokenizer: "PreTrainedTokenizer | None" = None


def get_tokenizer() -> "PreTrainedTokenizer":
    """Return the pinned BGE-M3 fast tokenizer, loading it on first call."""
    global _tokenizer
    if _tokenizer is None:
        from transformers import AutoTokenizer

        model_source = _local_snapshot_path() or TOKENIZER_MODEL_ID
        _tokenizer = AutoTokenizer.from_pretrained(
            model_source,
            revision=TOKENIZER_REVISION,
            trust_remote_code=False,
        )
    return _tokenizer


def _local_snapshot_path() -> str | None:
    cache_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    snapshot = cache_home / "hub" / "models--BAAI--bge-m3" / "snapshots" / TOKENIZER_REVISION
    return str(snapshot) if snapshot.exists() else None


def count_tokens(text: str) -> int:
    """Return the number of tokens for *text* using the pinned tokenizer.

    Uses ``add_special_tokens=False`` — no [CLS]/[SEP] tokens are added.
    """
    tok = get_tokenizer()
    ids = tok.encode(text, add_special_tokens=False)
    return len(ids)


def tokenize_with_offsets(text: str) -> tuple[list[int], list[tuple[int, int]]]:
    """Return token IDs and exact source character offsets."""
    encoded = get_tokenizer()(
        text,
        add_special_tokens=False,
        return_offsets_mapping=True,
    )
    return (
        list(encoded["input_ids"]),
        [tuple(offset) for offset in encoded["offset_mapping"]],
    )


def tokenizer_identity() -> dict[str, str]:
    """Return the manifest-ready tokenizer identity dict."""
    return {
        "model_id": TOKENIZER_MODEL_ID,
        "revision": TOKENIZER_REVISION,
    }
