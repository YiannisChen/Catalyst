"""Shared within-document attribution selection for the M8-B/M8-C candidate FTS.

One policy, used by both the M8-B recovery retrieval runner and the M8-C
candidate-FTS adapter. The FTS window is only asked to *identify documents*
(``retrieve_lexical`` stays the document-identification step); the display
ordinal inside those documents is chosen here from public chunk text and
metadata.

Rules encoded here (M8 executor lock, M8-B rerun prompt):

* The candidate window is pulled at the FTS cap (100) and fails closed below
  the floor (50) so a slot can be refilled from full-text supply.
* Documents come from the FTS window. A ``FULL_TEXT`` hit contributes its
  document; a ``METADATA_ONLY`` / ``TITLE_ONLY`` hit may contribute its
  ``document_id`` only and never a display slot.
* Every ``FULL_TEXT`` body of an identified document is expanded from
  ``corpus_build_chunks`` for the exact build, so ordinals outside the window
  can be displayed.
* Chunks are scored from public text only. Earnings/results language
  (Item 2.02, Item 8.01, Exhibit 99, results tables, press release) scores
  positive; XBRL schema references, Exchange Act cover-page boilerplate and
  copyright/litigation boilerplate score negative. Lexical/FTS order (document
  order in the window, then chunk ordinal) is the tie-break.
* At most ``MAX_CHUNKS_PER_DOCUMENT`` chunks per document are displayed, and
  unscored siblings never pad a slot: an empty slot is preferred to a chunk
  with no positive public signal.

No benchmark field (case id, expected evidence, evidence judgments, oracle
status, gold ordinal) is read by this module.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Sequence

FULL_TEXT_STATE = "FULL_TEXT"

# FTS builds are capped at 100 rows by ``retrieve_lexical``; the display needs a
# window deep enough to refill a slot behind a run of non-citable hits.
CANDIDATE_DEPTH_CAP = 100
CANDIDATE_DEPTH_FLOOR = 50

# One filing's attribution is a results section, not a whole document.
MAX_CHUNKS_PER_DOCUMENT = 3

# A marker repeated in a scraped table would otherwise dominate the score.
MARKER_REPEAT_CAP = 2

# Public earnings/results language (lower-case substrings).
POSITIVE_MARKERS: tuple[tuple[str, int], ...] = (
    ("item 2.02", 10),
    ("item 8.01", 8),
    ("exhibit 99", 12),
    ("ex-99", 12),
    ("press release", 6),
    ("results of operations", 6),
    ("total revenue", 10),
    ("cost of revenue", 6),
    ("gross profit", 8),
    ("operating income", 6),
    ("income from operations", 6),
    ("net income", 8),
    ("diluted", 6),
    ("earnings per share", 8),
    ("per share", 4),
    ("net sales", 6),
    ("earnings", 3),
    ("results", 2),
    ("revenue", 8),
)

# Public markers of the wrong ordinal: XBRL/scaffolding, cover-page boilerplate
# and litigation boilerplate.
NEGATIVE_MARKERS: tuple[tuple[str, int], ...] = (
    ("xbrl", 12),
    ("fasb.org", 8),
    ("us-gaap", 8),
    (".xsd", 4),
    ("exchange act of 1934", 8),
    ("commission file number", 8),
    ("check the appropriate box", 8),
    ("employer identification", 6),
    ("copyright", 8),
    ("alleging", 8),
    ("breach of fiduciary", 8),
    ("plaintiff", 6),
    ("lawsuit", 6),
)


def chunk_selection_score(text: str) -> int:
    """Public-text score for one chunk (<= 0 means "do not display")."""
    haystack = (text or "").casefold()
    if not haystack.strip():
        return 0
    score = 0
    for marker, weight in POSITIVE_MARKERS:
        hits = haystack.count(marker)
        if hits:
            score += weight * min(hits, MARKER_REPEAT_CAP)
    for marker, weight in NEGATIVE_MARKERS:
        hits = haystack.count(marker)
        if hits:
            score -= weight * min(hits, MARKER_REPEAT_CAP)
    return score


def _batched(values: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _window_document_order(
    conn: sqlite3.Connection, *, build_id: str, window: Sequence[str]
) -> tuple[tuple[str, ...], dict[str, int]]:
    """Document order of the FTS window (first appearance wins).

    ``FULL_TEXT`` and metadata-only hits both identify a document; only the
    document id is taken from a metadata-only hit.
    """
    order: dict[str, int] = {}
    for batch in _batched(window):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT chunk_id, document_id FROM corpus_build_chunks "
            f"WHERE build_id=? AND chunk_id IN ({placeholders})",
            (build_id, *batch),
        ).fetchall()
        found = {str(row[0]): str(row[1]) for row in rows}
        for chunk_id in batch:
            document_id = found.get(chunk_id)
            if document_id and document_id not in order:
                order[document_id] = len(order)
    return tuple(order), order


def _expanded_full_text_chunks(
    conn: sqlite3.Connection, *, build_id: str, document_ids: Sequence[str]
) -> list[tuple[str, str, str, str]]:
    """All citable ``FULL_TEXT`` bodies of the identified documents."""
    expanded: list[tuple[str, str, str, str]] = []
    for batch in _batched(document_ids):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT chunk_id, document_id, ordinal, content_text "
            "FROM corpus_build_chunks "
            f"WHERE build_id=? AND document_id IN ({placeholders}) "
            "AND content_state='FULL_TEXT' AND content_text IS NOT NULL "
            "AND length(trim(content_text)) > 0",
            (build_id, *batch),
        ).fetchall()
        expanded.extend(
            (str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows
        )
    return expanded


def select_within_document_slots(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    candidate_chunk_ids: Sequence[str],
    top_k: int,
) -> tuple[str, ...]:
    """Return the display/ranked chunk ids for one query (order preserved).

    ``candidate_chunk_ids`` is the FTS candidate window in retrieval order. The
    result contains ``FULL_TEXT`` chunks only, at most
    ``MAX_CHUNKS_PER_DOCUMENT`` per document, ordered by public score with
    lexical order as the tie-break, and capped at ``top_k``. Unscored chunks are
    never used to pad: fewer than ``top_k`` ids (or none) is a valid answer.
    """
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    window = tuple(dict.fromkeys(str(chunk_id) for chunk_id in candidate_chunk_ids))
    if not window:
        return ()
    document_ids, document_rank = _window_document_order(
        conn, build_id=build_id, window=window
    )
    if not document_ids:
        return ()
    scored: list[tuple[int, int, str, str, str]] = []
    for chunk_id, document_id, ordinal, text in _expanded_full_text_chunks(
        conn, build_id=build_id, document_ids=document_ids
    ):
        score = chunk_selection_score(text)
        if score <= 0:
            continue
        scored.append(
            (score, document_rank[document_id], ordinal, document_id, chunk_id)
        )
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    selected: list[str] = []
    per_document: dict[str, int] = {}
    for _score, _rank, _ordinal, document_id, chunk_id in scored:
        if per_document.get(document_id, 0) >= MAX_CHUNKS_PER_DOCUMENT:
            continue
        per_document[document_id] = per_document.get(document_id, 0) + 1
        selected.append(chunk_id)
        if len(selected) >= top_k:
            break
    return tuple(selected)


__all__ = [
    "CANDIDATE_DEPTH_CAP",
    "CANDIDATE_DEPTH_FLOOR",
    "FULL_TEXT_STATE",
    "MARKER_REPEAT_CAP",
    "MAX_CHUNKS_PER_DOCUMENT",
    "NEGATIVE_MARKERS",
    "POSITIVE_MARKERS",
    "chunk_selection_score",
    "select_within_document_slots",
]
