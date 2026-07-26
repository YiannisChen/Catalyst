"""Tests for stable identities, content/metadata hash, and reconciliation."""
from __future__ import annotations

import hashlib


def test_document_id_is_provider_native():
    """document_id uses provider-native canonical identity."""
    from catalyst_data.articles import compute_article_id

    aid = compute_article_id("poly", "abc123")
    assert aid == "poly:abc123"


def test_content_hash_is_embedding_text_sha256():
    """content_hash = SHA256 of exact normalized embedding text."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article = {
        "document_id": "poly:art1",
        "title": "T",
        "description": "D",
        "available_at": "2026-01-01T09:00:00Z",
    }
    chunks = profile.chunk(article)
    expected_hash = hashlib.sha256("T\nD".encode("utf-8")).hexdigest()
    assert chunks[0].content_hash == expected_hash


def test_image_change_does_not_alter_content_hash():
    """Changing image_url preserves content_hash."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article1 = {
        "document_id": "poly:img1", "title": "T", "description": "D",
        "available_at": "2026-01-01T09:00:00Z", "image_url": "a.jpg",
    }
    article2 = {
        "document_id": "poly:img1", "title": "T", "description": "D",
        "available_at": "2026-01-01T09:00:00Z", "image_url": "b.jpg",
    }
    c1 = profile.chunk(article1)[0]
    c2 = profile.chunk(article2)[0]
    assert c1.content_hash == c2.content_hash


def test_ticker_change_alters_metadata_hash():
    """Ticker association change alters metadata_hash but not content_hash."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article1 = {
        "document_id": "poly:t1", "title": "T", "description": "D",
        "available_at": "2026-01-01T09:00:00Z", "ticker_associations": '["AAPL"]',
    }
    article2 = {
        "document_id": "poly:t1", "title": "T", "description": "D",
        "available_at": "2026-01-01T09:00:00Z", "ticker_associations": '["AAPL","MSFT"]',
    }
    c1 = profile.chunk(article1)[0]
    c2 = profile.chunk(article2)[0]
    assert c1.content_hash == c2.content_hash
    assert c1.metadata_hash != c2.metadata_hash


# ── Reconciliation tests ─────────────────────────────────────────────────────

def test_reconciliation_detects_removed_chunks():
    """Chunks previously active but now absent → tombstones emitted."""
    from catalyst_data.corpus.reconciliation import reconcile
    from corpus_fixtures import reconciliation_case

    case = reconciliation_case("document_removal")
    result = reconcile(
        case.db,
        active_chunks=case.active_chunks,
        manifest_id=case.next_manifest_id,
    )
    assert any(t.chunk_id == "poly:rem:news_v2:body:0001"
               for t in result.tombstones)


def test_reconciliation_detects_content_changes():
    """Content change → chunk marked for re-embedding."""
    from catalyst_data.corpus.reconciliation import reconcile
    from corpus_fixtures import reconciliation_case

    case = reconciliation_case("content_change")
    result = reconcile(
        case.db,
        active_chunks=case.active_chunks,
        manifest_id=case.next_manifest_id,
    )
    assert "poly:c1:news_v2:body:0001" in result.to_embed


def test_reconciliation_metadata_only_no_reembed():
    """Metadata-only change → update metadata, skip re-embedding."""
    from catalyst_data.corpus.reconciliation import reconcile
    from corpus_fixtures import reconciliation_case

    case = reconciliation_case("metadata_only_change")
    result = reconcile(
        case.db,
        active_chunks=case.active_chunks,
        manifest_id=case.next_manifest_id,
    )
    assert "poly:m1:news_v2:body:0001" not in result.to_embed
    assert "poly:m1:news_v2:body:0001" in result.to_update_metadata


def test_profile_version_change_tombstones_old():
    """Profile version change → old chunks tombstoned, new chunks with new IDs."""
    from catalyst_data.corpus.reconciliation import reconcile
    from corpus_fixtures import reconciliation_case

    case = reconciliation_case("profile_version_replacement")
    result = reconcile(
        case.db,
        active_chunks=case.active_chunks,
        manifest_id=case.next_manifest_id,
    )
    assert any(t.chunk_id == "poly:pv:news_v1:body:0001"
               for t in result.tombstones)
    assert "poly:pv:news_v2:body:0001" not in {
        t.chunk_id for t in result.tombstones
    }


def test_reconciliation_new_chunk_to_embed():
    """New chunk not in DB → marked for embedding."""
    from catalyst_data.corpus.reconciliation import reconcile
    from corpus_fixtures import reconciliation_case

    case = reconciliation_case("new_chunk")
    result = reconcile(
        case.db,
        active_chunks=case.active_chunks,
        manifest_id=case.next_manifest_id,
    )
    assert "poly:n2:news_v2:body:0001" in result.to_embed


def test_reconciliation_eligibility_loss():
    """Eligibility loss → tombstone emitted."""
    from catalyst_data.corpus.reconciliation import reconcile
    from corpus_fixtures import reconciliation_case

    case = reconciliation_case("eligibility_loss")
    result = reconcile(
        case.db,
        active_chunks=case.active_chunks,
        manifest_id=case.next_manifest_id,
    )
    assert any(t.chunk_id == "poly:inel:news_v2:body:0001"
               for t in result.tombstones)


def test_disappeared_child_tombstoned():
    """Disappeared child chunk → tombstone."""
    from catalyst_data.corpus.reconciliation import reconcile
    from corpus_fixtures import reconciliation_case

    case = reconciliation_case("disappeared_child")
    result = reconcile(
        case.db,
        active_chunks=case.active_chunks,
        manifest_id=case.next_manifest_id,
    )
    assert any(t.chunk_id == "poly:dc:news_v2:body:0002"
               for t in result.tombstones)


def test_reconcile_uses_real_index_state_schema_and_respects_caller_transaction():
    """Reconciliation uses corpus_item_id and never commits its caller's transaction."""
    from catalyst_data.corpus.reconciliation import reconcile
    from conftest import _fresh_db_at_version
    from db_fixtures import apply_migration_v9

    db = _fresh_db_at_version(8)
    apply_migration_v9(db)
    manifest_id = "a" * 64
    db.execute(
        "INSERT INTO corpus_manifest VALUES (?, '{}', 0, ?)",
        (manifest_id, "2026-01-01T00:00:00Z"),
    )
    db.execute(
        """INSERT INTO index_state
           (chunk_id, chunk_level, corpus_item_id, source_kind, content_hash,
            content_text, status, metadata_hash, is_tombstone)
           VALUES (?, 'l2', ?, 'article', ?, 'old', 'embedded', ?, 0)""",
        ("poly:old:news_v2:body:0001", "poly:old", "1" * 64, "2" * 64),
    )
    db.commit()

    db.execute("BEGIN")
    result = reconcile(db, {}, manifest_id=manifest_id, commit=False)
    assert [item.reason for item in result.tombstones] == ["document_removed"]
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM corpus_tombstones").fetchone()[0] == 0


