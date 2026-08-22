"""News full-body recovery + state-bound content hash (M3-5; §F/§A.2).

``recover_body`` determines ``content_state``: FULL_TEXT only with an
authentic material body from ``raw_payload["body"]``
(``len(body.strip()) >= RAG_MIN_CHAR_COUNT``); otherwise TITLE_ONLY /
METADATA_ONLY / EMPTY / FAILED. Provider description/summary/headline/lede/
snippet/title are snippets/ledes and never mint FULL_TEXT (Batch A A1).
Content hashes are state-bound (execution-lock §A.2): EMPTY/FAILED mint no
content version.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from catalyst_data.articles.url_normalize import NormalizedUrl
from catalyst_data.canonical.ids import canonical_json_bytes
from catalyst_data.config import RAG_MIN_CHAR_COUNT
from catalyst_data.corpus.news_v2 import _normalize_text

BODY_NORMALIZER_VERSION = "news_body_v1"


@dataclass(frozen=True)
class BodyRecoveryResult:
    content_state: str  # FULL_TEXT|TITLE_ONLY|METADATA_ONLY|EMPTY|FAILED
    body_text: str | None
    content_hash: str | None  # None when EMPTY/FAILED (no content version)


def _content_hash_for_state(content_state: str, **fields: object) -> str:
    payload: dict[str, object] = {"content_state": content_state}
    payload.update(fields)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _terminal_failure(raw_payload: object) -> bool:
    return isinstance(raw_payload, Mapping) and bool(
        raw_payload.get("content_failed")
    )


def _metadata_only_hash(
    *, title: str | None, description: str | None, canonical_url: str | None
) -> str:
    """Existing METADATA_ONLY contract (§A.2): state + title/description/URL."""
    return _content_hash_for_state(
        "METADATA_ONLY",
        normalized_title=title,
        normalized_description=description,
        canonical_url=canonical_url,
    )


def recover_body(
    article_row: Mapping[str, Any], raw_payload: object
) -> BodyRecoveryResult:
    """Classify one article's recovered content state and state-bound hash.

    ``raw_payload`` is the provider payload; ``content_failed=True`` in it marks
    a terminal provider/extraction failure (FAILED). Only
    ``raw_payload["body"]`` (a str) may mint FULL_TEXT; description text is a
    snippet/lede, never FULL_TEXT by itself (execution-lock §F; Batch A A1).
    """
    if _terminal_failure(raw_payload):
        return BodyRecoveryResult("FAILED", None, None)

    title = _normalize_text(article_row.get("title") or "") or None
    raw_description = article_row.get("description")
    canonical_url = article_row.get("article_url") or None

    # 1. Authentic body: raw_payload["body"] is the only FULL_TEXT-capable key.
    if isinstance(raw_payload, Mapping) and isinstance(
        raw_payload.get("body"), str
    ):
        body = _normalize_text(raw_payload["body"])
        if not body:
            # Whitespace-only after normalize -> EMPTY (no content version).
            return BodyRecoveryResult("EMPTY", None, None)
        if len(body) >= RAG_MIN_CHAR_COUNT:
            return BodyRecoveryResult(
                "FULL_TEXT",
                body,
                _content_hash_for_state("FULL_TEXT", normalized_body=body),
            )
        # Non-empty below threshold -> METADATA_ONLY (never FULL_TEXT).
        description = (
            _normalize_text(raw_description) if isinstance(raw_description, str)
            else None
        )
        return BodyRecoveryResult(
            "METADATA_ONLY",
            None,
            _metadata_only_hash(
                title=title,
                description=description or None,
                canonical_url=canonical_url,
            ),
        )

    # 2. No authentic body: description/title are metadata only.
    if raw_description is None:
        # Title with no body field at all -> TITLE_ONLY.
        if title:
            return BodyRecoveryResult(
                "TITLE_ONLY",
                None,
                _content_hash_for_state(
                    "TITLE_ONLY", normalized_title=title
                ),
            )
        return BodyRecoveryResult("EMPTY", None, None)

    if not isinstance(raw_description, str) or not raw_description.strip():
        # Whitespace-only body -> EMPTY (never a content version).
        return BodyRecoveryResult("EMPTY", None, None)

    description = _normalize_text(raw_description)
    if not description:
        return BodyRecoveryResult("EMPTY", None, None)

    # Description non-empty any length (including >= RAG_MIN_CHAR_COUNT) is a
    # snippet/lede -> METADATA_ONLY, never FULL_TEXT.
    return BodyRecoveryResult(
        "METADATA_ONLY",
        None,
        _metadata_only_hash(
            title=title,
            description=description,
            canonical_url=canonical_url,
        ),
    )


def persist_article_content_repair(
    conn: sqlite3.Connection,
    *,
    article_id: str,
    normalized_url: NormalizedUrl | None,
    result: BodyRecoveryResult,
    normalizer_version: str = BODY_NORMALIZER_VERSION,
) -> None:
    """Atomically write the v14 ``articles`` repair columns.

    Never writes canonical rows; M3-5B consumes these columns. An unknown
    ``article_id`` fails closed.
    """
    exists = conn.execute(
        "SELECT 1 FROM articles WHERE article_id=?", (article_id,)
    ).fetchone()
    if not exists:
        raise ValueError(f"unknown article_id: {article_id}")

    normalized_value = (
        normalized_url.value
        if normalized_url is not None and not normalized_url.unknown
        else None
    )
    conn.execute(
        """UPDATE articles SET
               normalized_url = ?,
               recovered_body_text = ?,
               recovered_content_state = ?,
               recovered_content_hash = ?,
               body_normalizer_version = ?
           WHERE article_id = ?""",
        (
            normalized_value,
            result.body_text,
            result.content_state,
            result.content_hash,
            normalizer_version,
            article_id,
        ),
    )
    conn.commit()


__all__ = [
    "BODY_NORMALIZER_VERSION",
    "BodyRecoveryResult",
    "persist_article_content_repair",
    "recover_body",
]
