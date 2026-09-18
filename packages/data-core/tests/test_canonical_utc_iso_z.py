"""Canonical UTC serialization at the retrieval/context boundary (M7-10).

``datetime.isoformat()`` renders a UTC datetime as ``...+00:00``. The
retrieval contract requires the canonical ``...Z`` rendering, and the SQLite
rows it is compared against are stored as ``...Z``. An offset-rendered cutoff
is therefore both rejected (``invalid_cutoff``) and mis-ordered by string
comparison, so the canonical formatter is the only correct serialization.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from retrieval_fixtures import (
    MANIFEST_A,
    build_fts5,
    fresh_v10_db,
    insert_corpus_chunk,
)

CUTOFF_Z = "2026-01-15T21:00:00Z"
CUTOFF_OFFSET = "2026-01-15T21:00:00+00:00"


# ── canonical formatter ─────────────────────────────────────────────────────

def test_utc_iso_z_renders_utc_with_z_suffix() -> None:
    from catalyst_data.canonical import utc_iso_z

    assert utc_iso_z(datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc)) == CUTOFF_Z


def test_utc_iso_z_converts_non_utc_offsets_to_utc_before_rendering() -> None:
    from catalyst_data.canonical import utc_iso_z

    shifted = datetime(2026, 1, 16, 5, 0, tzinfo=timezone(timedelta(hours=8)))
    assert utc_iso_z(shifted) == CUTOFF_Z


def test_utc_iso_z_is_second_resolution_and_rejects_naive_values() -> None:
    from catalyst_data.canonical import utc_iso_z

    micro = datetime(2026, 1, 15, 21, 0, 0, 123456, tzinfo=timezone.utc)
    assert utc_iso_z(micro) == CUTOFF_Z
    with pytest.raises(ValueError):
        utc_iso_z(datetime(2026, 1, 15, 21, 0))
    assert not utc_iso_z(micro).endswith("+00:00")


# ── retrieval contract ──────────────────────────────────────────────────────

def _seeded_lexical_db():
    db = fresh_v10_db()
    insert_corpus_chunk(
        db,
        chunk_id="poly:1:news_v2:body:0001",
        document_id="poly:1",
        content_text="AAPL earnings report strong results",
        available_at="2026-01-01T09:00:00Z",
        eligibility="eligible",
        ticker_associations=("AAPL",),
        manifest_id=MANIFEST_A,
        status="active",
        source_class="reported_news",
        chunk_profile_version="news_v2",
        ordinal="0001",
    )
    db.commit()
    build_fts5(db, MANIFEST_A)
    return db


def test_lexical_retrieval_rejects_offset_rendered_cutoff() -> None:
    from catalyst_data.retrieval.fts5 import retrieve_lexical
    from catalyst_data.retrieval.result import RetrievalContractError

    db = _seeded_lexical_db()
    with pytest.raises(RetrievalContractError) as excinfo:
        retrieve_lexical(
            db,
            "earnings",
            ticker="AAPL",
            cutoff=CUTOFF_OFFSET,
            requested_manifest_id=MANIFEST_A,
            top_k=5,
            candidate_depth=10,
        )
    assert "invalid_cutoff" in str(excinfo.value)


def test_lexical_retrieval_accepts_canonical_z_cutoff() -> None:
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    db = _seeded_lexical_db()
    result = retrieve_lexical(
        db,
        "earnings",
        ticker="AAPL",
        cutoff=CUTOFF_Z,
        requested_manifest_id=MANIFEST_A,
        top_k=5,
        candidate_depth=10,
    )
    assert result is not None
