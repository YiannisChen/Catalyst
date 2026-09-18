"""M8-A contract tests: sealed source-selection ingest into canonical.

Small but real: an empty sealed selection writes nothing; a non-empty selection
mints canonical FULL_TEXT rows for a news/official document and binds an SEC
Archives exhibit to the filing that owns its accession. Identity is public
(URL / accession + body hash) and re-running is idempotent.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.canonical.source_ingest import (
    SourceIngestError,
    ingest_source_selection_documents,
)
from catalyst_data.canonical.source_selection import load_source_selection_manifest
from catalyst_data.config import RAG_MIN_CHAR_COUNT
from catalyst_data.migrations import run_migrations
from catalyst_data.storage.sqlite import init_db

ACCESSION = "0000731766-26-000025"
PRIMARY_URL = (
    "https://www.sec.gov/Archives/edgar/data/731766/000073176626000025/unh-8k.htm"
)
EXHIBIT_URL = (
    "https://www.sec.gov/Archives/edgar/data/731766/000073176626000025/"
    "a991unherq42025.htm"
)
NEWS_URL = "https://www.whitehouse.gov/briefings-statements/2025/05/example/"
NEWS_TITLE = "Joint Statement on U.S.-China Economic and Trade Meeting in Geneva"
NEWS_BODY = (
    "The White House published the joint statement following the meeting. "
    "China will take measures and the United States will act accordingly, "
    "including a ninety day suspension of the tariff escalation announced "
    "in April. Both sides said they would keep consulting on the channel.\n"
) * 3
_EXHIBIT_PARAGRAPH = (
    "<p>Item 2.02 Results of Operations. UnitedHealth Group reported a medical "
    "care ratio of 88.8 percent and reaffirmed its full year outlook for 2026 "
    "with adjusted earnings per share growth in the reported quarter.</p>"
)
EXHIBIT_HTML = "<html><body>" + _EXHIBIT_PARAGRAPH * 6 + "</body></html>"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "derivative.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    run_migrations(conn)
    from catalyst_data.corpus.streaming_publication import (
        ensure_streaming_publication_schema,
    )

    ensure_streaming_publication_schema(conn)
    conn.execute(
        """INSERT INTO raw_assets
           (asset_id, ticker, source_type, reference_date, fetched_at,
            data_version, content_raw, metadata_json)
           VALUES ('raw:sec-fixture','UNH','sec_filings','2026-01-05',
                   '2026-01-05T10:00:00Z','v1','raw','{}')"""
    )
    conn.execute(
        """INSERT INTO filings (
               filing_id, cik, ticker, form_type, filed_at, accession_number,
               primary_document, url, raw_asset_id, created_at,
               accepted_time_utc, eligible_at, eligible_at_reason,
               temporal_precision, accepted_time_recovered,
               eligibility_fail_closed
           ) VALUES ('filing-unh','0000731766','UNH','8-K','2026-01-05',?,
                     'unh-8k.htm',?,'raw:sec-fixture',
                     '2026-01-05T10:00:00Z','2026-01-05T20:06:03Z',
                     '2026-01-05T20:06:03Z','accepted_time_recovered',
                     'accepted_time',1,0)""",
        (ACCESSION, PRIMARY_URL),
    )
    conn.commit()
    from catalyst_data.canonical.backfill import backfill_from_subtypes

    # Production order: the general M3-5B backfill runs before the sealed
    # source ingest, so the parent filing asset already exists.
    backfill_from_subtypes(conn)
    conn.commit()
    return conn


def _manifest(tmp_path: Path, documents: list[dict]) -> tuple[Path, Path]:
    root = tmp_path / "bodies"
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "v1_1_source_selection_v1",
        "selection_policy_id": "general-public-fulltext-v1",
        "documents": documents,
    }
    path = tmp_path / "source_selection.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, root


def _document(tmp_path: Path, *, url: str, body: str, **extra) -> dict:
    root = tmp_path / "bodies"
    root.mkdir(parents=True, exist_ok=True)
    name = "".join(ch if ch.isalnum() else "_" for ch in url)[-40:] + ".txt"
    (root / name).write_text(body, encoding="utf-8")
    document = {
        "canonical_url": url,
        "source_class": extra.pop("source_class", "official_government"),
        "source_published_at": "2025-05-12T07:01:01Z",
        "eligible_at": "2025-05-12T07:01:01Z",
        "fetched_at": "2026-09-18T00:00:00Z",
        "body_sha256": _sha(body),
        "body_path": name,
        "provider": "official_html",
        "publisher": "The White House",
        "tickers": ["UNH"],
        **extra,
    }
    return document


def test_empty_selection_ingests_nothing(tmp_path):
    conn = _conn(tmp_path)
    try:
        before = (
            conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM canonical_assets").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM canonical_content_versions").fetchone()[0],
        )
        path, root = _manifest(tmp_path, [])
        manifest = load_source_selection_manifest(path, body_root=root)
        assert manifest.documents == ()
        result = ingest_source_selection_documents(
            conn, manifest=manifest, body_root=root
        )
        assert result.documents == ()
        after = (
            conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM canonical_assets").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM canonical_content_versions").fetchone()[0],
        )
        assert before == after
    finally:
        conn.close()


def test_news_document_mints_canonical_full_text_deterministically(tmp_path):
    conn = _conn(tmp_path)
    try:
        assert len(NEWS_BODY) >= RAG_MIN_CHAR_COUNT
        path, root = _manifest(
            tmp_path,
            [
                _document(
                    tmp_path,
                    url=NEWS_URL,
                    body=NEWS_BODY,
                    title=NEWS_TITLE,
                    publisher="The White House",
                )
            ],
        )
        manifest = load_source_selection_manifest(path, body_root=root)
        first = ingest_source_selection_documents(
            conn, manifest=manifest, body_root=root
        )
        second = ingest_source_selection_documents(
            conn, manifest=manifest, body_root=root
        )
        assert first.documents[0].content_state == "FULL_TEXT"
        assert (
            first.documents[0].canonical_content_version_id
            == second.documents[0].canonical_content_version_id
        )
        assert (
            first.documents[0].asset_id == second.documents[0].asset_id
        )
        row = conn.execute(
            "SELECT content_state, serving_status FROM canonical_assets "
            "WHERE asset_id=?",
            (first.documents[0].asset_id,),
        ).fetchone()
        assert row["content_state"] == "FULL_TEXT"
        assert row["serving_status"] == "body_candidate"
        assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM article_tickers WHERE is_canonical=1"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_news_document_without_title_fails_closed(tmp_path):
    conn = _conn(tmp_path)
    try:
        path, root = _manifest(
            tmp_path, [_document(tmp_path, url=NEWS_URL, body=NEWS_BODY)]
        )
        manifest = load_source_selection_manifest(path, body_root=root)
        with pytest.raises(SourceIngestError, match="title"):
            ingest_source_selection_documents(
                conn, manifest=manifest, body_root=root
            )
    finally:
        conn.close()


def test_sec_exhibit_binds_to_the_filing_that_owns_the_accession(tmp_path):
    conn = _conn(tmp_path)
    try:
        path, root = _manifest(
            tmp_path,
            [
                _document(
                    tmp_path,
                    url=EXHIBIT_URL,
                    body=EXHIBIT_HTML,
                    source_class="issuer_disclosure",
                    title=None,
                    **{
                        "filing_accession": ACCESSION,
                        "document_role": "exhibit_99_1",
                    },
                )
            ],
        )
        manifest = load_source_selection_manifest(path, body_root=root)
        result = ingest_source_selection_documents(
            conn, manifest=manifest, body_root=root
        )
        assert result.documents[0].document_kind == "filing_document"
        doc = conn.execute(
            "SELECT document_id, parser_version, extraction_status, document_hash "
            "FROM filing_documents WHERE filing_id='filing-unh' "
            "AND document_id!=?",
            ("",),
        ).fetchone()
        assert doc is not None
        assert doc["parser_version"]
        assert doc["extraction_status"] == "success"
        assert doc["document_hash"]
        assoc = conn.execute(
            "SELECT canonical_content_version_id FROM canonical_subtype_assoc "
            "WHERE subtype_pk_value=?",
            (doc["document_id"],),
        ).fetchone()
        assert assoc is not None
        assert assoc[0] == result.documents[0].canonical_content_version_id
    finally:
        conn.close()


def test_sec_document_without_matching_filing_fails_closed(tmp_path):
    conn = _conn(tmp_path)
    try:
        body = EXHIBIT_HTML
        path, root = _manifest(
            tmp_path,
            [
                _document(
                    tmp_path,
                    url=(
                        "https://www.sec.gov/Archives/edgar/data/1/000000000000000001/"
                        "ex99.htm"
                    ),
                    body=body,
                    source_class="issuer_disclosure",
                    title=None,
                    **{
                        "filing_accession": "0000000000-00-000001",
                        "document_role": "exhibit_99_1",
                    },
                )
            ],
        )
        manifest = load_source_selection_manifest(path, body_root=root)
        with pytest.raises(SourceIngestError, match="no filing"):
            ingest_source_selection_documents(
                conn, manifest=manifest, body_root=root
            )
    finally:
        conn.close()


def test_first_party_html_body_is_extracted_not_stored_raw(tmp_path):
    """Archive HTML (whitehouse.gov/cms.gov style) must become citable text."""
    from catalyst_data.canonical.source_ingest import _first_party_body_text

    html = (
        "<html><head><title>Joint Statement</title></head><body>"
        + "<p>China will take measures and the United States will act "
        "accordingly, including a suspension of the tariff escalation.</p>" * 8
        + "</body></html>"
    )
    text = _first_party_body_text(html.encode("utf-8"))
    assert isinstance(text, str)
    assert "<p>" not in text and "<html>" not in text
    assert "China will take measures" in text
    # Plain text bodies are never run through the HTML extractor.
    assert _first_party_body_text(b"plain body text\n") == "plain body text\n"


def test_news_document_from_html_archive_mints_full_text(tmp_path):
    conn = _conn(tmp_path)
    try:
        body = (
            "<html><body>"
            + "<p>Both sides said they would keep consulting on the channel and "
            "the suspension covers the tariff escalation announced in April.</p>" * 8
            + "</body></html>"
        )
        path, root = _manifest(
            tmp_path,
            [
                _document(
                    tmp_path,
                    url=NEWS_URL,
                    body=body,
                    title=NEWS_TITLE,
                    publisher="The White House",
                )
            ],
        )
        manifest = load_source_selection_manifest(path, body_root=root)
        result = ingest_source_selection_documents(
            conn, manifest=manifest, body_root=root
        )
        assert result.documents[0].content_state == "FULL_TEXT"
        stored = conn.execute(
            "SELECT subtype_metadata FROM canonical_assets WHERE asset_id=?",
            (result.documents[0].asset_id,),
        ).fetchone()[0]
        assert "<p>" not in stored
    finally:
        conn.close()
