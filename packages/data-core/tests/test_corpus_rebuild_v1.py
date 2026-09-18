"""M3-9: inactive corpus candidate rebuild from canonical projection."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from catalyst_data.canonical.backfill import (
    MATERIALITY_VERSION,
    NEWS_NORMALIZER_VERSION,
    SEC_NORMALIZER_VERSION,
)
from catalyst_data.canonical.ids import (
    canonical_json_bytes,
    compute_canonical_projection_digest,
    corpus_document_id,
)
from catalyst_data.config import BGE_M3_REVISION
from catalyst_data.corpus.source_classifier import CLASSIFIER_VERSION
from catalyst_data.corpus.tokenizer import TOKENIZER_MODEL_ID, TOKENIZER_REVISION
from catalyst_data.storage.sqlite import init_db


SNAPSHOT = "b" * 64
PROBE = "c" * 64
POSTBUILD = "d" * 64
NOW = "2026-08-01T00:00:00Z"
NEWS_BODY = (
    "Apple Inc. announced new AI features during its product event. "
    "The company said the updates will roll out to customers starting next "
    "month. Analysts expect the changes to improve device performance and "
    "battery life across the lineup. This paragraph is deliberately long "
    "enough to clear the minimum material body threshold so the projection "
    "classifies the article as full text evidence rather than metadata."
)
FILING_BODY = (
    "Item 1.01 Entry into a Material Definitive Agreement\n"
    "The registrant entered into a material agreement with counterparties. "
    "The terms include customary covenants and closing conditions that apply "
    "to the parties under the agreement as of the date of this report.\n"
    "Item 2.02 Results of Operations and Financial Condition\n"
    "The registrant announced financial results for the fiscal period. "
    "Revenue and operating income are discussed in the accompanying exhibit."
)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pointer_snapshot(conn: sqlite3.Connection, active_path: Path) -> dict[str, object]:
    current = conn.execute(
        "SELECT manifest_id, is_current, manifest_json FROM corpus_manifest "
        "WHERE is_current=1 ORDER BY manifest_id"
    ).fetchall()
    served = conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN "
        "('corpus_served_chunks','corpus_chunks')"
    ).fetchall()
    mapping = []
    if served:
        rel = served[0][0]
        mapping = conn.execute(
            f"SELECT chunk_id, manifest_id FROM {rel} ORDER BY chunk_id"
        ).fetchall()
    lex = conn.execute(
        "SELECT singleton_id, corpus_manifest_id, mode_served, row_count "
        "FROM lexical_index_state WHERE singleton_id=1"
    ).fetchone()
    return {
        "manifest": current,
        "served": mapping,
        "lexical": lex,
        "active": active_path.read_bytes() if active_path.exists() else b"",
    }


def _seed_live_pointers(conn: sqlite3.Connection, active_path: Path) -> None:
    live_manifest = "a" * 64
    conn.execute(
        """INSERT OR REPLACE INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?,?,1,?)""",
        (live_manifest, json.dumps({"live": True}), NOW),
    )
    conn.execute(
        """INSERT OR REPLACE INTO corpus_chunks (
            chunk_id, document_id, chunk_profile_version, section_key, ordinal,
            content_text, content_hash, metadata_hash, source_class,
            available_at, ticker_associations, eligibility, manifest_id, status,
            boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
            prefix_token_count, prefix_truncated, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "live-chunk-1",
            "live-doc",
            "news_v2",
            "body",
            "0001",
            "live text",
            _h("live text"),
            _h("live-meta"),
            "reported_news",
            NOW,
            '["AAPL"]',
            "eligible",
            live_manifest,
            "active",
            "document_end",
            0,
            1,
            0,
            0,
            0,
            NOW,
            NOW,
        ),
    )
    conn.execute(
        """INSERT OR REPLACE INTO lexical_index_state
           (singleton_id, schema_version, corpus_manifest_id, mode_served,
            row_count, built_at)
           VALUES (1,'1.0.0',?,?,1,?)""",
        (live_manifest, "fts5", NOW),
    )
    active_path.write_text(
        json.dumps({"schema_version": "active_generation_v1", "table_name": "live_table"}),
        encoding="utf-8",
    )
    conn.commit()


