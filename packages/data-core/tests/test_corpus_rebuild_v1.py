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


def _insert_news(conn: sqlite3.Connection) -> None:
    asset_id = "v1:asset:news-fixture"
    version_id = "v1:content:news-fixture"
    conn.execute(
        """INSERT INTO canonical_assets (
            asset_id, asset_type, issuer_id, tickers_json, provider, publisher,
            canonical_url, source_class, source_published_at, eligible_at,
            eligible_at_reason, temporal_precision, accepted_time_recovered,
            fail_closed, ingested_at, content_state, serving_status, title,
            content_ref, dedup_cluster_id, independence_group_id, parse_quality,
            subtype_metadata, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            asset_id, "NEWS", "issuer:AAPL", '["AAPL"]', "polygon", "Fixture News",
            "https://example.test/news", "reported_news", NOW, NOW,
            "publication_time_provider", "publication_time", 0, 0, NOW,
            "FULL_TEXT", "body_candidate", "Unicode café news",
            "raw:news-1", "v1:dedup:news", "v1:ind:news", "not_applicable",
            json.dumps({"body": NEWS_BODY, "description": NEWS_BODY}),
            NOW, NOW,
        ),
    )
    conn.execute(
        """INSERT INTO canonical_content_versions (
            canonical_content_version_id, asset_id, content_hash,
            normalizer_version, materiality_version, version_ordinal,
            created_at, payload_ref
        ) VALUES (?,?,?,?,?,1,?,?)""",
        (
            version_id, asset_id, _h(NEWS_BODY), NEWS_NORMALIZER_VERSION,
            MATERIALITY_VERSION, NOW, "raw:news-1",
        ),
    )
    conn.execute(
        """INSERT INTO canonical_subtype_assoc (
            asset_id, subtype_table, subtype_pk, subtype_pk_value,
            canonical_content_version_id, created_at
        ) VALUES (?,?,?,?,?,?)""",
        (asset_id, "articles", "article_id", "poly:000001", version_id, NOW),
    )
    conn.execute(
        "INSERT INTO canonical_asset_tickers (asset_id, ticker, issuer_id) VALUES (?,?,?)",
        (asset_id, "AAPL", "issuer:AAPL"),
    )


def _insert_filing(conn: sqlite3.Connection) -> None:
    asset_id = "v1:asset:filing-fixture"
    version_id = "v1:content:filing-fixture"
    conn.execute(
        """INSERT INTO canonical_assets (
            asset_id, asset_type, issuer_id, tickers_json, provider, publisher,
            canonical_url, source_class, source_published_at, eligible_at,
            eligible_at_reason, temporal_precision, accepted_time_recovered,
            fail_closed, ingested_at, content_state, serving_status, title,
            content_ref, dedup_cluster_id, independence_group_id, parse_quality,
            subtype_metadata, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            asset_id, "FILING", "issuer:AAPL", '["AAPL"]', "sec", None,
            "https://example.test/8k", "issuer_disclosure", NOW, NOW,
            "accepted_time", "accepted_time", 1, 0, NOW,
            "FULL_TEXT", "body_candidate", "Apple 8-K",
            "raw:filing-1", "v1:dedup:filing", "v1:ind:filing", "full",
            json.dumps({"raw_text": FILING_BODY, "form_type": "8-K", "document_role": "primary_doc"}),
            NOW, NOW,
        ),
    )
    conn.execute(
        """INSERT INTO canonical_content_versions (
            canonical_content_version_id, asset_id, content_hash,
            normalizer_version, materiality_version, version_ordinal,
            created_at, payload_ref
        ) VALUES (?,?,?,?,?,1,?,?)""",
        (
            version_id, asset_id, _h(FILING_BODY), SEC_NORMALIZER_VERSION,
            MATERIALITY_VERSION, NOW, "raw:filing-1",
        ),
    )
    conn.execute(
        """INSERT INTO canonical_subtype_assoc (
            asset_id, subtype_table, subtype_pk, subtype_pk_value,
            canonical_content_version_id, created_at
        ) VALUES (?,?,?,?,?,?)""",
        (asset_id, "filing_documents", "document_id", "e" * 64, version_id, NOW),
    )
    conn.execute(
        "INSERT INTO canonical_asset_tickers (asset_id, ticker, issuer_id) VALUES (?,?,?)",
        (asset_id, "AAPL", "issuer:AAPL"),
    )


def _fixture_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    _insert_news(conn)
    _insert_filing(conn)
    conn.commit()
    return conn


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

    news_doc = corpus_document_id(
        canonical_content_version_id="v1:content:news-fixture",
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
    news_chunks = [row for row in chunks if row["canonical_asset_id"] == "v1:asset:news-fixture"]
    filing_chunks = [row for row in chunks if row["canonical_asset_id"] == "v1:asset:filing-fixture"]
    assert news_chunks
    assert filing_chunks
    assert any("item_1.01" in row["chunk_id"] or "item_2.02" in row["chunk_id"] for row in filing_chunks)
    assert all(row["corpus_document_id"] for row in chunks)
    assert any(row["corpus_document_id"] == news_doc for row in news_chunks)
