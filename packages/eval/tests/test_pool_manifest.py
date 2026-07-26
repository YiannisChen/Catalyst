"""Tests for PoolManifest schema."""
from __future__ import annotations

import datetime


def test_pool_manifest_construction():
    """PoolManifest with required fields."""
    from catalyst_eval.benchmark.pool_manifest import PoolManifest, PoolArm

    pm = PoolManifest(
        schema_version="1.0.0",
        pool_id="3de44e669c38964abca146c46b71e1a56067e8f54e37dd0deceecec45644bc2c",
        case_id="case-001",
        arms=(
            PoolArm(arm="lexical", version="1.0.0", top_k=8),
        ),
        chunk_inventory=("poly:a:news_v2:body:0001",),
        corpus_manifest_id="b" * 64,
        index_manifest_id=None,
        source_artifact_id="c" * 64,
        created_at=datetime.datetime(2026, 1, 20, 12, 0, 0, tzinfo=datetime.timezone.utc),
    )
    assert pm.schema_version == "1.0.0"
    assert pm.pool_id == "3de44e669c38964abca146c46b71e1a56067e8f54e37dd0deceecec45644bc2c"
    assert len(pm.arms) == 1


def test_pool_id_matches_computed():
    """pool_id is validated against canonical JSON hash."""
    import hashlib, json
    from catalyst_eval.benchmark.pool_manifest import PoolManifest, PoolArm

    arms = (PoolArm(arm="lexical", version="1.0.0", top_k=8),)
    inventory = ("poly:a:news_v2:body:0001",)

    hash_input = {
        "schema_version": "1.0.0",
        "case_id": "case-001",
        "arms": [{"arm": "lexical", "version": "1.0.0", "top_k": 8}],
        "chunk_inventory": list(inventory),
        "corpus_manifest_id": "b" * 64,
        "index_manifest_id": None,
        "source_artifact_id": "c" * 64,
    }
    expected = hashlib.sha256(
        json.dumps(hash_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    pm = PoolManifest(
        schema_version="1.0.0",
        pool_id=expected,
        case_id="case-001",
        arms=arms,
        chunk_inventory=inventory,
        corpus_manifest_id="b" * 64,
        index_manifest_id=None,
        source_artifact_id="c" * 64,
        created_at=datetime.datetime(2026, 1, 20, 12, 0, 0, tzinfo=datetime.timezone.utc),
    )
    assert pm.pool_id == expected
