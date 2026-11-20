from __future__ import annotations

import hashlib
import sqlite3

import pytest
from catalyst_data.retrieval.evidence_fts_projection import (
    EvidenceText,
    EvidenceTextProjectionError,
    ensure_schema,
    project_evidence_text,
)


def _row(**overrides) -> EvidenceText:
    values = {
        "evidence_id": "web:article-1",
        "version_id": "version-1",
        "source_id": "alpaca_news",
        "document_id": "article-1",
        "provider": "alpaca",
        "kind": "body",
        "evidence_class": "body_evidence",
        "title": "Issuer announces a new contract",
        "text": "The issuer signed a multi-year contract with a named customer.",
        "origin": "benzinga:wire-77",
        "provenance": "versioned",
        "observed_at": "2026-10-24T16:00:00Z",
        "published_at": "2026-10-24T15:55:00Z",
        "provider_updated_at": "2026-10-24T15:55:00Z",
    }
    values.update(overrides)
    return EvidenceText(**values)


def test_projection_indexes_eligible_body_and_filing_with_provenance():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    body = _row()
    filing = _row(
        evidence_id="sec:accession-1:ex99-1",
        version_id="filing-v1",
        source_id="sec_filings",
        document_id="accession-1:ex99-1",
        provider="sec",
        kind="filing",
        evidence_class="filing_evidence",
        title="Quarterly results",
        text="Revenue increased by twelve percent.",
        origin="sec:accession-1",
    )

    assert project_evidence_text(conn, [body, filing]) == 2

    rows = conn.execute(
        """SELECT evidence_id, version_id, source_id, document_id, provider,
                  kind, origin, provenance, observed_at, published_at,
                  provider_updated_at, body_sha256
           FROM evidence_text_projection ORDER BY evidence_id"""
    ).fetchall()
    assert [row[:2] for row in rows] == [
        ("sec:accession-1:ex99-1", "filing-v1"),
        ("web:article-1", "version-1"),
    ]
    body_row = rows[1]
    assert body_row[2:11] == (
        "alpaca_news", "article-1", "alpaca", "body", "benzinga:wire-77",
        "versioned", "2026-10-24T16:00:00Z", "2026-10-24T15:55:00Z",
        "2026-10-24T15:55:00Z",
    )
    assert body_row[11] == hashlib.sha256(body.text.encode()).hexdigest()
    assert conn.execute(
        "SELECT evidence_id FROM evidence_text_fts WHERE evidence_text_fts MATCH ?",
        ('"multi-year"',),
    ).fetchall() == [("web:article-1",)]


def test_projection_excludes_leads_and_non_body_kinds_even_for_known_provider():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    lead = _row(evidence_class="lead", evidence_id="lead-1")
    headline = _row(kind="headline", evidence_class="lead", evidence_id="headline-1")

    assert project_evidence_text(conn, [lead, headline]) == 0
    assert conn.execute("SELECT COUNT(*) FROM evidence_text_projection").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kind,evidence_class",
    [("body", "filing_evidence"), ("filing", "body_evidence"), ("headline", "body_evidence")],
)
def test_projection_rejects_mismatched_evidence_class(kind, evidence_class):
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)

    with pytest.raises(EvidenceTextProjectionError, match="evidence_class_kind_mismatch"):
        project_evidence_text(conn, [_row(kind=kind, evidence_class=evidence_class)])


def test_projection_is_idempotent_but_rejects_identity_content_conflict():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    row = _row()

    assert project_evidence_text(conn, [row]) == 1
    assert project_evidence_text(conn, [row]) == 0
    assert conn.execute("SELECT COUNT(*) FROM evidence_text_projection").fetchone() == (1,)

    with pytest.raises(EvidenceTextProjectionError, match="projection_identity_conflict"):
        project_evidence_text(conn, [_row(text="different body")])


def test_projection_does_not_infer_evidence_from_provider_name():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    row = _row(provider="alpaca", evidence_class="lead")

    assert project_evidence_text(conn, [row]) == 0


def test_projection_respects_caller_rollback_when_it_opens_the_transaction():
    conn = sqlite3.connect(":memory:")
    assert project_evidence_text(conn, [_row()]) == 1

    conn.rollback()

    # Both schema creation and projected content belong to the caller's transaction.
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='evidence_text_projection'"
    ).fetchone()[0] == 0