def _seed_raw_asset(conn: sqlite3.Connection, *, asset_id: str, source_type: str) -> None:
    conn.execute(
        """INSERT INTO raw_assets
           (asset_id, ticker, source_type, reference_date, fetched_at,
            data_version, content_raw, metadata_json)
           VALUES (?, 'AAPL', ?, '2026-01-05', '2026-01-05T10:00:00Z',
                   'v1', ?, '{}')""",
        (asset_id, source_type, b"raw-payload"),
    )


def _seed_production_shaped_fixture(conn: sqlite3.Connection) -> None:
    """Seed raw subtype tables and run the REAL canonical backfill (M3-2).

    Production shape: filing body lives only in ``filing_documents.text`` and
    is bound through ``canonical_subtype_assoc``. No ``raw_text`` is placed in
    canonical ``subtype_metadata`` (matches production; M3-9 regression).
    """
    from catalyst_data.articles import upsert_article, upsert_article_ticker
    from catalyst_data.canonical.backfill import backfill_from_subtypes
    from catalyst_data.corpus.news_v2 import _normalize_text
    from catalyst_data.storage.sqlite import upsert_filing

    _seed_raw_asset(conn, asset_id="raw:news-1", source_type="finnhub_company_news")
    _seed_raw_asset(conn, asset_id="raw:sec-1", source_type="sec_filings")

    upsert_article(
        conn,
        article={
            "article_id": "finnhub:news-1",
            "raw_asset_id": "raw:news-1",
            "provider": "finnhub",
            "source_type": "finnhub_company_news",
            "ticker": "AAPL",
            "reference_date": "2026-01-05",
            "published_utc": "2026-01-05T15:30:00Z",
            "title": "Apple announces new AI features",
            "description": NEWS_BODY,
            "article_url": "https://www.reuters.com/fixture-news",
            "publisher_name": "Reuters",
        },
    )
    upsert_article_ticker(
        conn,
        article_id="finnhub:news-1",
        ticker="AAPL",
        raw_asset_id="raw:news-1",
        reference_date="2026-01-05",
    )

    upsert_filing(
        conn,
        filing_id="filing-8k-fixture",
        cik="0000320193",
        ticker="AAPL",
        form_type="8-K",
        filed_at="2026-01-05",
        accession_number="0000320193-26-000001",
        primary_document="a.htm",
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/a.htm",
        raw_asset_id="raw:sec-1",
    )
    # Production M3-3 shape: persist the recovered SEC acceptance time so the
    # filing projects as an eligible body_candidate (never fail-closed on a
    # date-only filed_at).
    conn.execute(
        """UPDATE filings SET
               accepted_time_utc='2026-01-05T20:06:03Z',
               eligible_at='2026-01-05T20:06:03Z',
               eligible_at_reason='accepted_time_recovered',
               temporal_precision='accepted_time',
               accepted_time_recovered=1,
               eligibility_fail_closed=0
           WHERE filing_id='filing-8k-fixture'"""
    )
    normalized_filing_body = _normalize_text(FILING_BODY)
    conn.execute(
        """INSERT INTO filing_documents (
               filing_id, document_url, document_type, text, char_len,
               content_type, byte_size, extraction_status, extracted_at,
               document_id, parser_version, document_hash, parse_quality,
               section_parse_degraded
           ) VALUES (?, ?, 'primary_doc', ?, ?, 'text/html', ?, 'success',
                     '2026-01-05T20:00:00Z', ?, 'sec_extract_v1', ?, 'full', 0)""",
        (
            "filing-8k-fixture",
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/a.htm",
            FILING_BODY,
            len(FILING_BODY.encode("utf-8")),
            len(FILING_BODY.encode("utf-8")),
            "e" * 64,
            _h(normalized_filing_body),
        ),
    )
    conn.commit()
    backfill_from_subtypes(conn)
    conn.commit()


