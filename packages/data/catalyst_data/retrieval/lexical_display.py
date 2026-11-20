"""Within-document attribution display for the V1.1 lexical retrieval path.

This is the ``lexical_display_v1`` production policy. It is a *behaviour-
identical* port of the historical eval reproduction module
``v1_1/within_document_selection.py`` in the eval package (Frozen-A3 §2.1: "the
policy is moved, not changed"). The historical module stays byte-identical and is the
reproduction path for sealed reports only; this data-core copy is what the
production agents and the eval RC runner now call.

The FTS window is only asked to *identify documents* (``retrieve_lexical`` stays
the document-identification step); the display ordinal inside those documents is
chosen here from public chunk text and metadata.

Rules encoded here (M8 executor lock, M8-B rerun prompt; Frozen-A3 §2.1):

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

Quirks are preserved, not fixed (Frozen-A3 §2.1 / C6): the string ordinal sort
(``"10" < "2"``), an empty window returning ``()``, and ``top_k <= 0`` raising.
Fixing any of them would be a ranking change.

No benchmark field (case id, expected evidence, evidence judgments, oracle
status, gold ordinal) is read by this module. data-core never imports the eval
package, and the module source names no eval module.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

# Query-policy identity bound into run manifests and data runtime identities.
# Defined here (the served lexical policy) so runtime code never imports the
# retired hybrid/dense stack; hybrid.py re-exports it for historical callers.
V1_QUERY_POLICY_VERSION = "qp:v1"

FULL_TEXT_STATE = "FULL_TEXT"

# FTS builds are capped at 100 rows by ``retrieve_lexical``; the display needs a
# window deep enough to refill a slot behind a run of non-citable hits.
CANDIDATE_DEPTH_CAP = 100
CANDIDATE_DEPTH_FLOOR = 50

# One filing's attribution is a results section, not a whole document.
MAX_CHUNKS_PER_DOCUMENT = 3

# Slots left empty after the per-document pass are refilled (display fill,
# 2026-09-29): first from positive chunks the per-document cap held back, so a
# single relevant filing is not cut to three of eight slots; then from neutral
# chunks, so a primary document without earnings vocabulary still displays.
FILL_UNUSED_SLOTS = True

# The frozen display cap (Frozen-A3 §2.1: ``display_top_k = 8``).
DISPLAY_TOP_K = 8

# The one production retrieval profile (Frozen-A3 §2.1).
RETRIEVAL_PROFILE = "lexical_display_v1"

# Why a scored candidate chunk was (or was not) displayed. These are the only
# exclusion reasons the display policy assigns; the G0 candidate artifact copies
# them verbatim instead of re-deriving the display decision.
EXCLUSION_DISPLAYED = "displayed"
EXCLUSION_NON_POSITIVE_SCORE = "non_positive_score"
EXCLUSION_PER_DOCUMENT_CAP = "per_document_cap"
EXCLUSION_BELOW_DISPLAY_CAP = "below_display_cap"

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

# The score tie-break, in order: public score descending, lexical document rank,
# then chunk ordinal (a *string* ordinal). Recorded here so the frozen tie-break
# order is part of the policy hash.
_SORT_KEY = ("-selection_score", "document_rank", "chunk_ordinal")

# The complete exclusion vocabulary, in policy order.
_EXCLUSIONS = (
    EXCLUSION_DISPLAYED,
    EXCLUSION_NON_POSITIVE_SCORE,
    EXCLUSION_PER_DOCUMENT_CAP,
    EXCLUSION_BELOW_DISPLAY_CAP,
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


@dataclass(frozen=True)
class WithinDocumentSlot:
    """One scored candidate chunk plus the display policy's decision on it.

    The policy is the single source of truth for the display decision: this
    record carries the score it computed, the ordered ``candidate_rank`` is the
    record's position in the returned tuple, ``display_rank`` is the post-cap
    rank (or ``None`` when the chunk was not displayed), and
    ``exclusion_reason`` is exactly one of the ``EXCLUSION_*`` values above.
    """

    chunk_id: str
    document_id: str
    ordinal: str
    document_rank: int
    selection_score: int
    display_rank: int | None
    exclusion_reason: str

    @property
    def displayed(self) -> bool:
        return self.display_rank is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "ordinal": self.ordinal,
            "document_rank": self.document_rank,
            "selection_score": self.selection_score,
            "display_rank": self.display_rank,
            "exclusion_reason": self.exclusion_reason,
        }


def select_within_document_slots(
    conn: sqlite3.Connection,
    *,
    build_id: str,
    candidate_chunk_ids: Sequence[str],
    top_k: int,
) -> tuple[WithinDocumentSlot, ...]:
    """Score every candidate and decide its display slot (the one policy).

    ``candidate_chunk_ids`` is the FTS/dense candidate window in retrieval
    order. Every ``FULL_TEXT`` body of an identified document is expanded and
    scored from public text, then returned in display order (public score, then
    lexical document order, then chunk ordinal). Each entry records whether the
    chunk was displayed and, if not, exactly why:

    * ``displayed`` — took one of the ``top_k`` slots;
    * ``non_positive_score`` — no positive public signal, never displayed;
    * ``per_document_cap`` — the document already used
      ``MAX_CHUNKS_PER_DOCUMENT`` slots;
    * ``below_display_cap`` — a positive-scoring chunk past the ``top_k`` cap.

    Displayed chunks are ``display_rank`` 1..N. Unscored siblings never pad a
    slot: fewer than ``top_k`` displayed chunks (or none) is a valid answer.
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
        scored.append(
            (
                chunk_selection_score(text),
                document_rank[document_id],
                ordinal,
                document_id,
                chunk_id,
            )
        )
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    slots: list[WithinDocumentSlot] = []
    per_document: dict[str, int] = {}
    displayed = 0
    for score, doc_rank, ordinal, document_id, chunk_id in scored:
        display_rank: int | None = None
        if score <= 0:
            reason = EXCLUSION_NON_POSITIVE_SCORE
        elif per_document.get(document_id, 0) >= MAX_CHUNKS_PER_DOCUMENT:
            reason = EXCLUSION_PER_DOCUMENT_CAP
        elif displayed >= top_k:
            reason = EXCLUSION_BELOW_DISPLAY_CAP
        else:
            per_document[document_id] = per_document.get(document_id, 0) + 1
            displayed += 1
            display_rank = displayed
            reason = EXCLUSION_DISPLAYED
        slots.append(
            WithinDocumentSlot(
                chunk_id=chunk_id,
                document_id=document_id,
                ordinal=ordinal,
                document_rank=doc_rank,
                selection_score=score,
                display_rank=display_rank,
                exclusion_reason=reason,
            )
        )
    if FILL_UNUSED_SLOTS:
        # Pass 2: positive chunks the per-document cap held back. Pass 3:
        # neutral (score 0) chunks in lexical order, so a non-earnings primary
        # document (a government statement, a contract 8-K) is not dropped for
        # lacking earnings vocabulary. Negative-score boilerplate never fills.
        for wanted in (
            lambda slot: slot.exclusion_reason == EXCLUSION_PER_DOCUMENT_CAP,
            lambda slot: slot.exclusion_reason == EXCLUSION_NON_POSITIVE_SCORE
            and slot.selection_score == 0,
        ):
            for index, slot in enumerate(slots):
                if displayed >= top_k:
                    break
                if wanted(slot):
                    displayed += 1
                    slots[index] = replace(
                        slot,
                        display_rank=displayed,
                        exclusion_reason=EXCLUSION_DISPLAYED,
                    )
    return tuple(slots)


