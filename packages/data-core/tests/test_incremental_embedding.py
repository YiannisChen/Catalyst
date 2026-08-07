from __future__ import annotations


def test_incremental_embedding_only_content_changed():
    from catalyst_data.retrieval.embedder import plan_embedding_work

    index_state = {
        "chunk:a:1": {"content_hash": "same", "metadata_hash": "same"},
        "chunk:b:1": {"content_hash": "different", "metadata_hash": "different"},
        "chunk:c:1": {"content_hash": "same", "metadata_hash": "changed"},
    }
    active = {
        "chunk:a:1": {"content_hash": "same", "metadata_hash": "same"},
        "chunk:b:1": {"content_hash": "new_hash", "metadata_hash": "new_meta"},
        "chunk:c:1": {"content_hash": "same", "metadata_hash": "newer"},
    }
    plan = plan_embedding_work(index_state, active)
    assert "chunk:a:1" not in plan.to_embed
    assert "chunk:b:1" in plan.to_embed
    assert "chunk:c:1" in plan.to_update_metadata


def test_incremental_embedding_marks_tombstones():
    from catalyst_data.retrieval.embedder import plan_embedding_work

    plan = plan_embedding_work({"old": {"content_hash": "x", "metadata_hash": "m"}}, {})
    assert plan.to_tombstone == ("old",)