def test_one_polygon_response_materializes_thirty_canonical_corpus_documents():
    """One raw response with N items retains N provenance and corpus identities."""
    import asyncio

    from catalyst_data.connectors.polygon import fetch_paginated_news
    from catalyst_data.index_builder import build_corpus
    from conftest import _fresh_db_at_version, _seed_ingestion_run
    from db_fixtures import apply_migration_v9

    db = _fresh_db_at_version(8)
    _seed_ingestion_run(db, run_id="run-thirty")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "results": [
                    {
                        "id": f"article-{index:02d}",
                        "title": f"Article {index:02d}",
                        "description": "Canonical description.",
                        "published_utc": "2026-01-01T00:00:00Z",
                        "article_url": f"https://www.reuters.com/article/{index}",
                        "tickers": ["AAPL"],
                        "publisher": {"name": "Reuters"},
                    }
                    for index in range(30)
                ],
                "next_url": None,
            }

    async def transport(*args, **kwargs):
        return FakeResponse()

    result = asyncio.run(fetch_paginated_news(
        db=db,
        run_id="run-thirty",
        ticker="AAPL",
        date="2026-01-01",
        transport=transport,
        page_limit=1,
        item_limit=30,
    ))
    assert result["articles_upserted"] == 30
    assert db.execute(
        "SELECT COUNT(*) FROM raw_assets WHERE data_version = 'v2'"
    ).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 30
    assert db.execute("SELECT COUNT(*) FROM normalized_provenance").fetchone()[0] == 30

    apply_migration_v9(db)
    corpus = build_corpus(db, certified_snapshot_identity="snapshot-thirty")
    assert len({chunk.document_id for chunk in corpus.chunks}) == 30