def displayed_chunk_ids(slots: Sequence[WithinDocumentSlot]) -> tuple[str, ...]:
    """Projection of the policy output: just the displayed chunk ids in order."""
    return tuple(slot.chunk_id for slot in slots if slot.display_rank is not None)


# --------------------------------------------------------------------------- #
# Frozen policy hash (Frozen-A3 §2.1 / C6)
# --------------------------------------------------------------------------- #
def lexical_display_policy_payload() -> dict[str, object]:
    """Canonical, JSON-serialisable view of **every** frozen policy parameter.

    The whole retrieval policy is frozen: changing any part of it needs a new,
    pre-registered Frozen amendment with a new ``eval_id`` (C6). This payload is
    the single source of the parameters the hash covers — the depths, the display
    cap, the per-document cap, the repeat cap, both marker lists (in order), the
    tie-break order and the exclusion vocabulary.
    """
    return {
        "profile": RETRIEVAL_PROFILE,
        "depth_cap": CANDIDATE_DEPTH_CAP,
        "depth_floor": CANDIDATE_DEPTH_FLOOR,
        "display_top_k": DISPLAY_TOP_K,
        "max_chunks_per_document": MAX_CHUNKS_PER_DOCUMENT,
        "fill_unused_slots": FILL_UNUSED_SLOTS,
        "marker_repeat_cap": MARKER_REPEAT_CAP,
        "positive_markers": [[marker, weight] for marker, weight in POSITIVE_MARKERS],
        "negative_markers": [[marker, weight] for marker, weight in NEGATIVE_MARKERS],
        "sort_key": list(_SORT_KEY),
        "exclusions": list(_EXCLUSIONS),
    }


def lexical_display_policy_hash(payload: Mapping[str, object]) -> str:
    """sha256 over the canonical (sorted-key, compact) JSON of ``payload``."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


LEXICAL_DISPLAY_POLICY_HASH = lexical_display_policy_hash(
    lexical_display_policy_payload()
)

__all__ = [
    "CANDIDATE_DEPTH_CAP",
    "CANDIDATE_DEPTH_FLOOR",
    "DISPLAY_TOP_K",
    "EXCLUSION_BELOW_DISPLAY_CAP",
    "EXCLUSION_DISPLAYED",
    "EXCLUSION_NON_POSITIVE_SCORE",
    "EXCLUSION_PER_DOCUMENT_CAP",
    "FULL_TEXT_STATE",
    "LEXICAL_DISPLAY_POLICY_HASH",
    "MARKER_REPEAT_CAP",
    "MAX_CHUNKS_PER_DOCUMENT",
    "NEGATIVE_MARKERS",
    "POSITIVE_MARKERS",
    "RETRIEVAL_PROFILE",
    "WithinDocumentSlot",
    "chunk_selection_score",
    "displayed_chunk_ids",
    "lexical_display_policy_hash",
    "lexical_display_policy_payload",
    "select_within_document_slots",
]