def _fixture_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_production_shaped_fixture(conn)
    return conn


def _filing_asset_identity(conn: sqlite3.Connection) -> tuple[str, str]:
    row = conn.execute(
        """SELECT a.asset_id, v.canonical_content_version_id
           FROM canonical_assets a
           JOIN canonical_content_versions v ON v.asset_id = a.asset_id
           WHERE a.asset_type='FILING'
           ORDER BY a.asset_id LIMIT 1"""
    ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


def _expected_build_id(projection_digest: str, profile_versions: dict[str, str]) -> str:
    header = {
        "certified_snapshot_identity": SNAPSHOT,
        "canonical_projection_digest": projection_digest,
        "normalization_version": "1.0.0",
        "materiality_version": MATERIALITY_VERSION,
        "sec_parser_version": SEC_NORMALIZER_VERSION,
        "news_body_normalizer_version": NEWS_NORMALIZER_VERSION,
        "chunk_profile_versions": profile_versions,
        "source_classifier_version": CLASSIFIER_VERSION,
        "tokenizer_model_id": TOKENIZER_MODEL_ID,
        "tokenizer_revision": TOKENIZER_REVISION,
        "embedding_revision_or_null": BGE_M3_REVISION,
    }
    payload = {"schema_version": "corpus_streaming_build_v2", **header}
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def test_stage_corpus_candidate_missing_api():
    from catalyst_data.corpus.streaming_publication import (
        InactiveCorpusCandidate,
        stage_corpus_candidate,
    )

    assert callable(stage_corpus_candidate)
    assert InactiveCorpusCandidate is not None


def _stage_fixture_candidate(tmp_path):
    from catalyst_data.corpus.streaming_publication import (
        InactiveCorpusCandidate,
        stage_corpus_candidate,
    )

    db_path = tmp_path / "m3-9.db"
    conn = _fixture_conn(db_path)
    active_path = tmp_path / "active_generation.json"
    _seed_live_pointers(conn, active_path)
    before = _pointer_snapshot(conn, active_path)
    out = tmp_path / "bundles"
    profiles = {"news": "news_v2", "filing": "filing_v3"}
    candidate = stage_corpus_candidate(
        conn,
        certified_snapshot_identity=SNAPSHOT,
        profile_versions=profiles,
        source_bundle_output_root=out,
        snapshot_id=SNAPSHOT,
        probe_report_id=PROBE,
        postbuild_readiness_id=POSTBUILD,
    )
    return conn, candidate, before, active_path


def test_real_backfill_filing_produces_filing_v3_chunks(tmp_path):
    """A production-shaped FULL_TEXT filing yields filing_v3 chunks."""
    from catalyst_data.corpus.streaming_publication import stage_corpus_candidate

    conn, candidate, _before, _active = _stage_fixture_candidate(tmp_path)
    asset_id, version_id = _filing_asset_identity(conn)
    chunks = conn.execute(
        """SELECT chunk_id, canonical_asset_id, content_version_id,
                  corpus_document_id, chunk_profile_version, content_state,
                  source_class, parse_quality
           FROM corpus_build_chunks WHERE build_id=?
           ORDER BY chunk_id""",
        (candidate.build_id,),
    ).fetchall()
    filing_chunks = [row for row in chunks if row["canonical_asset_id"] == asset_id]
    assert filing_chunks, "production-shaped filing produced no filing chunks"
    assert all(row["chunk_profile_version"] == "filing_v3" for row in filing_chunks)
    assert all(row["content_state"] == "FULL_TEXT" for row in filing_chunks)
    assert all(row["source_class"] == "official_government" for row in filing_chunks)
    assert any(
        "item_1.01" in row["chunk_id"] or "item_2.02" in row["chunk_id"]
        for row in filing_chunks
    )
    assert candidate.chunk_count >= 2


def test_duplicate_provenance_associations_do_not_duplicate_corpus_documents(tmp_path):
    """filings + filing_documents associations yield ONE corpus document per
    (asset_id, canonical_content_version_id, filing profile)."""
    from catalyst_data.index_builder import build_canonical_corpus_records

    conn, candidate, _before, _active = _stage_fixture_candidate(tmp_path)
    asset_id, version_id = _filing_asset_identity(conn)
    assocs = conn.execute(
        """SELECT subtype_table, subtype_pk, subtype_pk_value
           FROM canonical_subtype_assoc WHERE asset_id=? AND
             canonical_content_version_id=? ORDER BY subtype_table""",
        (asset_id, version_id),
    ).fetchall()
    assert {row["subtype_table"] for row in assocs} == {"filings", "filing_documents"}

    records = build_canonical_corpus_records(conn)
    filing_records = [r for r in records if r["asset_id"] == asset_id]
    assert len(filing_records) == 1, (
        "expected one canonical corpus record per filing content version, got "
        f"{len(filing_records)}"
    )
    surviving = filing_records[0]
    document_assoc = next(
        row for row in assocs if row["subtype_table"] == "filing_documents"
    )
    assert surviving["subtype_table"] == "filing_documents"
    assert surviving["subtype_pk"] == "document_id"
    assert surviving["subtype_pk_value"] == document_assoc["subtype_pk_value"]

    docs = conn.execute(
        """SELECT source_kind, source_key, document_id
           FROM corpus_build_documents WHERE build_id=? AND document_id=?
           ORDER BY source_key""",
        (candidate.build_id, corpus_document_id(
            canonical_content_version_id=version_id,
            chunk_profile_version="filing_v3",
        )),
    ).fetchall()
    assert len(docs) == 1, f"expected one corpus document, got {len(docs)}"

    chunk_ids = [
        row["chunk_id"]
        for row in conn.execute(
            "SELECT chunk_id FROM corpus_build_chunks WHERE build_id=? AND canonical_asset_id=?",
            (candidate.build_id, asset_id),
        )
    ]
    assert chunk_ids
    assert len(chunk_ids) == len(set(chunk_ids)), "duplicate filing chunk_ids emitted"


def test_filing_chunks_bind_correct_asset_and_content_version(tmp_path):
    """All emitted filing chunks bind the right asset/content version/document."""
    conn, candidate, _before, _active = _stage_fixture_candidate(tmp_path)
    asset_id, version_id = _filing_asset_identity(conn)
    expected_doc = corpus_document_id(
        canonical_content_version_id=version_id,
        chunk_profile_version="filing_v3",
    )
    rows = conn.execute(
        """SELECT chunk_id, canonical_asset_id, content_version_id,
                  corpus_document_id
           FROM corpus_build_chunks WHERE build_id=? AND chunk_profile_version='filing_v3'
           ORDER BY chunk_id""",
        (candidate.build_id,),
    ).fetchall()
    assert rows
    for row in rows:
        assert row["canonical_asset_id"] == asset_id
        assert row["content_version_id"] == version_id
        assert row["corpus_document_id"] == expected_doc


def _filing_candidate_with_missing_document_binding(tmp_path) -> sqlite3.Connection:
    conn = _fixture_conn(tmp_path / "m3-9.db")
    asset_id, version_id = _filing_asset_identity(conn)
    conn.execute(
        "DELETE FROM canonical_subtype_assoc WHERE asset_id=? AND subtype_table='filing_documents'",
        (asset_id,),
    )
    conn.commit()
    return conn


def test_missing_filing_document_binding_fails_closed(tmp_path):
    """A FULL_TEXT filing content version without a filing_documents binding
    must fail closed during candidate staging."""
    from catalyst_data.corpus.streaming_publication import stage_corpus_candidate

    conn = _filing_candidate_with_missing_document_binding(tmp_path)
    with pytest.raises(ValueError, match="filing_documents"):
        stage_corpus_candidate(
            conn,
            certified_snapshot_identity=SNAPSHOT,
            profile_versions={"news": "news_v2", "filing": "filing_v3"},
            source_bundle_output_root=tmp_path / "bundles",
            snapshot_id=SNAPSHOT,
            probe_report_id=PROBE,
            postbuild_readiness_id=POSTBUILD,
        )


def test_missing_filing_text_row_fails_closed(tmp_path):
    """An association that points at a non-existent filing_documents row must
    fail closed (LEFT JOIN yields no text)."""
    from catalyst_data.corpus.streaming_publication import stage_corpus_candidate

    conn = _fixture_conn(tmp_path / "m3-9.db")
    asset_id, version_id = _filing_asset_identity(conn)
    conn.execute(
        """UPDATE canonical_subtype_assoc SET subtype_pk_value=?
           WHERE asset_id=? AND subtype_table='filing_documents'""",
        ("f" * 64, asset_id),
    )
    conn.commit()
    with pytest.raises(ValueError, match="filing"):
        stage_corpus_candidate(
            conn,
            certified_snapshot_identity=SNAPSHOT,
            profile_versions={"news": "news_v2", "filing": "filing_v3"},
            source_bundle_output_root=tmp_path / "bundles",
            snapshot_id=SNAPSHOT,
            probe_report_id=PROBE,
            postbuild_readiness_id=POSTBUILD,
        )


def test_hash_mismatched_filing_text_fails_closed(tmp_path):
    """A filing_documents row whose normalized text hash differs from the
    content version must fail closed."""
    from catalyst_data.corpus.streaming_publication import stage_corpus_candidate

    conn = _fixture_conn(tmp_path / "m3-9.db")
    asset_id, _version_id = _filing_asset_identity(conn)
    document_id = conn.execute(
        """SELECT subtype_pk_value FROM canonical_subtype_assoc
           WHERE asset_id=? AND subtype_table='filing_documents'""",
        (asset_id,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE filing_documents SET text=? WHERE document_id=?",
        ("Item 1.01 TAMPERED BODY with completely different content.", document_id),
    )
    conn.commit()
    with pytest.raises(ValueError, match="content_hash|hash"):
        stage_corpus_candidate(
            conn,
            certified_snapshot_identity=SNAPSHOT,
            profile_versions={"news": "news_v2", "filing": "filing_v3"},
            source_bundle_output_root=tmp_path / "bundles",
            snapshot_id=SNAPSHOT,
            probe_report_id=PROBE,
            postbuild_readiness_id=POSTBUILD,
        )


def test_stage_corpus_candidate_rebuilds_inactive_from_canonical_projection(tmp_path):
    from catalyst_data.corpus.streaming_publication import (
        InactiveCorpusCandidate,
        stage_corpus_candidate,
    )
    from catalyst_data.index_builder import (
        build_article_records,
        build_canonical_corpus_records,
        build_filing_records,
    )

    db_path = tmp_path / "m3-9.db"
    conn = _fixture_conn(db_path)
    active_path = tmp_path / "active_generation.json"
    _seed_live_pointers(conn, active_path)
    before = _pointer_snapshot(conn, active_path)
    out = tmp_path / "bundles"
    profiles = {"news": "news_v2", "filing": "filing_v3"}

    fts_calls: list[object] = []
    cutover_calls: list[object] = []
    fts5_calls: list[object] = []
    article_calls: list[object] = []
    filing_calls: list[object] = []

    def _track_articles(inner_conn, **kwargs):
        article_calls.append(True)
        return build_article_records(inner_conn, **kwargs)

    def _track_filings(inner_conn, **kwargs):
        filing_calls.append(True)
        return build_filing_records(inner_conn, **kwargs)

    with (
        patch(
            "catalyst_data.corpus.streaming_publication._fts_phase",
            side_effect=lambda *a, **k: fts_calls.append((a, k)) or (_ for _ in ()).throw(
                AssertionError("_fts_phase must not be called")
            ),
        ),
        patch(
            "catalyst_data.corpus.streaming_publication._cutover",
            side_effect=lambda *a, **k: cutover_calls.append((a, k)) or (_ for _ in ()).throw(
                AssertionError("_cutover must not be called")
            ),
        ),
        patch(
            "catalyst_data.retrieval.fts5_builder.build_fts5_index",
            side_effect=lambda *a, **k: fts5_calls.append((a, k)),
        ),
        patch(
            "catalyst_data.index_builder.build_article_records",
            side_effect=_track_articles,
        ),
        patch(
            "catalyst_data.index_builder.build_filing_records",
            side_effect=_track_filings,
        ),
    ):
        first = stage_corpus_candidate(
            conn,
            certified_snapshot_identity=SNAPSHOT,
            profile_versions=profiles,
            source_bundle_output_root=out,
            snapshot_id=SNAPSHOT,
            probe_report_id=PROBE,
            postbuild_readiness_id=POSTBUILD,
        )
        second = stage_corpus_candidate(
            conn,
            certified_snapshot_identity=SNAPSHOT,
            profile_versions=profiles,
            source_bundle_output_root=out,
            snapshot_id=SNAPSHOT,
            probe_report_id=PROBE,
            postbuild_readiness_id=POSTBUILD,
        )

    assert isinstance(first, InactiveCorpusCandidate)
    assert first.build_id == second.build_id
    assert first.projection_digest == second.projection_digest
    assert first.inventory_digest == second.inventory_digest
    assert first.document_count == second.document_count
    assert first.chunk_count == second.chunk_count
    assert first.chunk_count >= 2
    records = build_canonical_corpus_records(conn)
    assert first.projection_digest == compute_canonical_projection_digest(records)
    assert first.build_id == _expected_build_id(first.projection_digest, profiles)
    v1 = hashlib.sha256(
        canonical_json_bytes(
            {"schema_version": "corpus_streaming_build_v1", "certified_snapshot_identity": SNAPSHOT}
        )
    ).hexdigest()
    assert first.build_id != v1

    is_current = conn.execute(
        "SELECT is_current FROM corpus_manifest WHERE manifest_id=?",
        (first.manifest_id,),
    ).fetchone()[0]
    assert int(is_current) == 0
    assert article_calls == []
    assert filing_calls == []
    assert fts_calls == []
    assert cutover_calls == []
    assert fts5_calls == []
    assert _pointer_snapshot(conn, active_path) == before

    filing_asset_id, filing_version_id = _filing_asset_identity(conn)
    news_asset_id = conn.execute(
        "SELECT asset_id FROM canonical_assets WHERE asset_type='NEWS'"
    ).fetchone()[0]
    news_version_id = conn.execute(
        "SELECT canonical_content_version_id FROM canonical_content_versions WHERE asset_id=?",
        (news_asset_id,),
    ).fetchone()[0]
    news_doc = corpus_document_id(
        canonical_content_version_id=news_version_id,
        chunk_profile_version="news_v2",
    )
    chunks = conn.execute(
        """SELECT chunk_id, canonical_asset_id, content_version_id,
                  corpus_document_id, source_class, content_state,
                  available_at, dedup_cluster_id, independence_group_id,
                  parse_quality
           FROM corpus_build_chunks WHERE build_id=? ORDER BY chunk_id""",
        (first.build_id,),
    ).fetchall()
    assert chunks
    news_chunks = [row for row in chunks if row["canonical_asset_id"] == news_asset_id]
    filing_chunks = [row for row in chunks if row["canonical_asset_id"] == filing_asset_id]
    assert news_chunks
    assert filing_chunks
    assert any("item_1.01" in row["chunk_id"] or "item_2.02" in row["chunk_id"] for row in filing_chunks)
    assert all(row["corpus_document_id"] for row in chunks)
    assert any(row["corpus_document_id"] == news_doc for row in news_chunks)
    assert all(row["content_version_id"] == filing_version_id for row in filing_chunks)
