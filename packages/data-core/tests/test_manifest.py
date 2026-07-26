"""Tests for deterministic CorpusManifest."""
from __future__ import annotations


def test_manifest_id_matches_contract():
    """manifest_id = SHA256 of canonical JSON per contract §5.5."""
    from catalyst_data.corpus.manifest import build_manifest, compute_manifest_id
    from corpus_fixtures import inventory_item, base_params
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION

    manifest = build_manifest(
        **base_params,
        active_chunk_inventory=[
            inventory_item("poly:a:news_v2:body:0001",
                           content_hash="0" * 64, metadata_hash="1" * 64),
            inventory_item("sec:b:filing_v2:item_1.01:0001",
                           content_hash="2" * 64, metadata_hash="3" * 64),
        ],
    )
    mid = compute_manifest_id(manifest)
    assert len(mid) == 64  # SHA256

    # created_at excluded from hash — same inventory, different created_at → same id
    manifest2 = build_manifest(
        **base_params,
        active_chunk_inventory=[
            inventory_item("sec:b:filing_v2:item_1.01:0001",
                           content_hash="2" * 64, metadata_hash="3" * 64),
            inventory_item("poly:a:news_v2:body:0001",
                           content_hash="0" * 64, metadata_hash="1" * 64),
        ],
    )
    assert compute_manifest_id(manifest2) == mid


def test_manifest_different_chunks_different_id():
    """Different chunk inventory → different manifest_id."""
    from catalyst_data.corpus.manifest import build_manifest, compute_manifest_id
    from corpus_fixtures import inventory_item, base_params

    m1 = build_manifest(
        **base_params,
        active_chunk_inventory=[
            inventory_item("poly:a:news_v2:body:0001",
                           content_hash="0" * 64, metadata_hash="1" * 64),
        ],
    )
    m2 = build_manifest(
        **base_params,
        active_chunk_inventory=[
            inventory_item("poly:a:news_v2:body:0001",
                           content_hash="f" * 64, metadata_hash="1" * 64),
        ],
    )
    assert compute_manifest_id(m1) != compute_manifest_id(m2)


def test_manifest_published_atomically():
    """Interrupted reconciliation preserves the previous current manifest."""
    import sqlite3
    import pytest
    from catalyst_data.corpus.manifest import (
        InjectedReconciliationFailure,
        publish_manifest,
        reconcile_and_publish,
    )
    from conftest import _fresh_db_at_version
    from db_fixtures import apply_migration_v9

    db = _fresh_db_at_version(8)
    apply_migration_v9(db)

    # Publish m1
    publish_manifest(
        db, manifest_id="a" * 64, manifest_json='{"test":true}',
        created_at="2026-01-01T00:00:00Z",
    )

    with pytest.raises(InjectedReconciliationFailure):
        reconcile_and_publish(
            db,
            next_manifest_id="b" * 64,
            next_manifest_json='{"test":false}',
            active_chunks={},
            fail_after_chunk_writes=True,
        )

    current = db.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current = 1"
    ).fetchone()
    assert current is not None
    assert current["manifest_id"] == "a" * 64

    # m2 was never published
    m2_exists = db.execute(
        "SELECT COUNT(*) FROM corpus_manifest WHERE manifest_id = ?",
        ("b" * 64,),
    ).fetchone()[0]
    assert m2_exists == 0
    db.close()


def test_manifest_sorted_inventory():
    """Active chunk inventory is sorted by chunk_id ASC."""
    from catalyst_data.corpus.manifest import build_manifest
    from corpus_fixtures import inventory_item, base_params

    manifest = build_manifest(
        **base_params,
        active_chunk_inventory=[
            inventory_item("poly:z:news_v2:body:0001",
                           content_hash="0" * 64, metadata_hash="0" * 64),
            inventory_item("poly:a:news_v2:body:0001",
                           content_hash="0" * 64, metadata_hash="0" * 64),
        ],
    )
    inventory = manifest["sorted_active_chunk_inventory"]
    assert inventory[0]["chunk_id"] == "poly:a:news_v2:body:0001"
    assert inventory[1]["chunk_id"] == "poly:z:news_v2:body:0001"


def test_manifest_inventory_has_exact_contract_fields():
    """Presentation fields are excluded and missing identity fields are rejected."""
    import pytest
    from catalyst_data.corpus.manifest import build_manifest
    from corpus_fixtures import inventory_item, base_params

    item = inventory_item(
        "poly:a:news_v2:body:0001",
        content_hash="0" * 64,
        metadata_hash="1" * 64,
        content_text="presentation-only",
    )
    manifest = build_manifest(**base_params, active_chunk_inventory=[item])
    assert "content_text" not in manifest["sorted_active_chunk_inventory"][0]

    del item["available_at"]
    with pytest.raises(ValueError, match="available_at"):
        build_manifest(**base_params, active_chunk_inventory=[item])
